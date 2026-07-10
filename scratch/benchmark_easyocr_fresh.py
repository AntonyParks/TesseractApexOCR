"""benchmark_easyocr_fresh.py — EasyOCR accuracy on the fresh, detection-fixed crop batch.

Unlike the earlier "keon" test set (mostly non-killfeed, captured before the detection fix),
this batch (gent/bubblegum/demoniio) was collected AFTER the killfeed-detection fix, using
STREAMER_SEARCH_ZONES where available. Ground truth is human-verified (via direct visual
inspection), frozen in test_crops/ground_truth_fresh.json before this script runs.

Uses the same metric implementations as pipeline_evaluator.py for consistency, and the
production-default preprocessing filter (F2: Inverted Grayscale) identified as best in the
earlier 8-filter sweep.
"""
import json
import statistics
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2

from ocr import ocr_with_easyocr, _get_easyocr_reader
from pipeline_evaluator import character_levenshtein, word_levenshtein

RAW_DIR = Path("test_crops/raw")
GT_FILE = Path("test_crops/ground_truth_fresh.json")


def _upscale_pad(gray_img):
    up = cv2.resize(gray_img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(up, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)


def filter_inverted_grayscale(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return _upscale_pad(cv2.bitwise_not(gray))


def compute_metrics(gt: str, text: str) -> dict:
    gt_l, text_l = gt.lower().strip(), text.lower().strip()
    similarity = SequenceMatcher(None, gt_l, text_l).ratio()
    exact = gt_l == text_l
    cer = character_levenshtein(gt, text) / max(1, len(gt))
    wer = word_levenshtein(gt.split(), text.split()) / max(1, len(gt.split()))
    return {"similarity": similarity, "exact": exact, "cer": cer, "wer": wer}


def main():
    gt_map = json.loads(GT_FILE.read_text(encoding="utf-8"))
    crops = [c for c in sorted(RAW_DIR.glob("*.png")) if c.name in gt_map]
    non_blank = [c for c in crops if gt_map[c.name].strip()]
    print(f"Evaluating {len(crops)} crops ({len(non_blank)} non-blank) from the fresh batch\n")

    print("Warming up EasyOCR reader...")
    _get_easyocr_reader()

    rows = []
    for i, crop_path in enumerate(crops, 1):
        gt = gt_map[crop_path.name]
        bgr = cv2.imread(str(crop_path))
        if bgr is None:
            continue
        processed = filter_inverted_grayscale(bgr)
        t0 = time.perf_counter()
        text = ocr_with_easyocr(processed)
        elapsed = (time.perf_counter() - t0) * 1000
        m = compute_metrics(gt, text)
        rows.append({"file": crop_path.name, "gt": gt, "text": text, "time_ms": elapsed, **m})
        tag = "BLANK" if not gt.strip() else "TEXT "
        print(f"[{i}/{len(crops)}] {tag} {crop_path.name}")
        print(f"    GT:   {gt!r}")
        print(f"    pred: {text!r}  (sim={m['similarity']:.0%}, {elapsed:.0f}ms)")

    print("\n" + "=" * 90)
    print(f"EASYOCR ACCURACY — FRESH DETECTION-FIXED BATCH (N={len(crops)}, {len(non_blank)} non-blank)")
    print("=" * 90)

    nb_rows = [r for r in rows if r["gt"].strip()]
    blank_rows = [r for r in rows if not r["gt"].strip()]

    if nb_rows:
        avg_sim = statistics.mean(r["similarity"] for r in nb_rows)
        avg_cer = statistics.mean(r["cer"] for r in nb_rows)
        avg_wer = statistics.mean(r["wer"] for r in nb_rows)
        exact_pct = 100.0 * sum(r["exact"] for r in nb_rows) / len(nb_rows)
        print(f"Non-blank (N={len(nb_rows)}): avg similarity={avg_sim:.1%}  avg CER={avg_cer:.1%}  "
              f"avg WER={avg_wer:.1%}  exact match={exact_pct:.1f}%")

    if blank_rows:
        blank_correct = sum(1 for r in blank_rows if not r["text"].strip())
        print(f"Blank crops (N={len(blank_rows)}): correctly returned empty = "
              f"{blank_correct}/{len(blank_rows)} ({100*blank_correct/len(blank_rows):.0f}%)")

    avg_time = statistics.mean(r["time_ms"] for r in rows)
    print(f"Avg inference time: {avg_time:.0f}ms")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_crops": len(crops),
        "n_non_blank": len(nb_rows),
        "rows": rows,
    }
    Path("scratch/easyocr_fresh_results.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nSaved: scratch/easyocr_fresh_results.json")


if __name__ == "__main__":
    main()
