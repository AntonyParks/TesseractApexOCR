"""Auto-detect Apex killfeed line regions from a Twitch stream.

The killfeed is always white text in the top-right corner of the frame.
We sample N frames, accumulate an average brightness map in the search
region, then find horizontal text bands via row/column projections.

Standalone usage (debug tool):
    python detect_killfeed.py faide
    python detect_killfeed.py https://twitch.tv/faide
    python detect_killfeed.py faide --preview   # CV2 window with bounding boxes
"""

import argparse
import io
import subprocess
import sys
from pathlib import Path

import av
import cv2
import numpy as np

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from config import DETECT_N_FRAMES, DETECT_BRIGHTNESS_THRESH, DETECT_MIN_LINES, STREAM_QUALITY


class _NonSeekablePipe(io.RawIOBase):
    """Wraps a subprocess stdout pipe and reports seekable()=False.

    On Windows, subprocess pipes may incorrectly claim to be seekable,
    causing PyAV to attempt a seek and fail with EINVAL.
    """
    def __init__(self, pipe):
        self._pipe = pipe

    def read(self, n=-1):
        return self._pipe.read(n)

    def readinto(self, b):
        data = self._pipe.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def readable(self):
        return True

    def seekable(self):
        return False

    def writable(self):
        return False


def _streamlink_bin() -> str:
    """Return path to the streamlink executable in the active Python environment."""
    scripts = Path(sys.executable).parent
    for name in ("streamlink.exe", "streamlink"):
        p = scripts / name
        if p.exists():
            return str(p)
    return "streamlink"  # fall back to PATH


def open_stream(username: str, quality: str | None = None
                ) -> tuple[av.container.Container, list[subprocess.Popen]]:
    """Open a Twitch stream: streamlink → ffmpeg (fMP4→MPEG-TS) → PyAV container.

    The ffmpeg remux step normalises Twitch's fragmented-MP4 HLS output into
    a clean MPEG-TS byte stream that PyAV can decode across segment boundaries.

    Returns (container, [sl_proc, ff_proc]).
    Caller must call proc.terminate() on each returned process when done.
    """
    quality = quality or STREAM_QUALITY
    sl_proc = subprocess.Popen(
        [_streamlink_bin(), "--stdout",
         f"https://www.twitch.tv/{username}", quality],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    ff_proc = subprocess.Popen(
        ["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1",
         "-loglevel", "error"],
        stdin=sl_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    container = av.open(_NonSeekablePipe(ff_proc.stdout), format='mpegts')
    return container, [sl_proc, ff_proc]

# Search region as fractions of frame dimensions.
# Known killfeed left-edges: HisWattson/ShivFPS ~75-76%, noko observed at 74.2%.
# Starting at 70% gives generous headroom for new streamers while still excluding
# most non-killfeed HUD (which generally occupies the left ~60% of the frame).
# Y0 set to 0.19 so the first killfeed line (seen as low as 21% on 936p streams) is not clipped.
# Known killfeed top range: 28.6%-41.8% of 1080p height.
_SEARCH_X_FRAC  = 0.70   # search from this fraction of width to right edge
_SEARCH_Y0_FRAC = 0.19   # search from this fraction of height (generous top margin)
_SEARCH_Y1_FRAC = 0.52   # search up to this fraction of height

# Detection thresholds
_MIN_ROW_BRIGHT  = 4.0   # avg bright pixels per row to count as "has text"
_ROW_GAP_TOL     = 6     # max gap (rows) between active rows in same line cluster
_MIN_LINE_HEIGHT = 15    # pixels
_MAX_LINE_HEIGHT = 55    # pixels
_MIN_LINE_WIDTH  = 80    # pixels  (filters small badges/icons)
# Full-width banners ("KILL LEADER", "CHAMPION SQUAD", etc.) span nearly the entire search box.
# Killfeed lines are at most ~50% of frame width wide; anything wider is likely a banner.
_MAX_LINE_WIDTH_FRAC = 0.50  # max line width as fraction of frame width
# Column threshold: require a column to be bright in ~15% of frames.
# At 60fps, 60 sampled frames ~= 1s; 15% means bright in >=9 frames (~0.15s of visibility).
# Low enough to catch brief kills; high enough to suppress random single-frame noise.
_MIN_COL_BRIGHT  = 0.15  # avg bright pixels per column to count as "in bounds"
_REFINE_GAP      = 2     # stricter gap tolerance for within-region sub-band check
# Killfeed lines are always near the TOP of the search region.
# Webcams, lower HUD, and other non-killfeed elements appear in the bottom portion.
# Reject any candidate whose top is in the lowest 20% of the search box.
# Verified: known killfeed last-lines sit at <=66% of the search height; webcam was at 89%.
_MAX_LINE_TOP_FRAC = 0.80  # fraction of search box height; reject lines starting below this


# ---------------------------------------------------------------------------
# Core detection functions
# ---------------------------------------------------------------------------

def _build_brightness_map(
    container: av.container.Container,
    search_box: tuple[int, int, int, int],
    n_frames: int,
    thresh: int,
) -> tuple[np.ndarray | None, int]:
    """Read up to *n_frames* video frames and return an averaged binary brightness map.

    Args:
        container:   Open PyAV container (stream already started).
        search_box:  (x0, y0, x1, y1) in absolute pixel coords.
        n_frames:    Max frames to sample.
        thresh:      Grayscale threshold to count a pixel as "bright text".

    Returns:
        (brightness_map, frames_read)
        brightness_map: float32 array shape (y1-y0, x1-x0), avg bright pixels; None if 0 frames.
    """
    x0, y0, x1, y1 = search_box
    box_h = y1 - y0
    box_w = x1 - x0

    accum = np.zeros((box_h, box_w), dtype=np.float32)
    frames_read = 0

    for packet in container.demux(video=0):
        if frames_read >= n_frames:
            break
        try:
            decoded = packet.decode()
        except Exception:
            continue

        for frame in decoded:
            arr = frame.to_ndarray(format='bgra')
            fh, fw = arr.shape[:2]

            # Clamp box to actual frame dimensions
            ax0, ay0 = min(x0, fw), min(y0, fh)
            ax1, ay1 = min(x1, fw), min(y1, fh)
            if ax1 <= ax0 or ay1 <= ay0:
                break

            crop = arr[ay0:ay1, ax0:ax1]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGRA2GRAY)
            _, bright = cv2.threshold(gray, thresh, 1, cv2.THRESH_BINARY)

            # Pad back to expected size if frame was smaller
            pad_h = box_h - bright.shape[0]
            pad_w = box_w - bright.shape[1]
            if pad_h > 0 or pad_w > 0:
                bright = np.pad(bright, ((0, pad_h), (0, pad_w)))

            accum += bright.astype(np.float32)
            frames_read += 1
            break  # one frame per packet is enough

    if frames_read == 0:
        return None, 0

    return accum / frames_read, frames_read


def detect_killfeed_regions(
    brightness_map: np.ndarray,
    origin_x: int,
    origin_y: int,
    frame_w: int = 0,
) -> list[dict]:
    """Find killfeed line bounding boxes from an averaged brightness map.

    Args:
        brightness_map: float32 array (h, w) — avg bright pixels per cell.
        origin_x:       Absolute x offset of the map's left edge in the frame.
        origin_y:       Absolute y offset of the map's top edge in the frame.
        frame_w:        Full frame width — used to filter oversized banner regions.

    Returns:
        List of {"left", "top", "width", "height"} dicts, top-to-bottom order.
    """
    max_line_w   = int(frame_w * _MAX_LINE_WIDTH_FRAC) if frame_w > 0 else 0
    search_h     = brightness_map.shape[0]
    max_top_local = int(search_h * _MAX_LINE_TOP_FRAC)  # reject lines starting below this row

    # --- Row projection ---
    row_proj = brightness_map.sum(axis=1)
    active_rows = np.where(row_proj >= _MIN_ROW_BRIGHT)[0]
    if len(active_rows) == 0:
        return []

    # --- Cluster consecutive active rows ---
    clusters: list[tuple[int, int]] = []
    start = int(active_rows[0])
    prev  = int(active_rows[0])
    for r in active_rows[1:]:
        r = int(r)
        if r - prev > _ROW_GAP_TOL:
            clusters.append((start, prev))
            start = r
        prev = r
    clusters.append((start, prev))

    # --- Build region dicts ---
    regions = []
    for r_start, r_end in clusters:
        # Reject clusters that start too far down — likely webcam, lower HUD, not killfeed
        if r_start > max_top_local:
            continue

        h = r_end - r_start + 1
        if not (_MIN_LINE_HEIGHT <= h <= _MAX_LINE_HEIGHT):
            continue

        # Column projection in this row band
        band = brightness_map[r_start:r_end + 1, :]
        col_proj = band.sum(axis=0)
        bright_cols = np.where(col_proj >= _MIN_COL_BRIGHT)[0]
        if len(bright_cols) < _MIN_LINE_WIDTH:
            continue

        left  = origin_x + int(bright_cols[0])
        width = int(bright_cols[-1]) - int(bright_cols[0]) + 1
        top   = origin_y + r_start

        # Reject full-width banners ("KILL LEADER", "CHAMPION SQUAD", etc.)
        if max_line_w > 0 and width > max_line_w:
            continue

        # Reject lines spanning nearly the full search box width — these are noise/webcam.
        # Killfeed text never fills the entire search region; a span >= 95% of box is bogus.
        box_w = brightness_map.shape[1]
        if width >= int(box_w * 0.95):
            continue

        regions.append({"left": left, "top": top, "width": width, "height": h})

    return regions


def _refine_regions(
    regions: list[dict],
    brightness_map: np.ndarray,
    origin_x: int,
    origin_y: int,
    verbose: bool = True,
) -> list[dict]:
    """Split any merged regions that contain two distinct text sub-bands.

    The initial row-clustering uses _ROW_GAP_TOL=6, which can merge two
    closely-spaced killfeed lines into a single region.  This pass re-checks
    each region with a stricter gap (_REFINE_GAP=3) and splits it if two
    sub-bands are found.
    """
    refined: list[dict] = []

    for region in regions:
        local_top = region["top"] - origin_y
        local_bot = local_top + region["height"] - 1

        band     = brightness_map[local_top : local_bot + 1, :]
        row_proj = band.sum(axis=1)
        active   = np.where(row_proj >= _MIN_ROW_BRIGHT)[0]

        if len(active) == 0:
            refined.append(region)
            continue

        # Cluster with stricter gap to expose internal line separation
        sub_clusters: list[tuple[int, int]] = []
        s = int(active[0]); p = int(active[0])
        for r in active[1:]:
            r = int(r)
            if r - p > _REFINE_GAP:
                sub_clusters.append((s, p))
                s = r
            p = r
        sub_clusters.append((s, p))

        if len(sub_clusters) <= 1:
            refined.append(region)
            continue

        # Two or more sub-bands found — split into separate regions
        if verbose:
            print(f"[REFINE] Region top={region['top']} h={region['height']} "
                  f"-> {len(sub_clusters)} sub-bands; splitting")

        for sc_start, sc_end in sub_clusters:
            sub_h = sc_end - sc_start + 1
            if sub_h < _MIN_LINE_HEIGHT:
                continue

            abs_r0 = local_top + sc_start
            abs_r1 = local_top + sc_end
            sub_band    = brightness_map[abs_r0 : abs_r1 + 1, :]
            col_proj    = sub_band.sum(axis=0)
            bright_cols = np.where(col_proj >= _MIN_COL_BRIGHT)[0]

            if len(bright_cols) < _MIN_LINE_WIDTH:
                continue

            refined.append({
                "left":   origin_x + int(bright_cols[0]),
                "top":    origin_y + abs_r0,
                "width":  int(bright_cols[-1]) - int(bright_cols[0]) + 1,
                "height": sub_h,
            })

    return refined


def detect_for_stream(
    container: av.container.Container,
    frame_w: int,
    frame_h: int,
    n_frames: int | None = None,
    thresh: int | None = None,
    return_bmap: bool = False,
) -> tuple[list[dict], int] | tuple[list[dict], int, object]:
    """High-level wrapper: define search box, build map, find regions.

    Args:
        container:   Open PyAV container.
        frame_w, frame_h: Stream frame dimensions.
        n_frames:    Override DETECT_N_FRAMES.
        thresh:      Override DETECT_BRIGHTNESS_THRESH.
        return_bmap: If True, return (regions, n_read, bmap) instead of (regions, n_read).

    Returns:
        (regions, frames_read) or (regions, frames_read, bmap) when return_bmap=True.
    """
    n_frames = n_frames or DETECT_N_FRAMES
    thresh   = thresh   or DETECT_BRIGHTNESS_THRESH

    x0 = int(frame_w * _SEARCH_X_FRAC)
    y0 = int(frame_h * _SEARCH_Y0_FRAC)
    x1 = frame_w
    y1 = int(frame_h * _SEARCH_Y1_FRAC)

    bmap, n_read = _build_brightness_map(container, (x0, y0, x1, y1), n_frames, thresh)
    if bmap is None:
        return ([], 0, None) if return_bmap else ([], 0)

    regions = detect_killfeed_regions(bmap, x0, y0, frame_w=frame_w)
    regions = _refine_regions(regions, bmap, x0, y0)
    return (regions, n_read, bmap) if return_bmap else (regions, n_read)


def detect_killfeed_from_frame(
    frame_bgra,
    frame_w: int,
    frame_h: int,
    thresh: int | None = None,
) -> list[dict]:
    """Single-frame killfeed detection for use inside the OCR loop.

    Runs the same region-finding logic as detect_for_stream() but on one already-decoded
    frame instead of averaging over N frames.  Cost: ~1 ms vs 50–200 ms for OCR.

    Returns [] if no valid regions found — caller should fall back to last known good.
    """
    thresh = thresh or DETECT_BRIGHTNESS_THRESH
    x0 = int(frame_w * _SEARCH_X_FRAC)
    y0 = int(frame_h * _SEARCH_Y0_FRAC)
    x1 = frame_w
    y1 = int(frame_h * _SEARCH_Y1_FRAC)

    region = frame_bgra[y0:y1, x0:x1]
    gray   = cv2.cvtColor(region, cv2.COLOR_BGRA2GRAY)
    bmap   = (gray >= thresh).astype(np.float32)

    regions = detect_killfeed_regions(bmap, x0, y0, frame_w=frame_w)
    regions = _refine_regions(regions, bmap, x0, y0, verbose=False)
    return regions


def _save_debug_images(
    frame_bgra,
    bmap,
    regions: list[dict],
    search_box: tuple[int, int, int, int],
    frame_path: str = "debug_frame.png",
    bmap_path: str  = "debug_bmap.png",
) -> None:
    """Save debug PNGs and print a stat summary for diagnosing detection failures."""
    import numpy as np

    x0, y0, x1, y1 = search_box
    box_w = x1 - x0
    box_h = y1 - y0

    # ── stat summary ──────────────────────────────────────────────────────────
    print(f"[DEBUG] Search box: x={x0}–{x1}  y={y0}–{y1}  ({box_w}×{box_h} px)")
    if bmap is not None:
        row_proj  = bmap.sum(axis=1)
        max_row   = float(row_proj.max()) if row_proj.size else 0.0
        active    = int((row_proj >= _MIN_ROW_BRIGHT).sum())
        print(f"[DEBUG] Brightness map shape: {bmap.shape}")
        print(f"[DEBUG] Max row brightness: {max_row:.1f}  (threshold: {_MIN_ROW_BRIGHT})")
        print(f"[DEBUG] Active rows: {active}")
    print(f"[DEBUG] Detected regions: {len(regions)}")

    # ── annotated frame ───────────────────────────────────────────────────────
    if frame_bgra is not None:
        display = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR)
        # Search box — red
        cv2.rectangle(display, (x0, y0), (x1 - 1, y1), (0, 0, 255), 1)
        # Active rows — yellow horizontal lines in search box
        if bmap is not None:
            row_proj = bmap.sum(axis=1)
            for ry, val in enumerate(row_proj):
                if val >= _MIN_ROW_BRIGHT:
                    fy = y0 + ry
                    cv2.line(display, (x0, fy), (x1 - 1, fy), (0, 255, 255), 1)
        # Detected regions — green
        for r in regions:
            cv2.rectangle(
                display,
                (r["left"], r["top"]),
                (r["left"] + r["width"], r["top"] + r["height"]),
                (0, 255, 0), 2,
            )
        cv2.imwrite(frame_path, display)
        print(f"[DEBUG] Saved annotated frame → {frame_path}")

    # ── brightness map as grayscale PNG ───────────────────────────────────────
    if bmap is not None:
        bmax = bmap.max()
        if bmax > 0:
            bmap_vis = (bmap / bmax * 255).astype(np.uint8)
        else:
            bmap_vis = np.zeros(bmap.shape, dtype=np.uint8)
        cv2.imwrite(bmap_path, bmap_vis)
        print(f"[DEBUG] Saved brightness map  → {bmap_path}")


def _get_frame_dimensions(container: av.container.Container) -> tuple[int, int]:
    """Get frame dimensions from stream metadata (no frames consumed)."""
    try:
        vs = container.streams.video[0]
        if vs.width and vs.height:
            return vs.width, vs.height
    except (IndexError, AttributeError):
        pass
    return 1920, 1080  # safe default


# ---------------------------------------------------------------------------
# Standalone CLI tool
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Detect Apex killfeed regions from a live Twitch stream"
    )
    parser.add_argument("channel", help="Twitch username or full URL")
    parser.add_argument(
        "--preview", action="store_true",
        help="Show a CV2 window with detected bounding boxes overlaid on a sample frame"
    )
    parser.add_argument("--frames", type=int, default=DETECT_N_FRAMES,
                        help=f"Frames to sample (default {DETECT_N_FRAMES})")
    parser.add_argument("--save-frame", metavar="PATH",
                        help="Save the search-region crop of the first frame to a PNG for inspection")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug_frame.png + debug_bmap.png and print brightness stats")
    args = parser.parse_args()

    username = args.channel.rstrip("/").split("/")[-1].lower()
    print(f"Opening stream for '{username}'...")

    container, procs = open_stream(username)

    try:
        fw, fh = _get_frame_dimensions(container)
        print(f"Frame size: {fw}×{fh}")
        print(f"Sampling {args.frames} frames for detection...")

        # Grab one frame for preview/save/debug before detection consumes frames
        preview_frame = None
        if args.preview or args.save_frame or args.debug:
            for packet in container.demux(video=0):
                try:
                    for frame in packet.decode():
                        preview_frame = frame.to_ndarray(format='bgra')
                        break
                    if preview_frame is not None:
                        break
                except Exception:
                    continue

        # Save search-region crop for visual inspection
        if args.save_frame and preview_frame is not None:
            x0s = int(fw * _SEARCH_X_FRAC)
            y0s = int(fh * _SEARCH_Y0_FRAC)
            y1s = int(fh * _SEARCH_Y1_FRAC)
            crop = cv2.cvtColor(preview_frame[y0s:y1s, x0s:fw], cv2.COLOR_BGRA2BGR)
            cv2.imwrite(args.save_frame, crop)
            print(f"Saved search region ({x0s},{y0s})-({fw},{y1s}) to {args.save_frame}")

        regions, n_read, bmap = detect_for_stream(
            container, fw, fh, n_frames=args.frames, return_bmap=True
        )

        print(f"\nSampled {n_read} frames.")
        if not regions:
            print("No killfeed lines detected. Is there active gameplay on the stream?")
        else:
            print(f"Detected {len(regions)} killfeed line(s):")
            for i, r in enumerate(regions):
                print(f"  Line {i}: left={r['left']}  top={r['top']}  "
                      f"width={r['width']}  height={r['height']}")

        x0 = int(fw * _SEARCH_X_FRAC)
        y0 = int(fh * _SEARCH_Y0_FRAC)
        y1 = int(fh * _SEARCH_Y1_FRAC)

        if args.debug:
            _save_debug_images(preview_frame, bmap, regions, (x0, y0, fw, y1))

        if args.preview and preview_frame is not None:
            display = cv2.cvtColor(preview_frame, cv2.COLOR_BGRA2BGR)
            for r in regions:
                cv2.rectangle(
                    display,
                    (r['left'], r['top']),
                    (r['left'] + r['width'], r['top'] + r['height']),
                    (0, 255, 0), 2
                )
            cv2.rectangle(display, (x0, y0), (fw - 1, y1), (0, 0, 255), 1)

            win = f"Killfeed Detection — {username}"
            cv2.namedWindow(win, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win, min(fw, 1280), int(min(fw, 1280) * fh / fw))
            cv2.imshow(win, display)
            print("\nPress any key in the preview window to close.")
            cv2.waitKey(0)
            cv2.destroyAllWindows()

    finally:
        container.close()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
