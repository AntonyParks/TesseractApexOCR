"""
benchmark_easyocr_filters.py — Compare EasyOCR accuracy across preprocessing filters.

Reads raw killfeed crops from test_crops/raw/, applies 8 different preprocessing
pipelines, runs EasyOCR on each variant, and compares against Gemini ground truth.
Produces a ranked performance matrix.

Usage:
    python scratch/benchmark_easyocr_filters.py [--sample 30] [--no-gemini]
"""
import argparse
import base64
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2
import numpy as np

from config import EASYOCR_GAP_THRESHOLD
from ocr import ocr_with_easyocr, _get_easyocr_reader

# Load .env for Gemini API
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Models to try (Gemini 3.5 Flash is quota-ready)
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

RAW_DIR = Path("test_crops/raw")
GT_FILE = Path("test_crops/ground_truth.json")
REPORT_DIR = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\d375a404-f651-426c-8536-6759a297562e")

GEMINI_PROMPT = """You are an expert Apex Legends OCR analyst.
This is a RAW (unprocessed, original colors) crop from the in-game killfeed.
Transcribe the text exactly as it appears. Use <GUN_ICON> where the weapon icon sits between attacker and victim names.
Output ONLY raw JSON: {"transcription": "exact text here"}
Do not include markdown formatting."""


# ---------------------------------------------------------------------------
# Preprocessing Filters
# ---------------------------------------------------------------------------

def _upscale_pad(gray_img: np.ndarray) -> np.ndarray:
    """Standard 2x upscale + 15px white padding."""
    up = cv2.resize(gray_img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(up, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)


def filter_baseline_hsv_mask(bgr: np.ndarray) -> np.ndarray:
    """F1: HSV color mask (white + yellow + red) → inverted binary → 2x."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask_w = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_y = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_r1 = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255]))
    mask_r2 = cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    combined = mask_w | mask_y | mask_r1 | mask_r2
    return _upscale_pad(cv2.bitwise_not(combined))


def filter_inverted_grayscale(bgr: np.ndarray) -> np.ndarray:
    """F2: Simple grayscale inversion → 2x."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return _upscale_pad(cv2.bitwise_not(gray))


def filter_hsv_value_channel(bgr: np.ndarray) -> np.ndarray:
    """F3: HSV Value channel inversion → 2x. (Current pipeline default)"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    _, _, v = cv2.split(hsv)
    return _upscale_pad(cv2.bitwise_not(v))


def filter_otsu_threshold(bgr: np.ndarray) -> np.ndarray:
    """F4: Otsu auto-threshold binarization → 2x."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(thresh) < 127:
        thresh = cv2.bitwise_not(thresh)
    return _upscale_pad(thresh)


def filter_adaptive_threshold(bgr: np.ndarray) -> np.ndarray:
    """F5: Adaptive Gaussian threshold → 2x."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 11, 2,
    )
    return _upscale_pad(cv2.bitwise_not(thresh))


def filter_bilateral_otsu(bgr: np.ndarray) -> np.ndarray:
    """F6: Bilateral filter + Otsu binarization → 2x."""
    smoothed = cv2.bilateralFilter(bgr, 9, 75, 75)
    gray = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(thresh) < 127:
        thresh = cv2.bitwise_not(thresh)
    return _upscale_pad(thresh)


def filter_clahe(bgr: np.ndarray) -> np.ndarray:
    """F7: CLAHE contrast enhancement → inverted grayscale → 2x."""
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    yuv[:, :, 0] = clahe.apply(yuv[:, :, 0])
    equalized = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    gray = cv2.cvtColor(equalized, cv2.COLOR_BGR2GRAY)
    return _upscale_pad(cv2.bitwise_not(gray))


def filter_morphological_cleanup(bgr: np.ndarray) -> np.ndarray:
    """F8: HSV mask + morphological open/close to clean up noise → 2x."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask_w = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_y = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_r1 = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255]))
    mask_r2 = cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    combined = mask_w | mask_y | mask_r1 | mask_r2
    # Morphological open (remove small noise) then close (fill small holes)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    cleaned = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)
    return _upscale_pad(cv2.bitwise_not(cleaned))


# All filter pipelines in test order
FILTERS = [
    ("F1: HSV Color Mask (baseline)",      filter_baseline_hsv_mask),
    ("F2: Inverted Grayscale",             filter_inverted_grayscale),
    ("F3: HSV Value Channel",              filter_hsv_value_channel),
    ("F4: Otsu Threshold",                 filter_otsu_threshold),
    ("F5: Adaptive Gaussian Threshold",    filter_adaptive_threshold),
    ("F6: Bilateral + Otsu",              filter_bilateral_otsu),
    ("F7: CLAHE Contrast",                filter_clahe),
    ("F8: HSV Mask + Morphological",      filter_morphological_cleanup),
]


# ---------------------------------------------------------------------------
# Gemini ground truth
# ---------------------------------------------------------------------------

def get_gemini_ground_truth(raw_img_path: Path) -> str | None:
    """Send the RAW crop to Gemini for ground truth transcription."""
    if not GEMINI_API_KEY:
        return None
    b64 = base64.b64encode(raw_img_path.read_bytes()).decode()
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/png", "data": b64}},
            {"text": GEMINI_PROMPT},
        ]}],
        "generationConfig": {
            "maxOutputTokens": 256,
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }
    for attempt in range(3):
        try:
            import requests
            resp = requests.post(GEMINI_URL, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"      Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            return data.get("transcription", "")
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"      Gemini error: {e}")
                return None
    return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=30,
                        help="Max crops to test (default 30)")
    parser.add_argument("--no-gemini", action="store_true",
                        help="Skip Gemini ground truth (just show EasyOCR outputs)")
    parser.add_argument("--gemini-delay", type=float, default=4.5,
                        help="Seconds between Gemini calls (default 4.5)")
    args = parser.parse_args()

    # Find raw crops
    crops = sorted(RAW_DIR.glob("*.png"))[:args.sample]
    if not crops:
        print(f"No crops found in {RAW_DIR}/")
        print("Run  python scratch/collect_test_crops.py  first to capture crops.")
        return

    # Load offline ground truth database if available
    gt_data = {}
    if GT_FILE.exists():
        try:
            gt_data = json.loads(GT_FILE.read_text(encoding="utf-8"))
            print(f"Loaded {len(gt_data)} ground truth labels from JSON.")
        except Exception as e:
            print(f"Warning: Could not load ground_truth.json: {e}")

    use_gemini = (GEMINI_API_KEY and not args.no_gemini) or len(gt_data) > 0
    print(f"Found {len(crops)} raw crops in {RAW_DIR}/")
    print(f"Gemini ground truth: {'enabled' if use_gemini else 'disabled'}")
    print(f"Filters: {len(FILTERS)}")
    print()

    # Init EasyOCR reader
    print("Initializing EasyOCR reader...")
    _get_easyocr_reader()
    print()

    # Aggregate results: filter_name -> list of similarities
    agg_sim = defaultdict(list)
    agg_time = defaultdict(list)
    results = []  # per-crop details

    for crop_idx, crop_path in enumerate(crops):
        print(f"[{crop_idx + 1}/{len(crops)}] {crop_path.name}")

        bgr = cv2.imread(str(crop_path))
        if bgr is None:
            print("  ERROR: Could not load image")
            continue

        # Get ground truth (JSON first, API fallback second)
        ground_truth = None
        if use_gemini:
            if crop_path.name in gt_data:
                ground_truth = gt_data[crop_path.name]
                print(f"  GROUND TRUTH (Offline): {ground_truth!r}")
            elif GEMINI_API_KEY and not args.no_gemini:
                ground_truth = get_gemini_ground_truth(crop_path)
                if ground_truth:
                    print(f"  GROUND TRUTH (API): {ground_truth!r}")
                else:
                    print(f"  GROUND TRUTH (API): (failed)")
                time.sleep(args.gemini_delay)

        crop_result = {"file": crop_path.name, "ground_truth": ground_truth, "filters": {}}

        for filter_name, filter_fn in FILTERS:
            try:
                processed = filter_fn(bgr)
            except Exception as e:
                print(f"    {filter_name}: PREPROCESS ERROR: {e}")
                continue

            t0 = time.perf_counter()
            ocr_text = ocr_with_easyocr(processed)
            elapsed = (time.perf_counter() - t0) * 1000

            agg_time[filter_name].append(elapsed)

            if ground_truth:
                sim = similarity(ocr_text, ground_truth)
                agg_sim[filter_name].append(sim)
                print(f"    {filter_name[:35]:<35}: {ocr_text!r}  (sim={sim:.0%}, {elapsed:.0f}ms)")
            else:
                print(f"    {filter_name[:35]:<35}: {ocr_text!r}  ({elapsed:.0f}ms)")

            crop_result["filters"][filter_name] = {
                "text": ocr_text,
                "similarity": sim if ground_truth else None,
                "time_ms": elapsed,
            }

        results.append(crop_result)
        print()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("=" * 90)
    print(f"EASYOCR PREPROCESSING FILTER BENCHMARK  (N={len(crops)} crops)")
    print("=" * 90)

    if agg_sim:
        print(f"\n{'Filter':<40} | {'Avg Sim':>8} | {'Min':>5} | {'≥85%':>5} | {'≥95%':>5} | {'Time':>6}")
        print("-" * 90)
        ranked = sorted(FILTERS, key=lambda f: statistics.mean(agg_sim.get(f[0], [0])), reverse=True)
        for filter_name, _ in ranked:
            sims = agg_sim.get(filter_name, [])
            times = agg_time.get(filter_name, [])
            if not sims:
                continue
            avg_s = statistics.mean(sims)
            min_s = min(sims)
            above_85 = sum(1 for s in sims if s >= 0.85)
            above_95 = sum(1 for s in sims if s >= 0.95)
            avg_t = statistics.mean(times) if times else 0
            n = len(sims)
            print(f"  {filter_name:<38} | {avg_s:>7.1%} | {min_s:>4.0%} | "
                  f"{above_85:>2}/{n:<2} | {above_95:>2}/{n:<2} | {avg_t:>5.0f}ms")
        print("=" * 90)
    else:
        print("\nNo Gemini comparisons available — showing raw EasyOCR outputs only.")
        print("Re-run without --no-gemini to get accuracy metrics.\n")

    # Save markdown report
    report_path = REPORT_DIR / "filter_benchmark_report.md"
    _write_report(report_path, results, agg_sim, agg_time, len(crops))
    print(f"\nReport saved to: {report_path}")


def _write_report(path: Path, results, agg_sim, agg_time, n_crops):
    lines = [
        "# EasyOCR Filter Benchmark Report\n\n",
        f"**Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"**Crops tested**: {n_crops}\n",
        f"**Ground truth**: Gemini {GEMINI_MODEL}\n\n",
    ]

    if agg_sim:
        lines.append("## Filter Ranking (by average similarity)\n\n")
        lines.append("| Rank | Filter | Avg Similarity | Min | ≥85% | ≥95% | Avg Time |\n")
        lines.append("|---|---|---|---|---|---|---|\n")
        ranked = sorted(
            [(name, agg_sim[name]) for name, _ in FILTERS if name in agg_sim],
            key=lambda x: statistics.mean(x[1]),
            reverse=True,
        )
        for rank, (name, sims) in enumerate(ranked, 1):
            avg_s = statistics.mean(sims)
            min_s = min(sims)
            above_85 = sum(1 for s in sims if s >= 0.85)
            above_95 = sum(1 for s in sims if s >= 0.95)
            avg_t = statistics.mean(agg_time.get(name, [0]))
            n = len(sims)
            lines.append(f"| {rank} | {name} | {avg_s:.1%} | {min_s:.0%} | "
                         f"{above_85}/{n} | {above_95}/{n} | {avg_t:.0f}ms |\n")

    lines.append("\n## Per-Crop Details\n\n")
    for r in results:
        lines.append(f"### {r['file']}\n")
        if r["ground_truth"]:
            lines.append(f"**Ground truth**: `{r['ground_truth']}`\n\n")
        lines.append("| Filter | EasyOCR Output | Similarity | Time |\n")
        lines.append("|---|---|---|---|\n")
        for fname, fdata in r["filters"].items():
            sim_str = f"{fdata['similarity']:.0%}" if fdata["similarity"] is not None else "—"
            lines.append(f"| {fname} | `{fdata['text']}` | {sim_str} | {fdata['time_ms']:.0f}ms |\n")
        lines.append("\n")

    path.write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
