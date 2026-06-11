"""Detect whether the current Apex Legends frame is from a ranked match.

The ranked badge (Rookie / Bronze / Silver / Gold / Platinum / Diamond /
Master / Predator shield icon) appears in the top-right corner of the HUD
during ranked matches.  During pubs / mixtape the badge is absent and that
region shows plain game background.

We detect ranked mode by looking for a cluster of high-saturation pixels
in the badge region — rank icons have vivid colours (bronze, teal, blue,
purple, red) that are absent when the slot is empty.

Standalone debug usage:
    python detect_ranked.py faide
    python detect_ranked.py faide --save-frame debug_ranked.png
    python detect_ranked.py faide --frames 5
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from config import STREAM_QUALITY

# ---------------------------------------------------------------------------
# Search region (fractions of frame dimensions)
# The rank badge sits in the top-right corner, above the killfeed zone.
# Killfeed starts at ~28 % height; badge is well above that.
# ---------------------------------------------------------------------------
_BADGE_X_FRAC  = 0.88   # search from this fraction of width to right edge
_BADGE_Y0_FRAC = 0.02   # top of search region
_BADGE_Y1_FRAC = 0.18   # bottom of search region (well above killfeed at ~28 %)

# A pixel counts as "badge-coloured" when HSV saturation > _MIN_SAT AND
# HSV value (brightness) > _MIN_VAL.  Both in 0-255 range.
_MIN_SAT = 80
_MIN_VAL = 80

# Default: at least 5 % of the badge region must be coloured pixels.
# Can be overridden via config.py RANKED_MIN_SAT_FRAC.
try:
    from config import RANKED_MIN_SAT_FRAC as _DEFAULT_MIN_FRAC
except ImportError:
    _DEFAULT_MIN_FRAC = 0.05


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def is_ranked_game(
    frame_bgra: np.ndarray,
    frame_w: int,
    frame_h: int,
    min_frac: float | None = None,
) -> bool:
    """Return True if *frame_bgra* appears to be from a ranked match.

    Args:
        frame_bgra: Full frame as a BGRA uint8 array.
        frame_w, frame_h: Frame dimensions in pixels.
        min_frac: Override for the minimum saturated-pixel fraction.
    """
    if min_frac is None:
        min_frac = _DEFAULT_MIN_FRAC

    x0 = int(frame_w * _BADGE_X_FRAC)
    y0 = int(frame_h * _BADGE_Y0_FRAC)
    y1 = int(frame_h * _BADGE_Y1_FRAC)

    region = frame_bgra[y0:y1, x0:frame_w]
    if region.size == 0:
        return False

    bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colored = int(np.sum((sat > _MIN_SAT) & (val > _MIN_VAL)))
    total   = region.shape[0] * region.shape[1]

    return (colored / total) >= min_frac


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Detect whether a live Twitch Apex stream is playing ranked"
    )
    parser.add_argument("channel", help="Twitch username or full URL")
    parser.add_argument("--frames", type=int, default=5,
                        help="Number of frames to sample (default 5)")
    parser.add_argument("--save-frame", metavar="PATH",
                        help="Save the badge-region crop of the first frame to PNG")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_MIN_FRAC,
                        help=f"Min saturated-pixel fraction (default {_DEFAULT_MIN_FRAC})")
    args = parser.parse_args()

    from detect_killfeed import open_stream, _get_frame_dimensions
    import av

    username = args.channel.rstrip("/").split("/")[-1].lower()
    print(f"Opening stream for '{username}'...")

    container, procs = open_stream(username)
    try:
        fw, fh = _get_frame_dimensions(container)
        print(f"Frame size: {fw}x{fh}")
        print(f"Badge search region: x=[{int(fw*_BADGE_X_FRAC)},{fw}]  "
              f"y=[{int(fh*_BADGE_Y0_FRAC)},{int(fh*_BADGE_Y1_FRAC)}]")

        frames_checked = 0
        ranked_count   = 0

        for packet in container.demux(video=0):
            if frames_checked >= args.frames:
                break
            try:
                decoded = packet.decode()
            except Exception:
                continue

            for frame in decoded:
                arr = frame.to_ndarray(format='bgra')
                fh_cur, fw_cur = arr.shape[:2]

                x0 = int(fw_cur * _BADGE_X_FRAC)
                y0 = int(fh_cur * _BADGE_Y0_FRAC)
                y1 = int(fh_cur * _BADGE_Y1_FRAC)
                region = arr[y0:y1, x0:fw_cur]

                # Compute saturation fraction for display
                bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                colored = int(np.sum(
                    (hsv[:, :, 1] > _MIN_SAT) & (hsv[:, :, 2] > _MIN_VAL)
                ))
                total = region.shape[0] * region.shape[1]
                frac  = colored / total if total > 0 else 0.0

                ranked = frac >= args.threshold
                if ranked:
                    ranked_count += 1

                print(f"  Frame {frames_checked+1}: sat_frac={frac:.3f}  -> "
                      f"{'RANKED' if ranked else 'not ranked'}")

                if args.save_frame and frames_checked == 0:
                    save_path = args.save_frame
                    bgr_region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
                    cv2.imwrite(save_path, bgr_region)
                    print(f"  Saved badge region to {save_path}")

                frames_checked += 1
                break  # one frame per packet

        print(f"\nResult: {ranked_count}/{frames_checked} frames detected as ranked")
        if ranked_count == 0:
            print("Tip: stream may be in pubs/mixtape, lobby, or loading screen.")
            print(f"     Try lowering --threshold (current: {args.threshold})")
        elif ranked_count < frames_checked:
            print("Tip: mixed result — streamer may be between games.")

    finally:
        container.close()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
