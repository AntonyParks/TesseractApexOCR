"""prepare_wordlevel_dataset.py -- Rebuild the EasyOCR fine-tune dataset at word-box granularity.

Fixes two confirmed train/inference mismatches from the imgW=512 run:
  1. Granularity: ocr_with_easyocr() recognizes each CRAFT-detected word box separately and
     inserts "<GUN_ICON>" via a Python-side gap heuristic -- the recognition net never sees a
     whole line or the literal marker at inference. Training on whole-line crops labeled with
     the literal "<GUN_ICON>" text taught the model a task it's never actually asked to do.
  2. Polarity: prepare_easyocr_lmdb.py copied raw crop files straight into the LMDB, but
     inference always runs preprocess_for_easyocr() first (grayscale, bitwise_not invert, 2x
     upscale, 15px white pad) before OCR. Training images were the opposite color polarity
     from what the model sees in production.

Both are fixed by construction here: every saved crop is taken from stock EasyOCR's own
detected word boxes on the *preprocessed* (inverted/upscaled/padded) image -- i.e. exactly the
pixels the recognition network receives at inference, at exactly the granularity it receives
them.

Blank/noise crops are deliberately NOT included: at inference a blank crop produces zero
detection boxes (or is filtered by is_empty_line() before OCR even runs), so the recognition
network is never invoked on a blank. Training it on full-frame blanks would reintroduce a
granularity mismatch of its own.

Usage:
    python prepare_wordlevel_dataset.py [--min-box-width 25] [--spot-check 8]
"""

import argparse
import csv
import statistics
from pathlib import Path

import cv2

import ocr as ocr_mod

LABELS_CSV = Path("labels/labels_clean.csv")
OUT_DIR = Path("wordlevel_crops")
OUT_CSV = Path("labels/labels_wordlevel.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-box-width", type=int, default=25,
                     help="Drop detected boxes narrower than this (filters stray-punctuation artifacts)")
    ap.add_argument("--spot-check", type=int, default=8,
                     help="Number of produced crops to print file paths for, for manual visual verification")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ocr_mod._easyocr_reader = None
    reader = ocr_mod._get_easyocr_reader()  # must be stock (no apex.pth deployed) -- verified by caller

    rows = []
    with LABELS_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["label"].strip() and Path(r["filepath"]).exists():
                rows.append(r)

    kept_lines = 0
    skipped_mismatch = 0
    skipped_no_boxes = 0
    kept_widths, filtered_widths = [], []
    out_rows = []  # (filename, label)
    idx = 0

    for row in rows:
        label = row["label"].replace("\n", " ").replace("\t", " ").strip()
        img = cv2.imread(row["filepath"])
        if img is None:
            continue
        processed, _, _ = ocr_mod.preprocess_for_easyocr(img)

        segments = [s.strip() for s in label.split("<GUN_ICON>")]
        segments = [s for s in segments if s]
        if not segments:
            continue

        horizontal_list_agg, _free_list_agg = reader.detect(processed)
        raw_boxes = horizontal_list_agg[0] if horizontal_list_agg else []

        boxes = []
        for b in raw_boxes:
            x_min, x_max, y_min, y_max = b
            width = x_max - x_min
            if width < args.min_box_width:
                filtered_widths.append(width)
                continue
            kept_widths.append(width)
            boxes.append(b)
        boxes.sort(key=lambda b: b[0])

        if not boxes:
            skipped_no_boxes += 1
            continue
        if len(boxes) != len(segments):
            skipped_mismatch += 1
            continue

        h, w = processed.shape[:2]
        for box, seg in zip(boxes, segments):
            x_min, x_max, y_min, y_max = box
            x_min, x_max = max(0, x_min), min(w, x_max)
            y_min, y_max = max(0, y_min), min(h, y_max)
            if x_max <= x_min or y_max <= y_min:
                continue
            crop = processed[y_min:y_max, x_min:x_max]
            fname = f"word_{idx:06d}.png"
            cv2.imwrite(str(OUT_DIR / fname), crop)
            out_rows.append((fname, seg))
            idx += 1
        kept_lines += 1

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "label"])
        for fname, lab in out_rows:
            writer.writerow([fname, lab])

    print(f"Source lines with non-empty label: {len(rows)}")
    print(f"  kept (box count matched segment count): {kept_lines}")
    print(f"  skipped (box/segment count mismatch):   {skipped_mismatch}")
    print(f"  skipped (zero boxes after width filter): {skipped_no_boxes}")
    if kept_widths:
        print(f"  kept box widths:     min={min(kept_widths)} median={statistics.median(kept_widths):.0f} max={max(kept_widths)}")
    if filtered_widths:
        print(f"  filtered box widths: min={min(filtered_widths)} median={statistics.median(filtered_widths):.0f} max={max(filtered_widths)} (n={len(filtered_widths)})")
    print(f"\nTotal word-level crops written: {len(out_rows)} -> {OUT_DIR}/")
    print(f"Labels written -> {OUT_CSV}")

    if args.spot_check and out_rows:
        print(f"\n--- Spot-check these {min(args.spot_check, len(out_rows))} crops manually before training ---")
        step = max(1, len(out_rows) // args.spot_check)
        for i in range(0, len(out_rows), step):
            fname, lab = out_rows[i]
            print(f"  {OUT_DIR / fname}  ->  {lab!r}")


if __name__ == "__main__":
    main()
