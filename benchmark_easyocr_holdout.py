"""benchmark_easyocr_holdout.py -- head-to-head OCR accuracy on labels/eval_holdout.csv.

eval_holdout.csv is the 155-crop hand-labeled set that is structurally excluded from
label_crops.py's training-label collection (see labels/eval_holdout_manifest.json), so it
was never seen by any of these models during training -- this is the real test, not the
in-training validation split.

Compares up to three EasyOCR recog_network variants by swapping the weight file at
models/easyocr_custom/apex.pth between runs:
  - stock:        base english_g2 model, no custom recog_network at all
  - old_deployed: whatever was in production before this run (backed up automatically)
  - new_candidate: the newly fine-tuned checkpoint being evaluated

Usage:
    python benchmark_easyocr_holdout.py --new models/easyocr_custom/apex_new_candidate.pth
"""

import argparse
import csv
import shutil
import statistics
from difflib import SequenceMatcher
from pathlib import Path

import cv2

import ocr as ocr_mod
from database import PlayerDatabase
from parsers import parse_killfeed_line
from pipeline_evaluator import character_levenshtein

HOLDOUT_CSV = Path("labels/eval_holdout.csv")
CUSTOM_MODEL_DIR = Path("models/easyocr_custom")
APEX_PTH = CUSTOM_MODEL_DIR / "apex.pth"
SNAPSHOT_PTH = CUSTOM_MODEL_DIR / ".apex_pth_snapshot_before_benchmark"


def load_holdout() -> list[dict]:
    rows = []
    with HOLDOUT_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if Path(r["filepath"]).exists():
                rows.append(r)
    return rows


def _parsed_tuple(text: str, db: PlayerDatabase) -> tuple:
    res = parse_killfeed_line(text, db) or {}
    norm = lambda v: (v or "").strip().lower()
    return (norm(res.get("event_type")), norm(res.get("attacker")), norm(res.get("victim")))


def run_variant(rows: list[dict]) -> dict:
    """OCR every row with whatever model is currently loaded, return per-row results."""
    ocr_mod._easyocr_reader = None
    ocr_mod._get_easyocr_reader()  # forces load now, not lazily on first readtext() call

    results = []
    for row in rows:
        img = cv2.imread(row["filepath"])
        if img is None:
            continue
        processed, _, _ = ocr_mod.preprocess_for_easyocr(img)
        text = ocr_mod.ocr_with_easyocr(processed).strip()
        results.append({**row, "ocr_text": text})
    return results


def score_variant(name: str, results: list[dict]) -> dict:
    db = PlayerDatabase()
    killfeed = [r for r in results if r["quality"] == "killfeed"]
    nonkillfeed = [r for r in results if r["quality"] == "nonkillfeed"]
    noise = [r for r in results if r["quality"] == "noise"]

    def text_metrics(subset):
        sims, cers, exact = [], [], 0
        for r in subset:
            gt, ocr_text = r["label"].strip(), r["ocr_text"]
            sims.append(SequenceMatcher(None, gt.lower(), ocr_text.lower()).ratio())
            cers.append(character_levenshtein(gt, ocr_text) / max(1, len(gt)))
            if gt.lower() == ocr_text.lower():
                exact += 1
        n = len(subset) or 1
        return {
            "n": len(subset),
            "exact_match_pct": round(100 * exact / n, 1),
            "avg_similarity": round(statistics.mean(sims), 4) if sims else 0.0,
            "avg_cer": round(statistics.mean(cers), 4) if cers else 0.0,
        }

    parsed_correct = 0
    for r in killfeed:
        gt_tuple = _parsed_tuple(r["label"], db)
        ocr_tuple = _parsed_tuple(r["ocr_text"], db)
        if gt_tuple == ocr_tuple and gt_tuple != ("", "", ""):
            parsed_correct += 1
    parsed_pct = round(100 * parsed_correct / max(1, len(killfeed)), 1)

    empty_correct = sum(1 for r in noise if len(r["ocr_text"]) <= 2)
    noise_pct = round(100 * empty_correct / max(1, len(noise)), 1)

    return {
        "variant": name,
        "killfeed": text_metrics(killfeed),
        "killfeed_parsed_correct_pct": parsed_pct,
        "nonkillfeed": text_metrics(nonkillfeed),
        "noise_correctly_empty_pct": noise_pct,
    }


def swap_model(variant: str, old_backup: Path | None, new_candidate: Path | None):
    if variant == "stock":
        if APEX_PTH.exists():
            APEX_PTH.unlink()
    elif variant == "old_deployed":
        shutil.copy(old_backup, APEX_PTH)
    elif variant == "new_candidate":
        shutil.copy(new_candidate, APEX_PTH)


def print_report(all_scores: list[dict]):
    print("\n" + "=" * 100)
    print(f"{'Variant':<16}{'KF n':>6}{'KF exact%':>11}{'KF sim':>9}{'KF CER':>9}{'KF parsed%':>12}{'noise ok%':>11}{'nonKF sim':>11}")
    print("-" * 100)
    for s in all_scores:
        kf = s["killfeed"]
        nkf = s["nonkillfeed"]
        print(f"{s['variant']:<16}{kf['n']:>6}{kf['exact_match_pct']:>10}%{kf['avg_similarity']:>9}{kf['avg_cer']:>9}"
              f"{s['killfeed_parsed_correct_pct']:>11}%{s['noise_correctly_empty_pct']:>10}%{nkf['avg_similarity']:>11}")
    print("=" * 100)
    print("KF = killfeed-quality rows (94). 'parsed%' = attacker/victim/event_type match after parsers.parse_killfeed_line.")
    print("'noise ok%' = OCR correctly produced ~empty output on blank/noise crops (19).")


def main():
    ap = argparse.ArgumentParser(description="Head-to-head EasyOCR accuracy on the held-out eval set")
    ap.add_argument("--new", required=True, help="Path to the new candidate .pth to evaluate")
    ap.add_argument("--old-backup", default=None, help="Path to the currently-deployed (pre-swap) .pth backup")
    ap.add_argument("--skip-stock", action="store_true", help="Skip the stock (no custom model) baseline")
    ap.add_argument("--skip-old", action="store_true", help="Skip the old-deployed baseline")
    args = ap.parse_args()

    new_candidate = Path(args.new)
    old_backup = Path(args.old_backup) if args.old_backup else None
    if old_backup is None:
        candidates = sorted(CUSTOM_MODEL_DIR.glob("apex.pth.bak_*"))
        old_backup = candidates[-1] if candidates else None

    rows = load_holdout()
    print(f"Loaded {len(rows)} holdout rows "
          f"(killfeed={sum(1 for r in rows if r['quality']=='killfeed')}, "
          f"nonkillfeed={sum(1 for r in rows if r['quality']=='nonkillfeed')}, "
          f"noise={sum(1 for r in rows if r['quality']=='noise')})")

    variants = []
    if not args.skip_stock:
        variants.append("stock")
    if not args.skip_old and old_backup and old_backup.exists():
        variants.append("old_deployed")
    variants.append("new_candidate")

    # Snapshot whatever is deployed right now so it can be restored byte-for-byte
    # afterward -- this script must never leave production state changed as a
    # side effect of benchmarking, regardless of which variants ran or in what order.
    had_original = APEX_PTH.exists()
    if had_original:
        shutil.copy(APEX_PTH, SNAPSHOT_PTH)

    all_scores = []
    try:
        for variant in variants:
            print(f"\n--- Running variant: {variant} ---")
            swap_model(variant, old_backup, new_candidate)
            results = run_variant(rows)
            all_scores.append(score_variant(variant, results))
    finally:
        if had_original:
            shutil.move(str(SNAPSHOT_PTH), str(APEX_PTH))
        elif APEX_PTH.exists():
            APEX_PTH.unlink()

    print_report(all_scores)


if __name__ == "__main__":
    main()
