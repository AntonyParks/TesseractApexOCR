"""Audit saved killfeed crops for two-line concatenation leaks (bead nqu).

A correctly-collected crop holds exactly ONE killfeed row. When two consecutive
[Bleed Out]-highlighted rows bridge the inter-row gap at the detection threshold,
the row-clusterer can merge them into one band and (before the _refine_regions
gap-aware backstop) leak a two-line crop. This tool measures how often that
happens by replaying the crop-level gap-aware splitter over saved crops -- the
same dk._force_split_tall_region(require_gap=True) the live detector now applies.

Usage:
    python tools/golden/crop_concat_audit.py                 # newest 4000 crops
    python tools/golden/crop_concat_audit.py --limit 0       # ALL crops (slow)
    python tools/golden/crop_concat_audit.py --since 20260717_163800   # crops saved after this stamp
    python tools/golden/crop_concat_audit.py --streamer Xlilfredson --examples 20

Exit code is non-zero when the two-line rate exceeds --max-rate (default 0.30%),
so it can gate a post-restart check: after the fix, freshly-captured crops should
trend toward ~0% while the historical corpus retains its ~0.8% backlog.
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import detect_killfeed as dk  # noqa: E402
from config import CROP_OUTPUT_DIR  # noqa: E402


def is_two_band(gray: np.ndarray) -> bool:
    """True if the crop splits into >=2 gap-separated text bands (a two-line leak)."""
    h, w = gray.shape
    bmap = (gray >= dk.DETECT_BRIGHTNESS_THRESH).astype(np.float32)
    gmap = (gray >= dk._GAP_TEXT_THRESH).astype(np.float32)
    reg = {"left": 0, "top": 0, "width": w, "height": h}
    subs = dk._force_split_tall_region(
        reg, bmap, 0, 0, dk._MAX_SINGLE_LINE_HEIGHT, require_gap=True, gap_map=gmap
    )
    return len(subs) >= 2


def _stamp(path: Path) -> str:
    # crops/<streamer>/YYYYMMDD_HHMMSS_lineN_hex4_raw.png -> "YYYYMMDD_HHMMSS"
    name = path.name
    return name[:15] if len(name) >= 15 else name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crops", default=str(CROP_OUTPUT_DIR), help="crops root dir")
    ap.add_argument("--streamer", default=None, help="restrict to one streamer subdir")
    ap.add_argument("--limit", type=int, default=4000, help="scan newest N crops (0 = all)")
    ap.add_argument("--since", default=None, help="only crops with stamp >= this YYYYMMDD_HHMMSS")
    ap.add_argument("--examples", type=int, default=15, help="how many offending crops to list")
    ap.add_argument("--max-rate", type=float, default=0.30, help="fail (exit 1) above this two-line %%")
    args = ap.parse_args()

    root = Path(args.crops)
    base = root / args.streamer if args.streamer else root
    files = list(base.glob("**/*_raw.png"))
    if args.since:
        files = [f for f in files if _stamp(f) >= args.since]
    files.sort(key=lambda p: p.name, reverse=True)  # newest first (stamp-prefixed names sort chronologically)
    if args.limit > 0:
        files = files[: args.limit]

    two = 0
    scanned = 0
    examples: list[tuple[str, tuple[int, int]]] = []
    for f in files:
        im = cv2.imread(str(f))
        if im is None:
            continue
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        scanned += 1
        if is_two_band(gray):
            two += 1
            if len(examples) < args.examples:
                examples.append((str(f.relative_to(root)), (im.shape[0], im.shape[1])))

    if scanned == 0:
        print("no crops scanned")
        return 0

    rate = 100.0 * two / scanned
    scope = f"newest {scanned}" if args.limit > 0 else f"all {scanned}"
    since = f" since {args.since}" if args.since else ""
    print(f"crop-concat audit ({scope}{since}): {two} two-line crops = {rate:.2f}%  (threshold {args.max_rate:.2f}%)")
    for name, (h, w) in examples:
        print(f"  {h:>3}x{w:<4} {name}")

    return 1 if rate > args.max_rate else 0


if __name__ == "__main__":
    sys.exit(main())
