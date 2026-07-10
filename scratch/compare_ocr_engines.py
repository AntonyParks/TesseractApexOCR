"""compare_ocr_engines.py — Head-to-head TrOCR vs EasyOCR accuracy on today's Keon test crops.

Reuses the exact metric implementations from pipeline_evaluator.py (character/word
Levenshtein, confusion alignment) so results are computed the same way as the
historical TrOCR pipeline health report. Ground truth is frozen in
test_crops/ground_truth.json (labeled via Gemini) before this script runs —
it does not call Gemini itself.

Each engine gets its own best preprocessing:
  - TrOCR:   production preprocess_for_trocr() (HSV-V channel + Bilateral+Otsu, best-of-2)
  - EasyOCR: swept across all 8 filters from benchmark_easyocr_filters.py, plus a
             beamsearch decoder variant on the best-performing filter.

Usage:
    python scratch/compare_ocr_engines.py
"""
import json
import statistics
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2

import config
from ocr import preprocess_for_trocr, ocr_with_easyocr, _get_easyocr_reader
from trocr_inference import ocr_with_trocr
from pipeline_evaluator import character_levenshtein, word_levenshtein
from benchmark_easyocr_filters import FILTERS, RAW_DIR, GT_FILE

BEST_FILTER_NAME_HINT = "F2: Inverted Grayscale"  # winner of the earlier 8-filter sweep


def compute_metrics(gt: str, text: str) -> dict:
    gt_l, text_l = gt.lower().strip(), text.lower().strip()
    similarity = SequenceMatcher(None, gt_l, text_l).ratio()
    exact = gt_l == text_l
    cer = character_levenshtein(gt, text) / max(1, len(gt))
    wer = word_levenshtein(gt.split(), text.split()) / max(1, len(gt.split()))
    return {"similarity": similarity, "exact": exact, "cer": cer, "wer": wer}


def summarize(rows: list[dict]) -> dict:
    non_blank = [r for r in rows if r["gt"].strip()]
    blank = [r for r in rows if not r["gt"].strip()]
    return {
        "n": len(non_blank),
        "avg_similarity": statistics.mean(r["similarity"] for r in non_blank) if non_blank else 0.0,
        "avg_cer": statistics.mean(r["cer"] for r in non_blank) if non_blank else 0.0,
        "avg_wer": statistics.mean(r["wer"] for r in non_blank) if non_blank else 0.0,
        "exact_pct": 100.0 * sum(r["exact"] for r in non_blank) / len(non_blank) if non_blank else 0.0,
        "avg_time_ms": statistics.mean(r["time_ms"] for r in rows) if rows else 0.0,
        "n_blank": len(blank),
        "blank_correct_pct": 100.0 * sum(1 for r in blank if not r["text"].strip()) / len(blank) if blank else None,
    }


def main():
    gt_map = json.loads(GT_FILE.read_text(encoding="utf-8"))
    crops = [c for c in sorted(RAW_DIR.glob("*.png")) if c.name in gt_map]
    print(f"Evaluating on {len(crops)} labeled crops ({sum(1 for v in gt_map.values() if v.strip())} non-blank)\n")

    print("Warming up EasyOCR + TrOCR readers...")
    _get_easyocr_reader()
    reader = _get_easyocr_reader()

    # results["trocr"] and results[filter_name] for each EasyOCR filter, plus a beamsearch variant
    results = defaultdict(list)

    for i, crop_path in enumerate(crops, 1):
        gt = gt_map[crop_path.name]
        bgr = cv2.imread(str(crop_path))
        if bgr is None:
            continue
        print(f"[{i}/{len(crops)}] {crop_path.name}  GT={gt!r}")

        # --- TrOCR (production preprocessing) ---
        processed, _, _ = preprocess_for_trocr(bgr)
        t0 = time.perf_counter()
        text, conf = ocr_with_trocr(processed, [], config.TROCR_MODEL_PATH)
        elapsed = (time.perf_counter() - t0) * 1000
        m = compute_metrics(gt, text)
        results["trocr"].append({"gt": gt, "text": text, "time_ms": elapsed, **m})
        print(f"    trocr                          : {text!r}  (sim={m['similarity']:.0%}, {elapsed:.0f}ms)")

        # --- EasyOCR across all 8 filters ---
        best_filter_processed = None
        for filter_name, filter_fn in FILTERS:
            processed_img = filter_fn(bgr)
            t0 = time.perf_counter()
            text = ocr_with_easyocr(processed_img)
            elapsed = (time.perf_counter() - t0) * 1000
            m = compute_metrics(gt, text)
            results[filter_name].append({"gt": gt, "text": text, "time_ms": elapsed, **m})
            print(f"    {filter_name[:30]:<30}: {text!r}  (sim={m['similarity']:.0%}, {elapsed:.0f}ms)")
            if filter_name == BEST_FILTER_NAME_HINT:
                best_filter_processed = processed_img

        # --- EasyOCR beamsearch decoder variant on the best filter ---
        if best_filter_processed is not None:
            t0 = time.perf_counter()
            raw = reader.readtext(best_filter_processed, detail=0, paragraph=False, decoder="beamsearch")
            text = " ".join(raw)
            elapsed = (time.perf_counter() - t0) * 1000
            m = compute_metrics(gt, text)
            results["EasyOCR (best filter + beamsearch)"].append({"gt": gt, "text": text, "time_ms": elapsed, **m})
            print(f"    {'EasyOCR beamsearch':<30}: {text!r}  (sim={m['similarity']:.0%}, {elapsed:.0f}ms)")

        print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("=" * 100)
    print(f"HEAD-TO-HEAD OCR ENGINE COMPARISON  (N={len(crops)} crops, Keon stream, {time.strftime('%Y-%m-%d')})")
    print("=" * 100)
    header = f"{'Engine/Config':<38} | {'Sim':>6} | {'CER':>6} | {'WER':>6} | {'Exact':>6} | {'Blank OK':>8} | {'Time':>7}"
    print(header)
    print("-" * len(header))

    summaries = {name: summarize(rows) for name, rows in results.items()}
    ranked = sorted(summaries.items(), key=lambda kv: kv[1]["avg_similarity"], reverse=True)
    for name, s in ranked:
        blank_str = f"{s['blank_correct_pct']:.0f}%" if s["blank_correct_pct"] is not None else "n/a"
        print(f"{name:<38} | {s['avg_similarity']:>5.1%} | {s['avg_cer']:>5.1%} | {s['avg_wer']:>5.1%} | "
              f"{s['exact_pct']:>5.1f}% | {blank_str:>8} | {s['avg_time_ms']:>6.0f}ms")
    print("=" * 100)

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_crops": len(crops),
        "n_non_blank": sum(1 for v in gt_map.values() if v.strip()),
        "summaries": summaries,
    }
    out_path = Path("scratch/ocr_engine_comparison.json")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
