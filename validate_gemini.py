"""Validate gemini_validator.py against real crops.

Usage:
    python validate_gemini.py                      # 20 random crops from all streamers
    python validate_gemini.py --n 50               # 50 random crops
    python validate_gemini.py --streamer Faide     # restrict to one streamer
    python validate_gemini.py --quality high       # only crops already labeled 'high'
    python validate_gemini.py --compare            # compare Gemini vs existing labels_clean.csv labels
"""

import argparse
import csv
import random
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from config import GEMINI_AGREE_THRESHOLD
from database import PlayerDatabase
from gemini_validator import validate_killfeed_crop
from parsers import parse_killfeed_line

CROPS_DIR  = Path("crops")
LABELS_CSV = Path("labels/labels_clean.csv")


def _load_crop(path: Path) -> np.ndarray | None:
    """Load a PNG crop as a grayscale numpy array."""
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return img


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _collect_crops(streamer: str | None, quality_filter: str | None) -> list[Path]:
    """Return list of crop paths, optionally filtered by streamer or quality tier."""
    if quality_filter and LABELS_CSV.exists():
        # Load paths from labels_clean.csv filtered by quality
        paths = []
        with LABELS_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("quality") != quality_filter:
                    continue
                p = Path(row["filepath"])
                if not p.exists():
                    continue
                if streamer and streamer.lower() not in str(p).lower():
                    continue
                paths.append(p)
        return paths

    # Default: glob crops directory
    pattern = f"{streamer}/**/*.png" if streamer else "**/*.png"
    return list(CROPS_DIR.glob(pattern))


def run_validation(n: int, streamer: str | None, quality_filter: str | None,
                   compare: bool, db: PlayerDatabase):
    all_crops = _collect_crops(streamer, quality_filter)
    if not all_crops:
        print("No crops found. Check crops/ directory or --streamer name.")
        return

    # Load existing labels for --compare mode
    existing_labels: dict[str, str] = {}
    if compare and LABELS_CSV.exists():
        with LABELS_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_labels[row["filepath"]] = row["label"]

    sample = random.sample(all_crops, min(n, len(all_crops)))
    print(f"Sampling {len(sample)} crops from {len(all_crops)} available\n")

    total = len(sample)
    valid = 0
    rejected = 0
    kill_events = 0
    exact_matches = 0
    partial_matches = 0
    disagreements = 0

    for i, path in enumerate(sample, 1):
        print(f"[{i}/{total}] {path}")

        crop = _load_crop(path)
        if crop is None:
            print("  ERROR: could not load image\n")
            rejected += 1
            continue

        t0 = time.time()
        gemini_text = validate_killfeed_crop(crop)
        elapsed = time.time() - t0

        if gemini_text is None:
            print(f"  Gemini:  EMPTY / rejected  ({elapsed:.1f}s)\n")
            rejected += 1
            continue

        valid += 1
        parsed = parse_killfeed_line(gemini_text, db, time.time())
        evt = parsed.get("event_type") or "—"
        atk = parsed.get("attacker") or "—"
        vic = parsed.get("victim") or "—"
        a_conf = parsed.get("attacker_conf", 0.0)
        v_conf = parsed.get("victim_conf", 0.0)

        print(f"  Gemini:  {gemini_text!r}  ({elapsed:.1f}s)")
        print(f"  Parsed: event={evt}  atk={atk} ({a_conf:.2f})  vic={vic} ({v_conf:.2f})")

        if evt == "Kill":
            kill_events += 1

        # --compare: show existing label vs Gemini
        if compare:
            existing = existing_labels.get(str(path))
            if existing:
                sim = _similarity(gemini_text, existing)
                if sim >= 0.99:
                    match_str = "EXACT"
                    exact_matches += 1
                elif sim >= 0.80:
                    match_str = f"PARTIAL ({sim:.2f})"
                    partial_matches += 1
                else:
                    match_str = f"DISAGREE ({sim:.2f})"
                    disagreements += 1
                print(f"  Existing label: {existing!r}")
                print(f"  Match:          {match_str}")
            else:
                print("  Existing label: (none in labels_clean.csv)")

        print()
        if i < total:
            time.sleep(5.5)

    # Summary
    print(f"{'='*60}")
    print(f"Summary ({total} sampled)")
    print(f"{'='*60}")
    print(f"Valid responses:    {valid:3d} / {total}  ({100*valid/total:.0f}%)")
    print(f"EMPTY / rejected:   {rejected:3d} / {total}  ({100*rejected/total:.0f}%)")
    if valid:
        print(f"Kill events:        {kill_events:3d} / {valid}  ({100*kill_events/valid:.0f}% of valid)")
    if compare and (exact_matches + partial_matches + disagreements):
        compared = exact_matches + partial_matches + disagreements
        print(f"\nComparison vs labels_clean.csv ({compared} matched):")
        print(f"  Exact match:    {exact_matches:3d}  ({100*exact_matches/compared:.0f}%)")
        print(f"  Partial (>=0.8):{partial_matches:3d}  ({100*partial_matches/compared:.0f}%)")
        print(f"  Disagreement:   {disagreements:3d}  ({100*disagreements/compared:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Validate gemini_validator against real crops")
    parser.add_argument("--n", type=int, default=20, help="Number of crops to sample")
    parser.add_argument("--streamer", type=str, default=None, help="Filter to one streamer")
    parser.add_argument("--quality", type=str, default=None,
                        choices=["high", "medium", "low"],
                        help="Only sample crops with this quality tier from labels_clean.csv")
    parser.add_argument("--compare", action="store_true",
                        help="Compare Gemini output vs existing labels_clean.csv labels")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    db = PlayerDatabase()
    db.load_databases()

    run_validation(
        n=args.n,
        streamer=args.streamer,
        quality_filter=args.quality,
        compare=args.compare,
        db=db,
    )


if __name__ == "__main__":
    main()
