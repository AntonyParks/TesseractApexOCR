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

from config import (
    DETECT_N_FRAMES, DETECT_BRIGHTNESS_THRESH, DETECT_MIN_LINES, STREAM_QUALITY,
    TWITCH_CHANNELS, STREAMER_SEARCH_ZONES,
)


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

# Search region as fractions of frame dimensions. This is the FALLBACK box used only for
# unconfigured/ad-hoc streamers with no STREAMER_SEARCH_ZONES entry (see config.py) — known
# configured streamers use a much tighter, per-streamer zone instead.
# Known killfeed left-edges: HisWattson/ShivFPS ~75-76%, noko observed at 74.2%.
# Starting at 70% gives generous headroom for new streamers while still excluding
# most non-killfeed HUD (which generally occupies the left ~60% of the frame).
# Y0 set to 0.19 so the first killfeed line (seen as low as 21% on 936p streams) is not clipped.
# NOTE: the killfeed top range historically cited here as "28.6%-41.8% of 1080p height" was
# derived from pixel data actually calibrated at 2560x1440 (see STREAMER_KILLFEED_CONFIGS in
# config.py) and misinterpreted as an 1080p fraction. The corrected range (dividing by 1440,
# not 1080) is closer to 20%-31% — verified 2026-07-01 against a fresh 1080p Apryze VOD.
_SEARCH_X_FRAC  = 0.70   # search from this fraction of width to right edge
_SEARCH_Y0_FRAC = 0.19   # search from this fraction of height (generous top margin)
_SEARCH_Y1_FRAC = 0.52   # search up to this fraction of height

# Fixed Apex HUD elements that are part of the game's own UI (not a streamer's OBS overlay),
# so their position is universal across all streamers/resolutions as a fraction of frame size.
# Zeroed out of the brightness map before line detection runs — defense-in-depth against
# false-positive detections, most relevant for unconfigured streamers using the generic box
# above (a per-streamer STREAMER_SEARCH_ZONES entry, once tightened, already excludes most of
# this by virtue of starting further down). Currently just the squad-counter row + kill-leader
# badge icon (top-right, "N SQUADS LEFT" + player count + emblem) — present in every match,
# unlike the optional/toggleable FPS-network-diagnostic overlay, which is NOT included here
# since its presence/position isn't confirmed stable across all configured streamers.
_HUD_EXCLUDE_ZONES: list[tuple[float, float, float, float]] = [
    # (x0_frac, y0_frac, x1_frac, y1_frac) — squad-counter + kill-leader badge, top-right.
    (0.80, 0.0, 1.0, 0.11),
]

_MIN_ROW_BRIGHT  = 10.0  # avg bright pixels per row to count as "has text"
_ROW_GAP_TOL     = 6     # max gap (rows) between active rows in same line cluster
_MIN_LINE_HEIGHT = 15    # pixels
_MAX_LINE_HEIGHT = 55    # pixels
_MAX_SINGLE_LINE_HEIGHT = 35  # max height for a single dynamically detected line
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

def detect_content_x_bounds(frame_bgra: np.ndarray, threshold: int = 8) -> tuple[int, int]:
    """Return (content_x0, content_x1) — the pixel columns where actual game content starts/ends.

    Handles streams with horizontal black bars (common when a player uses a 4:3 or other
    non-16:9 game resolution centred inside a 1920×1080 or 2560×1440 broadcast).
    Samples the middle third of rows so HUD overlays at the top/bottom don't confuse the
    detection.  Falls back to (0, frame_w) when no bars are found or detection is ambiguous.
    """
    fh, fw = frame_bgra.shape[:2]
    y0 = fh // 3
    y1 = (2 * fh) // 3
    region = frame_bgra[y0:y1, :, :3]          # BGR, middle rows only
    col_max = region.max(axis=(0, 2))           # max brightness per column across rows & channels

    content = np.where(col_max > threshold)[0]
    if len(content) == 0 or (content[-1] - content[0]) < fw * 0.5:
        return 0, fw                            # can't detect reliably — assume full frame

    return int(content[0]), int(content[-1] + 1)


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
    search_zone_active: bool = False,
) -> list[dict]:
    """Find killfeed line bounding boxes from an averaged brightness map.

    Args:
        brightness_map: float32 array (h, w) — avg bright pixels per cell.
        origin_x:       Absolute x offset of the map's left edge in the frame.
        origin_y:       Absolute y offset of the map's top edge in the frame.
        frame_w:        Full frame width — used to filter oversized banner regions.
        search_zone_active: True when brightness_map came from a tightly-fitted
                        per-streamer search zone (STREAMER_SEARCH_ZONES) rather
                        than the generic global box. Disables the box-relative
                        95%-width banner check below, which becomes unreliable
                        once the box itself is already fitted close to the
                        killfeed's true width (a legitimately narrow-relative-
                        to-frame line can still span ~100% of a tight box).

    Returns:
        List of {"left", "top", "width", "height"} dicts, top-to-bottom order.
    """
    max_line_w   = int(frame_w * _MAX_LINE_WIDTH_FRAC) if frame_w > 0 else 0
    search_h     = brightness_map.shape[0]
    max_top_local = int(search_h * _MAX_LINE_TOP_FRAC)  # reject lines starting below this row
    box_w        = brightness_map.shape[1]

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

    def _keep_width(width: int) -> bool:
        # Reject full-width banners ("KILL LEADER", "CHAMPION SQUAD", etc.)
        if max_line_w > 0 and width > max_line_w:
            return False
        # Reject lines spanning nearly the full search box width — these are noise/webcam.
        # Killfeed text never fills the entire search region; a span >= 95% of box is bogus.
        # Skip this check for a tightly-fitted per-streamer zone (see docstring).
        if not search_zone_active and width >= int(box_w * 0.95):
            return False
        return True

    # --- Build region dicts ---
    regions = []
    for r_start, r_end in clusters:
        # Reject clusters that start too far down — likely webcam, lower HUD, not killfeed
        if r_start > max_top_local:
            continue

        h = r_end - r_start + 1
        if h < _MIN_LINE_HEIGHT:
            continue

        if h <= _MAX_LINE_HEIGHT:
            # Column projection in this row band
            band = brightness_map[r_start:r_end + 1, :]
            col_proj = band.sum(axis=0)
            bright_cols = np.where(col_proj >= _MIN_COL_BRIGHT)[0]
            if len(bright_cols) < _MIN_LINE_WIDTH:
                continue

            left  = origin_x + int(bright_cols[0])
            width = int(bright_cols[-1]) - int(bright_cols[0]) + 1
            top   = origin_y + r_start

            if _keep_width(width):
                regions.append({"left": left, "top": top, "width": width, "height": h})
        else:
            # Oversized cluster (e.g. real killfeed lines merged with a webcam/chat
            # overlay below them) — attempt to split it into line-sized sub-bands
            # instead of discarding it outright. Previously this cluster was simply
            # dropped here and never reached _force_split_tall_region(), which exists
            # specifically to split merged multi-line content but only ever received
            # clusters that had already survived this height check.
            provisional = {
                "left": origin_x, "top": origin_y + r_start,
                "width": box_w, "height": h,
            }
            for sub in _force_split_tall_region(
                provisional, brightness_map, origin_x, origin_y, _MAX_SINGLE_LINE_HEIGHT
            ):
                if _keep_width(sub["width"]):
                    regions.append(sub)

    return regions


def _force_split_tall_region(
    region: dict,
    brightness_map: np.ndarray,
    origin_x: int,
    origin_y: int,
    max_height: int = 35,
) -> list[dict]:
    """Recursively split a region by finding the local minimum row projection if its height exceeds max_height."""
    h = region["height"]
    if h <= max_height:
        return [region]

    local_top = region["top"] - origin_y
    local_bot = local_top + h - 1

    band = brightness_map[local_top : local_bot + 1, :]
    row_proj = band.sum(axis=1)

    # Search for the minimum row projection in the middle 50% of the region
    min_y = int(0.25 * h)
    max_y = int(0.75 * h)
    if min_y >= max_y:
        return [region] # Can't split

    sub_proj = row_proj[min_y : max_y + 1]
    min_val = sub_proj.min()
    
    # Find the index with the minimum value closest to the center
    min_indices = np.where(sub_proj == min_val)[0] + min_y
    center = h / 2.0
    split_idx = min(min_indices, key=lambda idx: abs(idx - center))

    h_left = int(split_idx)
    h_right = int(h - split_idx - 1)

    results = []

    # Left sub-region
    if h_left >= _MIN_LINE_HEIGHT:
        # Recompute column bounds for the sub-region to trim horizontal space
        sub_band = brightness_map[local_top : local_top + h_left, :]
        col_proj = sub_band.sum(axis=0)
        bright_cols = np.where(col_proj >= _MIN_COL_BRIGHT)[0]
        if len(bright_cols) >= _MIN_LINE_WIDTH:
            left_reg = {
                "left": origin_x + int(bright_cols[0]),
                "top": int(region["top"]),
                "width": int(bright_cols[-1]) - int(bright_cols[0]) + 1,
                "height": h_left,
            }
            results.extend(_force_split_tall_region(left_reg, brightness_map, origin_x, origin_y, max_height))

    # Right sub-region
    if h_right >= _MIN_LINE_HEIGHT:
        sub_band = brightness_map[local_top + split_idx + 1 : local_bot + 1, :]
        col_proj = sub_band.sum(axis=0)
        bright_cols = np.where(col_proj >= _MIN_COL_BRIGHT)[0]
        if len(bright_cols) >= _MIN_LINE_WIDTH:
            right_reg = {
                "left": origin_x + int(bright_cols[0]),
                "top": int(region["top"] + split_idx + 1),
                "width": int(bright_cols[-1]) - int(bright_cols[0]) + 1,
                "height": h_right,
            }
            results.extend(_force_split_tall_region(right_reg, brightness_map, origin_x, origin_y, max_height))

    if not results:
        return [region]
    return results


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

    # Force split too-tall regions to prevent double-line crops
    final_refined = []
    for reg in refined:
        final_refined.extend(_force_split_tall_region(reg, brightness_map, origin_x, origin_y, _MAX_SINGLE_LINE_HEIGHT))
    return final_refined


def _apply_hud_exclusion(
    brightness_map: np.ndarray, origin_x: int, origin_y: int,
    content_x0: int, content_x1: int, frame_h: int,
) -> np.ndarray:
    """Zero out fixed Apex HUD elements (_HUD_EXCLUDE_ZONES) within brightness_map in place.

    Zone x-fractions are relative to game content width (content_x0..content_x1, consistent
    with how _resolve_search_box treats x0_frac/x1_frac — the HUD is drawn within the actual
    game viewport, not in any letterbox/pillarbox black bars); y-fractions relative to full
    frame height. Converted here to local (already search-box-cropped) array coordinates by
    subtracting the map's origin, then clipped to the map's shape.
    """
    content_w = content_x1 - content_x0
    box_h, box_w = brightness_map.shape[:2]
    for x0f, y0f, x1f, y1f in _HUD_EXCLUDE_ZONES:
        zx0 = content_x0 + int(content_w * x0f) - origin_x
        zx1 = content_x0 + int(content_w * x1f) - origin_x
        zy0 = int(frame_h * y0f) - origin_y
        zy1 = int(frame_h * y1f) - origin_y
        # Clip to the map's bounds — the zone may lie partly or fully outside a tight
        # per-streamer search box, in which case there's nothing to zero.
        zx0, zx1 = max(0, zx0), min(box_w, zx1)
        zy0, zy1 = max(0, zy0), min(box_h, zy1)
        if zx1 > zx0 and zy1 > zy0:
            brightness_map[zy0:zy1, zx0:zx1] = 0
    return brightness_map


def _resolve_search_box(
    content_x0: int, content_x1: int, frame_h: int, zone: dict | None = None,
) -> tuple[int, int, int, int]:
    """Compute the absolute (x0, y0, x1, y1) search box in frame pixel coordinates.

    Uses a per-streamer zone (a STREAMER_SEARCH_ZONES entry: x0_frac/x1_frac as fractions
    of content width, y0_frac/y1_frac as fractions of frame height) when provided, otherwise
    falls back to the generic _SEARCH_X_FRAC/_SEARCH_Y0_FRAC/_SEARCH_Y1_FRAC box used for
    unconfigured/ad-hoc streamers.
    """
    content_w = content_x1 - content_x0
    if zone is not None:
        x0 = content_x0 + int(content_w * zone["x0_frac"])
        x1 = content_x0 + int(content_w * zone["x1_frac"])
        y0 = int(frame_h * zone["y0_frac"])
        y1 = int(frame_h * zone["y1_frac"])
    else:
        x0 = content_x0 + int(content_w * _SEARCH_X_FRAC)
        x1 = content_x1
        y0 = int(frame_h * _SEARCH_Y0_FRAC)
        y1 = int(frame_h * _SEARCH_Y1_FRAC)
    return x0, y0, x1, y1


def detect_for_stream(
    container: av.container.Container,
    frame_w: int,
    frame_h: int,
    n_frames: int | None = None,
    thresh: int | None = None,
    return_bmap: bool = False,
    content_x0: int = 0,
    content_x1: int | None = None,
    search_zone: dict | None = None,
) -> tuple[list[dict], int] | tuple[list[dict], int, object]:
    """High-level wrapper: define search box, build map, find regions.

    Args:
        container:         Open PyAV container.
        frame_w, frame_h:  Stream frame dimensions.
        n_frames:          Override DETECT_N_FRAMES.
        thresh:            Override DETECT_BRIGHTNESS_THRESH.
        return_bmap:       If True, return (regions, n_read, bmap) instead of (regions, n_read).
        content_x0:        Left pixel of actual game content (0 if no black bars).
        content_x1:        Right pixel of actual game content (frame_w if no black bars).
        search_zone:       Optional per-streamer STREAMER_SEARCH_ZONES entry; falls back to
                            the generic global search box when None.

    Returns:
        (regions, frames_read) or (regions, frames_read, bmap) when return_bmap=True.
    """
    n_frames  = n_frames or DETECT_N_FRAMES
    thresh    = thresh   or DETECT_BRIGHTNESS_THRESH
    content_x1 = content_x1 if content_x1 is not None else frame_w
    content_w  = content_x1 - content_x0

    x0, y0, x1, y1 = _resolve_search_box(content_x0, content_x1, frame_h, search_zone)

    bmap, n_read = _build_brightness_map(container, (x0, y0, x1, y1), n_frames, thresh)
    if bmap is None:
        return ([], 0, None) if return_bmap else ([], 0)
    _apply_hud_exclusion(bmap, x0, y0, content_x0, content_x1, frame_h)

    regions = detect_killfeed_regions(
        bmap, x0, y0, frame_w=content_w, search_zone_active=search_zone is not None
    )
    regions = _refine_regions(regions, bmap, x0, y0)
    return (regions, n_read, bmap) if return_bmap else (regions, n_read)


def detect_killfeed_from_frame(
    frame_bgra,
    frame_w: int,
    frame_h: int,
    thresh: int | None = None,
    content_x0: int = 0,
    content_x1: int | None = None,
    search_zone: dict | None = None,
) -> list[dict]:
    """Single-frame killfeed detection for use inside the OCR loop.

    Runs the same region-finding logic as detect_for_stream() but on one already-decoded
    frame instead of averaging over N frames.  Cost: ~1 ms vs 50–200 ms for OCR.

    Args:
        content_x0, content_x1: Game-content pixel bounds (pass 0 / frame_w when no black bars).
        search_zone:             Optional per-streamer STREAMER_SEARCH_ZONES entry; falls back
                                  to the generic global search box when None.

    Returns [] if no valid regions found — caller should fall back to last known good.
    """
    thresh     = thresh or DETECT_BRIGHTNESS_THRESH
    content_x1 = content_x1 if content_x1 is not None else frame_w
    content_w  = content_x1 - content_x0

    x0, y0, x1, y1 = _resolve_search_box(content_x0, content_x1, frame_h, search_zone)

    region = frame_bgra[y0:y1, x0:x1]
    gray   = cv2.cvtColor(region, cv2.COLOR_BGRA2GRAY)
    bmap   = (gray >= thresh).astype(np.float32)
    _apply_hud_exclusion(bmap, x0, y0, content_x0, content_x1, frame_h)

    regions = detect_killfeed_regions(
        bmap, x0, y0, frame_w=content_w, search_zone_active=search_zone is not None
    )
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
    parser.add_argument("--no-zone", action="store_true",
                        help="Ignore STREAMER_SEARCH_ZONES and use the generic global search box "
                             "even for a configured streamer (useful for before/after comparison)")
    parser.add_argument("--use-cache", action="store_true",
                        help="If not hardcoded in STREAMER_SEARCH_ZONES, also check "
                             "calibration_cache.json for an auto-calibrated zone (see calibrate_zone.py)")
    args = parser.parse_args()

    username = args.channel.rstrip("/").split("/")[-1].lower()
    display_name = TWITCH_CHANNELS.get(username, username.title())
    zone_source = "none/generic"
    search_zone = None
    if not args.no_zone:
        search_zone = STREAMER_SEARCH_ZONES.get(display_name)
        if search_zone is not None:
            zone_source = f"hardcoded:{display_name}"
        elif args.use_cache:
            # Local import: calibrate_zone.py imports FROM this module, so importing it at
            # module level here would be circular. Only needed for this CLI testing path.
            from calibrate_zone import get_cached_zone
            search_zone = get_cached_zone(display_name)
            if search_zone is not None:
                zone_source = f"cached:{display_name}"
    print(f"Opening stream for '{username}' (display name: {display_name}, zone: {zone_source})...")

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

        x0, y0, x1, y1 = _resolve_search_box(0, fw, fh, search_zone)

        # Save search-region crop for visual inspection
        if args.save_frame and preview_frame is not None:
            crop = cv2.cvtColor(preview_frame[y0:y1, x0:x1], cv2.COLOR_BGRA2BGR)
            cv2.imwrite(args.save_frame, crop)
            print(f"Saved search region ({x0},{y0})-({x1},{y1}) to {args.save_frame}")

        regions, n_read, bmap = detect_for_stream(
            container, fw, fh, n_frames=args.frames, return_bmap=True, search_zone=search_zone
        )

        print(f"\nSampled {n_read} frames.")
        if not regions:
            print("No killfeed lines detected. Is there active gameplay on the stream?")
        else:
            print(f"Detected {len(regions)} killfeed line(s):")
            for i, r in enumerate(regions):
                print(f"  Line {i}: left={r['left']}  top={r['top']}  "
                      f"width={r['width']}  height={r['height']}")

        if args.debug:
            _save_debug_images(preview_frame, bmap, regions, (x0, y0, x1, y1))

        if args.preview and preview_frame is not None:
            display = cv2.cvtColor(preview_frame, cv2.COLOR_BGRA2BGR)
            for r in regions:
                cv2.rectangle(
                    display,
                    (r['left'], r['top']),
                    (r['left'] + r['width'], r['top'] + r['height']),
                    (0, 255, 0), 2
                )
            cv2.rectangle(display, (x0, y0), (x1 - 1, y1), (0, 0, 255), 1)

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
