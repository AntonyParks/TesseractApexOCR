"""calibrate_zone.py — Auto-calibrate a per-streamer killfeed search zone via Claude vision.

For streamers with no hand-calibrated STREAMER_SEARCH_ZONES entry (config.py), this module
runs a bounded, self-verifying propose -> verify -> refine loop instead of always falling back
to the loose generic search box in detect_killfeed.py.

Critical design decision: the vision model is never asked to invent pixel/fraction coordinates
from a blind frame. Every round is grounded in the REAL output of detect_for_stream() — Claude's
job is only to CLASSIFY numbered candidate regions (killfeed / webcam / chat / hud_banner /
ping_marker / menu_loading / other_noise), and the zone's fractions are derived deterministically
from the pixel coordinates of the regions it labels "killfeed". This mirrors the manual
calibration process used to hand-verify Apryze/ShivFPS/Faide/Sang/Gent: propose a zone, run the
actual detector, inspect the resulting candidate regions, and tighten based on what's really
there — not a single guess from a static image.

Usage (standalone, for testing/manual calibration):
    python calibrate_zone.py <twitch_username> [--force]

Live spot-check procedure for a newly auto-calibrated streamer:
    1. python calibrate_zone.py <streamer>        — run calibration in isolation, inspect the
       printed round-by-round classification log.
    2. python detect_killfeed.py <streamer> --use-cache --preview  — visually confirm the
       accepted zone's detected regions land on real killfeed text.
    3. Let a ChannelWorker run for a few minutes and check crops/<streamer>/ for contamination.
"""
import base64
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import av
import cv2
import numpy as np
from PIL import Image

from config import (
    AUTO_CALIBRATE_ZONES, AUTO_CALIBRATE_TIME_BUDGET, AUTO_CALIBRATE_MAX_ROUNDS,
    AUTO_CALIBRATE_MAX_CONCURRENT, AUTO_CALIBRATE_PAD_X_FRAC, AUTO_CALIBRATE_PAD_Y_FRAC,
    AUTO_CALIBRATE_RANKED_WAIT_FRAMES, AUTO_CALIBRATE_VERIFY_SAMPLES,
    AUTO_CALIBRATE_KILLFEED_HEIGHT_FRAC, AUTO_CALIBRATE_MIN_KILLFEED_LINES,
    AUTO_CALIBRATE_CONFIRM_OVERLAP, KILLFEED_TOP_MAX_FRAC, KILLFEED_MAX_SPAN_FRAC,
    CALIBRATION_CACHE_PATH, CALIBRATE_VISION_MODEL, DETECT_MIN_LINES, STREAMER_SEARCH_ZONES,
)
from detect_killfeed import detect_for_stream, detect_killfeed_from_frame
from detect_ranked import is_ranked_game

# Sentinel returned by get_search_zone() to mean "this streamer's overlay can't be reliably
# separated from the killfeed — skip OCR-ing them entirely" (rather than a dict zone, or None
# for "just use the generic fallback box"). Deliberately a distinct type from dict/None so
# callers can't accidentally treat it as a usable zone.
BYPASS = "__BYPASS__"

_cache_lock = threading.Lock()
_negative_cache: set[str] = set()  # in-process only; a fresh run always retries (see module docstring)
_calibration_semaphore = threading.Semaphore(AUTO_CALIBRATE_MAX_CONCURRENT)

_REGION_LABELS = [
    "killfeed", "webcam", "chat", "hud_banner",
    "ping_marker", "menu_loading", "other_noise",
]

# Anthropic tool-use provides structured output: we force a call to this tool and read its
# `input` as the classification result. Uses standard JSON Schema (lowercase types), unlike the
# Gemini responseSchema format this module originally used.
_CLASSIFY_TOOL = {
    "name": "report_region_labels",
    "description": "Report the classification label for each numbered region drawn on the screenshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The number drawn on the box."},
                        "label": {"type": "string", "enum": _REGION_LABELS},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["index", "label"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["regions"],
    },
}

# Lazily-initialised Anthropic client shared across calibration threads, plus a gentle minimum
# spacing between vision calls (paid tier has generous limits, but avoid needless bursts when
# several workers calibrate at once).
_anthropic_client = None
_client_lock = threading.Lock()
_vision_call_lock = threading.Lock()
_last_vision_call = [0.0]
_VISION_MIN_INTERVAL = 1.0


def _get_anthropic_client():
    """Return a cached Anthropic client, or None if the SDK or ANTHROPIC_API_KEY is unavailable."""
    global _anthropic_client
    with _client_lock:
        if _anthropic_client is None:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                print("[Calibrate] ANTHROPIC_API_KEY not set — vision calibration unavailable.")
                return None
            try:
                import anthropic
                _anthropic_client = anthropic.Anthropic(api_key=key)
            except Exception as e:
                print(f"[Calibrate] Anthropic client init failed: {type(e).__name__}: {e}")
                return None
        return _anthropic_client


def _png_b64(img_rgb: np.ndarray) -> str:
    """Encode an RGB ndarray as a base64 PNG string (no data-URI prefix)."""
    buf = io.BytesIO()
    Image.fromarray(img_rgb).save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _classify_prompt(n: int) -> str:
    return (
        "This is a full screenshot from a live Apex Legends Twitch stream. Numbered red boxes "
        f"have been drawn on it, labeled 0 to {n - 1}. The REAL Apex killfeed is the small "
        "semi-transparent strip in the upper-right area of the game view that lists recent "
        "eliminations as white text lines, e.g. 'PlayerName eliminated PlayerName' or "
        "'PlayerName knocked down PlayerName', appearing and disappearing as kills happen. "
        "It is NOT:\n"
        "- a webcam/facecam overlay (a real photographic image of a person)\n"
        "- a Twitch chat overlay (colored usernames + short chat messages)\n"
        "- a fixed HUD banner (e.g. 'N SQUADS LEFT', 'CHAMPION SQUAD', ability cooldown icons, "
        "subscriber/goal counters)\n"
        "- an in-world ping/waypoint marker (short text like a distance '297M' floating over "
        "the 3D game world, not a UI strip)\n"
        "- a main menu, loading screen, or death recap screen\n"
        "For EACH numbered box, classify it as one of: killfeed, webcam, chat, hud_banner, "
        "ping_marker, menu_loading, other_noise. A frame may have zero, one, or multiple boxes "
        "labeled killfeed if the killfeed text was split into multiple line regions."
    )


# ---------------------------------------------------------------------------
# Cache (calibration_cache.json) — successful zones + permanent bypass list, atomic writes
#
# Format: {"zones": {streamer: {x0_frac, ..., calibrated_at, ...}}, "bypass": {streamer: {reason, bypassed_at}}}
# A streamer appears in at most one of the two sections at a time.
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        raw = json.loads(CALIBRATION_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"zones": {}, "bypass": {}}
    if "zones" not in raw and "bypass" not in raw:
        # Migrate the older flat streamer->zone format transparently.
        return {"zones": raw, "bypass": {}}
    raw.setdefault("zones", {})
    raw.setdefault("bypass", {})
    return raw


def _save_cache(cache: dict) -> None:
    tmp = CALIBRATION_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CALIBRATION_CACHE_PATH)


def get_cached_zone(streamer: str) -> dict | None:
    with _cache_lock:
        entry = _load_cache()["zones"].get(streamer)
    if entry is None:
        return None
    return {k: entry[k] for k in ("x0_frac", "x1_frac", "y0_frac", "y1_frac")}


def _store_cached_zone(streamer: str, zone: dict, rounds_used: int, frame_w: int, frame_h: int) -> None:
    with _cache_lock:
        cache = _load_cache()
        cache["bypass"].pop(streamer, None)  # a fresh success supersedes any prior bypass
        cache["zones"][streamer] = {
            **zone,
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
            "rounds_used": rounds_used,
            "frame_w": frame_w,
            "frame_h": frame_h,
        }
        _save_cache(cache)


def is_bypassed(streamer: str) -> bool:
    with _cache_lock:
        return streamer in _load_cache()["bypass"]


def _store_bypass(streamer: str, reason: str) -> None:
    with _cache_lock:
        cache = _load_cache()
        cache["zones"].pop(streamer, None)
        cache["bypass"][streamer] = {
            "reason": reason,
            "bypassed_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_cache(cache)


def invalidate_cached_zone(streamer: str) -> bool:
    """Manually remove a streamer's cached zone or bypass entry (also clears the in-process
    negative cache), allowing calibration to be retried from scratch."""
    with _cache_lock:
        _negative_cache.discard(streamer)
        cache = _load_cache()
        removed = False
        if streamer in cache["zones"]:
            del cache["zones"][streamer]
            removed = True
        if streamer in cache["bypass"]:
            del cache["bypass"][streamer]
            removed = True
        if removed:
            _save_cache(cache)
        return removed


# ---------------------------------------------------------------------------
# Frame / region helpers
# ---------------------------------------------------------------------------

def _grab_one_frame(container: av.container.Container, deadline: float) -> np.ndarray | None:
    for packet in container.demux(video=0):
        if time.time() > deadline:
            return None
        try:
            for frame in packet.decode():
                return frame.to_ndarray(format="bgra")
        except Exception:
            continue
    return None


def _wait_for_ranked_frame(container: av.container.Container, deadline: float, max_frames: int) -> np.ndarray | None:
    """Sample frames until one shows confirmed ranked gameplay, or budget/frame cap exhausted."""
    checked = 0
    for packet in container.demux(video=0):
        if time.time() > deadline or checked >= max_frames:
            return None
        try:
            for frame in packet.decode():
                arr = frame.to_ndarray(format="bgra")
                checked += 1
                if is_ranked_game(arr, arr.shape[1], arr.shape[0]):
                    return arr
                break
        except Exception:
            pass
    return None


def _draw_numbered_regions(frame_bgra: np.ndarray, regions: list[dict]) -> np.ndarray:
    display = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR)
    for i, r in enumerate(regions):
        x0, y0 = r["left"], r["top"]
        x1, y1 = x0 + r["width"], y0 + r["height"]
        cv2.rectangle(display, (x0, y0), (x1, y1), (0, 0, 255), 2)
        cv2.putText(display, str(i), (x0, max(12, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
    return display


def _classify_regions(frame_bgra: np.ndarray, regions: list[dict]) -> list[dict] | None:
    """Ask Claude vision to classify each numbered candidate region. Returns the list of
    {index, label, confidence} dicts, or None on any failure (no client, API error, or
    malformed response)."""
    if not regions:
        return None
    client = _get_anthropic_client()
    if client is None:
        return None
    annotated_bgr = _draw_numbered_regions(frame_bgra, regions)
    annotated_rgb = cv2.cvtColor(annotated_bgr, cv2.COLOR_BGR2RGB)
    try:
        b64 = _png_b64(annotated_rgb)
    except Exception:
        return None

    # Gentle spacing between vision calls, shared across calibration threads.
    with _vision_call_lock:
        wait = _VISION_MIN_INTERVAL - (time.time() - _last_vision_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_vision_call[0] = time.time()

    try:
        resp = client.messages.create(
            model=CALIBRATE_VISION_MODEL,
            max_tokens=1024,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": _CLASSIFY_TOOL["name"]},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": b64,
                    }},
                    {"type": "text", "text": _classify_prompt(len(regions))},
                ],
            }],
        )
    except Exception as e:
        print(f"[Calibrate] Claude vision call failed: {type(e).__name__}: {e}")
        return None

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _CLASSIFY_TOOL["name"]:
            data = block.input
            if isinstance(data, dict) and isinstance(data.get("regions"), list):
                return data["regions"]
    return None


def _split_classifications(
    regions: list[dict], classifications: list[dict],
) -> tuple[list[dict], list[dict], list[str]]:
    """Split detected regions into (killfeed_regions, contaminant_regions, contaminant_labels)
    using Gemini's per-index classification. Regions are returned in the same dict shape
    detect_for_stream() produces (left/top/width/height), not the classification dicts
    themselves, so callers can feed them straight into _derive_zone_from_regions /
    _tighten_against_contaminants regardless of which detection pass they came from."""
    by_index = {c["index"]: c for c in classifications if isinstance(c.get("index"), int)}
    killfeed = [regions[i] for i in by_index
                if by_index[i].get("label") == "killfeed" and 0 <= i < len(regions)]
    contaminants = [regions[i] for i in by_index
                     if by_index[i].get("label") != "killfeed" and 0 <= i < len(regions)]
    labels = sorted({by_index[i].get("label") for i in by_index if by_index[i].get("label") != "killfeed"})
    return killfeed, contaminants, labels


def _derive_zone_from_regions(
    killfeed_regions: list[dict], content_x0: int, content_w: int, frame_h: int,
    streamer: str = "?",
) -> dict | None:
    """x1_frac is always 1.0, matching every hand-calibrated STREAMER_SEARCH_ZONES entry —
    killfeed runs to the right content edge in all data observed this session.

    y0 (top) and x0 (left) are tight — that is what excludes the top HUD banner and the minimap.
    y1 (bottom) is GENEROUS by default: the killfeed stacks DOWNWARD from a fixed top as kills
    happen, so a zone derived only from a quiet 1-2 line moment would silently drop the extra
    lines of a later multi-kill burst. So y1 = max(observed bottom, y0 + a full killfeed height).
    _tighten_against_contaminants then pulls y1 back up only if something (e.g. a facecam) is
    actually detected below — otherwise the generous height stands.

    COVERAGE GUARD (returns None to reject): the Apex killfeed always STARTS in the upper-right
    band. If the classifier mislabeled a facecam/HUD element below the killfeed, the topmost
    "killfeed" region sits too low — reject rather than lock a zone that misses the real feed. Any
    surviving low mislabels are dropped, and total height is capped, so the zone can't reach the
    facecam. See config KILLFEED_TOP_MAX_FRAC / KILLFEED_MAX_SPAN_FRAC."""
    if not killfeed_regions or content_w <= 0:
        return None

    top_min_frac = min(r["top"] for r in killfeed_regions) / frame_h
    # Guard 1: killfeed never starts this low on screen — the model locked onto a facecam/HUD.
    if top_min_frac > KILLFEED_TOP_MAX_FRAC:
        print(f"[Calibrate:{streamer}] COVERAGE GUARD 1 rejected zone: topmost killfeed region "
              f"y0={top_min_frac:.3f} > KILLFEED_TOP_MAX_FRAC={KILLFEED_TOP_MAX_FRAC} "
              f"(facecam/HUD mislabel below the real feed) — staying on generic box.")
        return None
    # Guard 2: drop "killfeed" regions sitting far below the top line (facecam/HUD mislabels below
    # the real feed) so they don't inflate the zone height.
    band_bottom_px = (top_min_frac + KILLFEED_MAX_SPAN_FRAC) * frame_h
    kept = [r for r in killfeed_regions if r["top"] <= band_bottom_px]
    if len(kept) < len(killfeed_regions):
        print(f"[Calibrate:{streamer}] COVERAGE GUARD 2 dropped {len(killfeed_regions) - len(kept)} "
              f"region(s) sitting below the killfeed band (top+{KILLFEED_MAX_SPAN_FRAC}).")
    if len(kept) < AUTO_CALIBRATE_MIN_KILLFEED_LINES:
        print(f"[Calibrate:{streamer}] COVERAGE GUARD rejected zone: only {len(kept)} killfeed "
              f"region(s) left in-band (< AUTO_CALIBRATE_MIN_KILLFEED_LINES) — staying on generic box.")
        return None

    lefts = [r["left"] for r in kept]
    tops = [r["top"] for r in kept]
    bottoms = [r["top"] + r["height"] for r in kept]

    x0_frac = max(0.0, (min(lefts) - content_x0) / content_w - AUTO_CALIBRATE_PAD_X_FRAC)
    y0_frac = max(0.0, min(tops) / frame_h - AUTO_CALIBRATE_PAD_Y_FRAC)
    observed_y1 = max(bottoms) / frame_h + AUTO_CALIBRATE_PAD_Y_FRAC
    generous_y1 = y0_frac + AUTO_CALIBRATE_KILLFEED_HEIGHT_FRAC
    y1_frac = min(1.0, max(observed_y1, generous_y1))
    # Guard 3: cap total height so the zone can't extend down into the facecam/HUD below.
    capped_y1 = min(y1_frac, y0_frac + KILLFEED_MAX_SPAN_FRAC)
    if capped_y1 < y1_frac:
        print(f"[Calibrate:{streamer}] COVERAGE GUARD 3 capped zone height: y1 {y1_frac:.3f} -> "
              f"{capped_y1:.3f} (span capped at KILLFEED_MAX_SPAN_FRAC={KILLFEED_MAX_SPAN_FRAC}).")
    y1_frac = capped_y1
    zone = {"x0_frac": round(x0_frac, 3), "x1_frac": 1.0, "y0_frac": round(y0_frac, 3), "y1_frac": round(y1_frac, 3)}
    print(f"[Calibrate:{streamer}] Derived zone {zone} "
          f"(y0={zone['y0_frac']:.3f}<=0.26, span={zone['y1_frac'] - zone['y0_frac']:.3f}<=0.24) — guard PASS.")
    return zone


def _tighten_against_contaminants(
    zone: dict, contaminants: list[dict], content_x0: int, content_w: int, frame_h: int,
) -> dict:
    """Pull the zone's boundary in from whichever side a contaminating region sits on."""
    zone = dict(zone)
    for c in contaminants:
        c_top_frac = c["top"] / frame_h
        c_right_frac = (c["left"] + c["width"] - content_x0) / content_w
        if c_top_frac >= zone["y0_frac"]:
            # Contaminant sits at/below the killfeed zone -- pull y1 up above it.
            zone["y1_frac"] = min(zone["y1_frac"], max(zone["y0_frac"] + 0.02, c_top_frac - AUTO_CALIBRATE_PAD_Y_FRAC))
        if c_right_frac <= zone["x0_frac"] + 0.05:
            # Contaminant sits to the left of (or overlapping the start of) the zone -- push x0 right past it.
            zone["x0_frac"] = max(zone["x0_frac"], c_right_frac + AUTO_CALIBRATE_PAD_X_FRAC)
    return zone


def _reverify_zone(
    streamer: str,
    container: av.container.Container,
    frame_w: int, frame_h: int,
    content_x0: int, content_x1: int,
    zone: dict,
    deadline: float,
) -> list[dict]:
    """Re-check a seemingly-clean zone with a few more time-spaced samples before trusting it.

    One clean frame is not proof there's no persistent overlay nearby (e.g. a subscriber-goal
    bar, browser watermark) that simply didn't register as a bright candidate region in that
    exact instant. Returns any contaminant regions found across the extra samples (empty list
    if every sample stayed clean or the samples were inconclusive, e.g. Gemini failures --
    inconclusive samples are NOT treated as evidence of contamination, only positive findings
    are).
    """
    found: list[dict] = []
    for i in range(AUTO_CALIBRATE_VERIFY_SAMPLES):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(2.5, remaining))
        regions, _n_read = detect_for_stream(
            container, frame_w, frame_h,
            content_x0=content_x0, content_x1=content_x1, search_zone=zone,
        )
        if not regions:
            continue
        frame_bgra = _grab_one_frame(container, deadline)
        if frame_bgra is None:
            continue
        classifications = _classify_regions(frame_bgra, regions)
        if classifications is None:
            continue
        _kf, contaminants, labels = _split_classifications(regions, classifications)
        if contaminants:
            print(f"[Calibrate:{streamer}] Verify sample {i + 1}/{AUTO_CALIBRATE_VERIFY_SAMPLES}: "
                  f"contamination found {labels}")
            found.extend(contaminants)
    return found


# ---------------------------------------------------------------------------
# Core propose -> verify -> refine loop
# ---------------------------------------------------------------------------

def _run_calibration(
    streamer: str,
    container: av.container.Container,
    frame_w: int, frame_h: int,
    content_x0: int, content_x1: int,
    stop_event: threading.Event,
    deadline: float,
) -> tuple[dict | None, int, bool]:
    """Returns (validated zone dict or None, rounds actually used, bypass).

    bypass=True means contamination was seen and never resolved within the round budget --
    this streamer's overlay likely can't be cleanly separated from the killfeed at all, and the
    caller should skip OCR-ing them entirely rather than falling back to the even-noisier
    generic box. bypass=False with zone=None just means "inconclusive this run" (no ranked
    game found, Gemini unreachable, etc.) -- worth retrying on a future launch.
    """
    if time.time() > deadline or stop_event.is_set():
        return None, 0, False

    ranked_frame = _wait_for_ranked_frame(container, deadline, AUTO_CALIBRATE_RANKED_WAIT_FRAMES)
    if ranked_frame is None:
        print(f"[Calibrate:{streamer}] No ranked gameplay confirmed within budget -- aborting this attempt.")
        return None, 0, False

    content_w = content_x1 - content_x0
    zone = None
    prev_zone: dict | None = None
    prev_n_killfeed = 0
    ever_saw_contamination = False

    for round_num in range(1, AUTO_CALIBRATE_MAX_ROUNDS + 1):
        if time.time() > deadline or stop_event.is_set():
            break

        regions, _n_read = detect_for_stream(
            container, frame_w, frame_h,
            content_x0=content_x0, content_x1=content_x1, search_zone=zone,
        )
        if not regions:
            print(f"[Calibrate:{streamer}] Round {round_num}: 0 regions detected -- giving up.")
            break

        frame_bgra = _grab_one_frame(container, deadline)
        if frame_bgra is None:
            break

        classifications = _classify_regions(frame_bgra, regions)
        if classifications is None:
            print(f"[Calibrate:{streamer}] Round {round_num}: vision classification failed -- giving up.")
            break

        killfeed_regions, contaminants, labels_found = _split_classifications(regions, classifications)
        print(f"[Calibrate:{streamer}] Round {round_num}: {len(killfeed_regions)} killfeed region(s), "
              f"{len(contaminants)} contaminant(s) {labels_found}")

        if not killfeed_regions:
            break

        if contaminants:
            ever_saw_contamination = True
        elif len(killfeed_regions) >= DETECT_MIN_LINES:
            # Looks clean on this one sample -- re-check with a few more time-spaced samples
            # before trusting it (see _reverify_zone's docstring for why).
            candidate = _derive_zone_from_regions(killfeed_regions, content_x0, content_w, frame_h, streamer)
            if candidate is None:
                break
            verify_contaminants = _reverify_zone(
                streamer, container, frame_w, frame_h, content_x0, content_x1, candidate, deadline
            )
            if not verify_contaminants:
                return candidate, round_num, False
            print(f"[Calibrate:{streamer}] Round {round_num} looked clean but re-verification "
                  f"caught contamination -- tightening and retrying.")
            ever_saw_contamination = True
            contaminants = verify_contaminants

        # Either this round or re-verification found contamination -- try to tighten around it.
        if prev_zone is not None and len(killfeed_regions) < prev_n_killfeed:
            break  # over-shrank vs. the last accepted round -- stop, evaluate prev_zone below

        candidate = _derive_zone_from_regions(killfeed_regions, content_x0, content_w, frame_h, streamer)
        if candidate is None:
            break
        candidate = _tighten_against_contaminants(candidate, contaminants, content_x0, content_w, frame_h)

        prev_zone, prev_n_killfeed = candidate, len(killfeed_regions)
        zone = candidate

    if prev_zone is not None and prev_n_killfeed >= DETECT_MIN_LINES and not ever_saw_contamination:
        return prev_zone, AUTO_CALIBRATE_MAX_ROUNDS, False

    if ever_saw_contamination:
        print(f"[Calibrate:{streamer}] Contamination persisted through the calibration budget -- "
              f"this streamer's overlay likely can't be cleanly separated from the killfeed. Bypassing.")
        return None, AUTO_CALIBRATE_MAX_ROUNDS, True

    print(f"[Calibrate:{streamer}] Exhausted rounds without enough evidence -- giving up for this run.")
    return None, AUTO_CALIBRATE_MAX_ROUNDS, False


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------

def resolve_static_zone(streamer: str) -> dict | str | None:
    """Instant, no-network zone lookup for worker startup. Returns, in priority order:
      - the hardcoded STREAMER_SEARCH_ZONES entry, or
      - the BYPASS sentinel if the streamer was permanently bypassed, or
      - a previously cached calibrated zone, or
      - None, meaning "not calibrated yet" — the worker starts on the generic box and calibrates
        live via attempt_calibration_from_frame() over the session.
    Never runs the vision model; safe to call synchronously at startup."""
    hardcoded = STREAMER_SEARCH_ZONES.get(streamer)
    if hardcoded is not None:
        return hardcoded
    if not AUTO_CALIBRATE_ZONES:
        return None
    if is_bypassed(streamer):
        return BYPASS
    return get_cached_zone(streamer)


def attempt_calibration_from_frame(
    streamer: str,
    frame_bgra: np.ndarray,
    regions: list[dict],
    content_x0: int,
    content_x1: int,
    frame_h: int,
) -> dict | None:
    """One live calibration attempt on a single already-decoded frame, using the candidate
    *regions* the worker's per-frame detector already produced from the generic box (so no
    redundant detection and no Claude call is spent on a blank frame).

    Classifies those regions with Claude; if a real killfeed of at least
    AUTO_CALIBRATE_MIN_KILLFEED_LINES stacked lines is present this frame, derives and returns a
    tight zone (generous height, pulled up past any contaminant below). Returns None if there is
    no killfeed on screen this frame or on any failure. Does NOT cache — a single frame can
    mislead (a facecam on a sparse feed can be mislabeled 'killfeed'), so the caller must confirm
    the zone against a second, independent attempt (see zones_overlap / commit_calibrated_zone)
    before locking it in. Never raises."""
    try:
        if not regions:
            return None
        content_w = content_x1 - content_x0
        if content_w <= 0:
            return None
        classifications = _classify_regions(frame_bgra, regions)
        if classifications is None:
            return None
        killfeed_regions, contaminants, labels = _split_classifications(regions, classifications)
        if len(killfeed_regions) < AUTO_CALIBRATE_MIN_KILLFEED_LINES:
            return None
        zone = _derive_zone_from_regions(killfeed_regions, content_x0, content_w, frame_h, streamer)
        if zone is None:
            return None
        zone = _tighten_against_contaminants(zone, contaminants, content_x0, content_w, frame_h)
        print(f"[Calibrate:{streamer}] Candidate zone from live frame: {zone} "
              f"(killfeed lines={len(killfeed_regions)}, other={labels})")
        return zone
    except Exception as e:
        print(f"[Calibrate:{streamer}] Live calibration attempt error: {type(e).__name__}: {e}")
        return None


def zones_overlap(z1: dict, z2: dict) -> bool:
    """True if two candidate zones agree enough to confirm one another: their vertical ranges
    overlap by >= AUTO_CALIBRATE_CONFIRM_OVERLAP of the smaller span AND their left edges are
    within 0.1. A one-off misclassification (e.g. a facecam frame) rarely reproduces the same
    zone twice, so requiring agreement across two independent attempts rejects it."""
    inter = max(0.0, min(z1["y1_frac"], z2["y1_frac"]) - max(z1["y0_frac"], z2["y0_frac"]))
    span = min(z1["y1_frac"] - z1["y0_frac"], z2["y1_frac"] - z2["y0_frac"])
    if span <= 0:
        return False
    return (inter / span) >= AUTO_CALIBRATE_CONFIRM_OVERLAP and abs(z1["x0_frac"] - z2["x0_frac"]) <= 0.1


def commit_calibrated_zone(streamer: str, zone: dict, frame_w: int, frame_h: int) -> None:
    """Persist a CONFIRMED zone to the cache (called by the worker only after two independent
    attempts agreed via zones_overlap)."""
    _store_cached_zone(streamer, zone, 2, frame_w, frame_h)
    print(f"[Calibrate:{streamer}] Zone CONFIRMED and locked: {zone}")


def get_search_zone(
    streamer: str,
    container: av.container.Container,
    frame_w: int,
    frame_h: int,
    content_x0: int,
    content_x1: int,
    stop_event: threading.Event,
) -> dict | str | None:
    """Resolve a search zone for *streamer*, auto-calibrating via Claude vision if needed.

    Resolution order:
      1. Hardcoded STREAMER_SEARCH_ZONES[streamer]  (unchanged, highest priority, free/instant)
      2. Permanent bypass list (calibration_cache.json's "bypass" section) -> returns BYPASS
      3. Cached zone from calibration_cache.json
      4. If AUTO_CALIBRATE_ZONES: run the calibration loop (bounded time/rounds). On success,
         cache and return the zone. If contamination persisted unresolved, permanently record
         the streamer as bypassed and return BYPASS. Otherwise (inconclusive -- no ranked game
         found, Gemini unreachable, etc.) fall through to None, retryable on a future launch.
      5. None (caller falls back to the generic search box in detect_killfeed.py).

    Never raises. Never blocks longer than AUTO_CALIBRATE_TIME_BUDGET seconds total.
    """
    hardcoded = STREAMER_SEARCH_ZONES.get(streamer)
    if hardcoded is not None:
        return hardcoded

    if not AUTO_CALIBRATE_ZONES:
        return None

    if is_bypassed(streamer):
        return BYPASS

    cached = get_cached_zone(streamer)
    if cached is not None:
        return cached

    with _cache_lock:
        if streamer in _negative_cache:
            return None

    deadline = time.time() + AUTO_CALIBRATE_TIME_BUDGET
    acquired = _calibration_semaphore.acquire(timeout=max(0.0, deadline - time.time()))
    if not acquired:
        with _cache_lock:
            _negative_cache.add(streamer)
        return None

    try:
        zone, rounds_used, bypass = _run_calibration(
            streamer, container, frame_w, frame_h, content_x0, content_x1, stop_event, deadline
        )
    except Exception as e:
        print(f"[Calibrate:{streamer}] Unexpected error during calibration: {e}")
        zone, rounds_used, bypass = None, 0, False
    finally:
        _calibration_semaphore.release()

    if zone is not None:
        _store_cached_zone(streamer, zone, rounds_used, frame_w, frame_h)
        print(f"[Calibrate:{streamer}] Calibration succeeded in {rounds_used} round(s): {zone}")
        return zone

    if bypass:
        _store_bypass(streamer, "contamination persisted through calibration budget")
        print(f"[Calibrate:{streamer}] Bypassing this streamer permanently -- overlay contamination "
              f"could not be resolved. Will not be OCR'd. (invalidate_cached_zone() to retry manually)")
        return BYPASS

    with _cache_lock:
        _negative_cache.add(streamer)
    print(f"[Calibrate:{streamer}] Calibration inconclusive -- using generic fallback box this session "
          f"(will retry calibration on a future launch).")
    return None


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    from detect_killfeed import open_stream, _get_frame_dimensions, detect_content_x_bounds
    from config import TWITCH_CHANNELS

    parser = argparse.ArgumentParser(description="Standalone killfeed zone calibration via Claude vision")
    parser.add_argument("channel", help="Twitch username")
    parser.add_argument("--force", action="store_true",
                         help="Bypass hardcoded/cached zones and re-run calibration for comparison")
    parser.add_argument("--unblock", action="store_true",
                         help="Clear a permanent bypass/cached zone for this streamer and exit "
                              "(allows calibration to be retried on the next launch)")
    args = parser.parse_args()

    username = args.channel.lower().strip()
    display_name = TWITCH_CHANNELS.get(username, username.title())

    if args.unblock:
        removed = invalidate_cached_zone(display_name)
        print(f"{'Cleared' if removed else 'No entry found for'} {display_name} in calibration_cache.json")
        return

    print(f"Opening stream for '{username}' (display name: {display_name})...")

    container, procs = open_stream(username)
    try:
        fw, fh = _get_frame_dimensions(container)
        content_x0, content_x1 = 0, fw
        for packet in container.demux(video=0):
            try:
                for frame in packet.decode():
                    arr = frame.to_ndarray(format="bgra")
                    content_x0, content_x1 = detect_content_x_bounds(arr)
                    break
            except Exception:
                pass
            break

        if args.force:
            deadline = time.time() + AUTO_CALIBRATE_TIME_BUDGET
            zone, rounds, bypass = _run_calibration(
                display_name, container, fw, fh, content_x0, content_x1, threading.Event(), deadline
            )
            if zone:
                print(f"\nCalibrated zone for {display_name}: {zone} (rounds={rounds})")
                _store_cached_zone(display_name, zone, rounds, fw, fh)
            elif bypass:
                print(f"\n{display_name} would be BYPASSED -- contamination persisted through the "
                      f"calibration budget and could not be resolved.")
            else:
                print(f"\nCalibration inconclusive for {display_name} (no ranked game found, "
                      f"Gemini unreachable, etc.) -- not enough evidence either way.")
        else:
            zone = get_search_zone(display_name, container, fw, fh, content_x0, content_x1, threading.Event())
            if zone == BYPASS:
                print(f"\n{display_name} is BYPASSED -- would not be OCR'd in the live pipeline.")
            elif zone:
                print(f"\nZone for {display_name}: {zone}")
            else:
                print(f"\nNo zone available for {display_name} -- generic fallback box would be used.")
    finally:
        container.close()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
