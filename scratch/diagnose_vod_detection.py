"""diagnose_vod_detection.py — Run killfeed detection against a Twitch VOD and save debug images.

Configured streamers aren't live right now, so this tests detection against recent VODs
of known-configured streamers (shivfps, apryze) instead, to check whether the region
detection bug seen on an unconfigured streamer ("keon") also affects configured ones.

Usage:
    python scratch/diagnose_vod_detection.py <vod_url> <label> [offset] [--zone NAME] [--no-zone]

    --zone NAME   Look up NAME in config.STREAMER_SEARCH_ZONES and use that search zone
                  instead of auto-resolving from the URL (useful when testing against an
                  unrelated VOD, e.g. a keon capture, under a specific streamer's zone).
    --no-zone     Force the generic global search box even if the VOD's channel name
                  matches a configured streamer — for explicit before/after comparison.
"""
import argparse
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import av
import numpy as np

from detect_killfeed import (
    _NonSeekablePipe, _streamlink_bin, detect_for_stream, _resolve_search_box,
    _get_frame_dimensions, _save_debug_images, detect_content_x_bounds,
)
from config import STREAM_QUALITY, TWITCH_CHANNELS, STREAMER_SEARCH_ZONES


def open_vod(url: str, quality: str = STREAM_QUALITY, offset: str = None):
    cmd = [_streamlink_bin(), "--stdout"]
    if offset:
        cmd += ["--hls-start-offset", offset]
    cmd += [url, quality]
    sl_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    ff_proc = subprocess.Popen(
        ["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1", "-loglevel", "error"],
        stdin=sl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    container = av.open(_NonSeekablePipe(ff_proc.stdout), format="mpegts")
    return container, [sl_proc, ff_proc]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Twitch VOD URL")
    parser.add_argument("label", help="Label used for output filenames")
    parser.add_argument("offset", nargs="?", default=None, help="streamlink --hls-start-offset, e.g. 0:45:00")
    parser.add_argument("--zone", metavar="NAME",
                         help="Look up NAME in STREAMER_SEARCH_ZONES instead of auto-resolving "
                              "from the URL's channel name")
    parser.add_argument("--no-zone", action="store_true",
                         help="Force the generic global search box even for a configured streamer")
    args = parser.parse_args()

    if args.zone:
        display_name = args.zone
    else:
        # Best-effort: pull the channel login out of a twitch.tv/<login>[/videos/...] URL
        login = args.url.rstrip("/").split("twitch.tv/")[-1].split("/")[0].lower()
        display_name = TWITCH_CHANNELS.get(login, login.title())
    search_zone = None if args.no_zone else STREAMER_SEARCH_ZONES.get(display_name)
    print(f"Display name: {display_name}  zone: {'none/generic' if search_zone is None else display_name}")

    print(f"Opening VOD: {args.url} (offset={args.offset})")
    container, procs = open_vod(args.url, offset=args.offset)
    try:
        fw, fh = _get_frame_dimensions(container)
        print(f"Frame size: {fw}x{fh}")

        preview_frame = None
        for packet in container.demux(video=0):
            try:
                for frame in packet.decode():
                    preview_frame = frame.to_ndarray(format="bgra")
                    break
                if preview_frame is not None:
                    break
            except Exception:
                continue

        content_x0, content_x1 = detect_content_x_bounds(preview_frame) if preview_frame is not None else (0, fw)
        print(f"Content bounds: {content_x0}-{content_x1} (frame width {fw})")

        regions, n_read, bmap = detect_for_stream(
            container, fw, fh, return_bmap=True, content_x0=content_x0, content_x1=content_x1,
            search_zone=search_zone,
        )
        print(f"Sampled {n_read} frames, detected {len(regions)} region(s):")
        for r in regions:
            print(f"  {r}")

        x0, y0, x1, y1 = _resolve_search_box(content_x0, content_x1, fh, search_zone)
        _save_debug_images(
            preview_frame, bmap, regions, (x0, y0, x1, y1),
            frame_path=f"scratch/debug_{args.label}_frame.png",
            bmap_path=f"scratch/debug_{args.label}_bmap.png",
        )
        if bmap is not None:
            np.save(f"scratch/debug_{args.label}_bmap.npy", bmap)
            print(f"Saved raw bmap -> scratch/debug_{args.label}_bmap.npy")
    finally:
        container.close()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
