"""
collect_test_crops.py — Capture raw killfeed crops from live Twitch streams.

Connects to Apex Legends streams, detects the killfeed region, and saves
both the raw BGR crop and the current-pipeline preprocessed version for
each killfeed line.  Stops after collecting N crops.

Usage:
    python scratch/collect_test_crops.py [--channel faide] [--count 50]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import av
import cv2
import numpy as np

from config import *
from detect_killfeed import (
    detect_for_stream,
    detect_killfeed_from_frame,
    detect_content_x_bounds,
    open_stream,
    _get_frame_dimensions,
)
from detect_ranked import is_ranked_game
from ocr import preprocess, is_empty_line

# Output directory
OUTPUT_DIR = Path("test_crops")
RAW_DIR = OUTPUT_DIR / "raw"           # Original BGR crops
PROC_DIR = OUTPUT_DIR / "preprocessed"  # Current-pipeline preprocessed


def main():
    parser = argparse.ArgumentParser(description="Collect killfeed test crops")
    parser.add_argument("--channel", default=None,
                        help="Twitch username (default: auto-pick top stream)")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of crops to collect (default: 50)")
    parser.add_argument("--ranked-only", action="store_true", default=True,
                        help="Only capture from ranked games (default)")
    args = parser.parse_args()

    # Resolve channel
    if args.channel:
        username = args.channel.lower().strip()
    else:
        try:
            from twitch_api import get_top_apex_streams
            streams = get_top_apex_streams(5, ranked_only=True)
            if not streams:
                print("No live ranked Apex streams found.")
                return
            username = streams[0]
        except Exception as e:
            print(f"Could not fetch streams: {e}")
            print("Pass --channel explicitly.")
            return

    display_name = TWITCH_CHANNELS.get(username, username.title())
    search_zone = STREAMER_SEARCH_ZONES.get(display_name)
    print(f"Display name: {display_name}  zone: {'none/generic' if search_zone is None else display_name}")

    # Create output dirs
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROC_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to twitch.tv/{username}...")
    container, procs = open_stream(username)
    fw, fh = _get_frame_dimensions(container)
    print(f"Stream opened: {fw}x{fh}")

    # Detect content bounds (black bars)
    content_x0, content_x1 = 0, None
    for packet in container.demux(video=0):
        try:
            for frame in packet.decode():
                arr = frame.to_ndarray(format='bgra')
                content_x0, content_x1 = detect_content_x_bounds(arr)
                break
        except Exception:
            pass
        break

    content_w = (content_x1 or fw) - content_x0
    stretch_x = fw / content_w if content_w > 0 else 1.0
    stretch_y = fh / 1080.0

    if content_x0 > fw * 0.02:
        print(f"Black bars detected: game content x={content_x0}–{content_x1}")
    if abs(stretch_y - 1.0) > 0.05:
        print(f"Non-1080p stream: {fw}×{fh}, stretch_y={stretch_y:.3f}")

    # Phase 1: Detect killfeed regions
    print("Detecting killfeed regions...")
    attempt = 0
    killfeed_lines = []
    while attempt < 30:
        attempt += 1
        regions, n_read = detect_for_stream(
            container, fw, fh,
            content_x0=content_x0, content_x1=content_x1,
            search_zone=search_zone,
        )
        if len(regions) >= 1:
            killfeed_lines = regions
            coords = [(r['left'], r['top'], r['width'], r['height']) for r in regions]
            print(f"Detected {len(regions)} lines (attempt {attempt}): {coords}")
            break
        # Check ranked
        if args.ranked_only:
            for packet in container.demux(video=0):
                try:
                    for frame in packet.decode():
                        arr = frame.to_ndarray(format='bgra')
                        if not is_ranked_game(arr, arr.shape[1], arr.shape[0]):
                            print(f"  Not in ranked game (attempt {attempt})... waiting")
                            time.sleep(5)
                        break
                except Exception:
                    pass
                break

    if not killfeed_lines:
        print("Failed to detect killfeed. Exiting.")
        for p in procs:
            p.terminate()
        return

    # Phase 2: Capture crops
    print(f"\nCapturing up to {args.count} crops... (Ctrl+C to stop early)")
    saved = 0
    skipped_empty = 0
    frame_count = 0
    last_process = 0.0
    max_width = max(r["width"] for r in killfeed_lines)

    try:
        for packet in container.demux(video=0):
            if saved >= args.count:
                break
            try:
                frames = packet.decode()
            except Exception:
                continue

            for frame in frames:
                if saved >= args.count:
                    break

                now = time.time()
                if now - last_process < 0.5:
                    continue
                last_process = now
                frame_count += 1

                frame_bgra = frame.to_ndarray(format='bgra')
                fh_cur, fw_cur = frame_bgra.shape[:2]

                # Re-detect on this frame
                new_regions = detect_killfeed_from_frame(
                    frame_bgra, fw_cur, fh_cur,
                    content_x0=content_x0, content_x1=content_x1,
                    search_zone=search_zone,
                )
                if not new_regions:
                    skipped_empty += 1
                    continue

                killfeed_lines = new_regions

                for line_idx, line_crop in enumerate(killfeed_lines):
                    if saved >= args.count:
                        break

                    l = line_crop["left"]
                    t = line_crop["top"]
                    w = line_crop["width"]
                    h = line_crop["height"]

                    if t + h > fh_cur or l + w > fw_cur:
                        continue

                    # Extract raw crop (BGR)
                    _PAD = 2
                    t0 = max(0, t - _PAD)
                    t1 = min(fh_cur, t + h + _PAD)
                    # Horizontal padding — mirrors the fix in ocr.py's ChannelWorker: a
                    # single-frame width detection can catch a line before a trailing
                    # name/word has fully rendered, truncating the crop with no margin.
                    _PAD_X = 25
                    l0 = max(0, l - _PAD_X)
                    l1 = min(fw_cur, l + w + _PAD_X)
                    raw_img = frame_bgra[t0:t1, l0:l1]

                    # Check if line is empty
                    processed, _, _ = preprocess(
                        raw_img, stretch_x=stretch_x, stretch_y=stretch_y
                    )
                    if is_empty_line(processed):
                        skipped_empty += 1
                        continue

                    # Generate unique filename
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    fname = f"{username}_L{line_idx}_{ts}_{saved:04d}"

                    # Save raw BGR crop
                    bgr_raw = cv2.cvtColor(raw_img, cv2.COLOR_BGRA2BGR)
                    cv2.imwrite(str(RAW_DIR / f"{fname}.png"), bgr_raw)

                    # Save preprocessed crop (current pipeline)
                    cv2.imwrite(str(PROC_DIR / f"{fname}.png"), processed)

                    saved += 1
                    if saved % 10 == 0 or saved == 1:
                        print(f"  Saved {saved}/{args.count} crops "
                              f"(frames scanned: {frame_count}, "
                              f"empty: {skipped_empty})")

    except KeyboardInterrupt:
        print(f"\nStopped early.")

    finally:
        try:
            container.close()
        except Exception:
            pass
        for p in procs:
            p.terminate()

    print(f"\nDone! Saved {saved} crops.")
    print(f"  Raw crops:          {RAW_DIR}")
    print(f"  Preprocessed crops: {PROC_DIR}")
    print(f"  Frames scanned:     {frame_count}")
    print(f"  Empty lines skipped: {skipped_empty}")


if __name__ == "__main__":
    main()
