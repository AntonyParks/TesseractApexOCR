"""Per-streamer daily MASTER/PREDATOR collection gate.

Only collect games from Master/Predator streamers (bead 2mo). A streamer's rank is classified ONCE
per day from IN-GAME frames -- the rank badge (top-right HUD) only renders during a live match, not
in lobby/menus/loadout, so classifying off a menu frame is unreliable (and menu red UI false-triggers
the Predator colour). The decision is cached per (streamer, date); a below-Master streamer is re-checked
the NEXT day, since they may have ranked up (Diamond -> Master).

Detection is COLOUR-ONLY (no OCR), calibrated against the official tier art (tools/golden/data/badge/
ref/all_badges.png): peak hue Predator=4 (RED), Master=133 (PURPLE); Diamond=103, Platinum=92, Gold=22,
Bronze/Rookie=13-14 -- so Pred-red is cleanly separable from Bronze/Gold and Master-purple from
Diamond-blue. Once/day/streamer makes the cost negligible.
"""
import datetime
import json
import os
from collections import Counter

import cv2
import numpy as np

# Badge search region (fractions of frame), same top-right slot as detect_ranked.
_BX, _BY0, _BY1 = 0.88, 0.02, 0.18

# A pixel is "strong" badge colour when saturation & value are both high (0-255).
_SAT_MIN, _VAL_MIN = 90, 90

# Hue bands (OpenCV 0-180). RED wraps both ends. PURPLE sits at 124-147: above Diamond-blue (~103) with
# margin, below magenta/pink cosmetics (>150). Calibrated on the official tier art.
def _band_fracs(badge_bgr: np.ndarray) -> tuple[float, float]:
    """Return (red_fraction, purple_fraction) of the badge crop -- share of ALL pixels that are strong
    red / strong purple. Works on an already-cropped badge region (BGR)."""
    if badge_bgr is None or badge_bgr.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(badge_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    strong = (s > _SAT_MIN) & (v > _VAL_MIN)
    n = badge_bgr.shape[0] * badge_bgr.shape[1]
    if n == 0:
        return 0.0, 0.0
    red = float((((h <= 8) | (h >= 170)) & strong).sum()) / n
    pur = float((((h >= 124) & (h <= 147)) & strong).sum()) / n
    return red, pur


def _crop_badge(frame_bgr: np.ndarray) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    x0, y0, y1 = int(w * _BX), int(h * _BY0), int(h * _BY1)
    return frame_bgr[y0:y1, x0:w]


# Minimum fraction of the badge region that must be the tier colour to call it.
RED_FRAC = 0.015
PUR_FRAC = 0.015


def classify_frame(frame_bgr: np.ndarray) -> str:
    """Classify a single IN-GAME frame's rank badge: 'PRED' | 'MASTER' | 'OTHER'.

    The caller MUST ensure the frame is in-game (badge present) -- e.g. squads-left HUD detected --
    since menus/lobbies lack the badge and their red UI false-triggers PRED.
    """
    red, pur = _band_fracs(_crop_badge(frame_bgr))
    if pur >= PUR_FRAC and pur >= red:
        return "MASTER"
    if red >= RED_FRAC and red > pur:
        return "PRED"
    return "OTHER"


def aggregate(votes: list[str], min_votes: int = 3) -> str | None:
    """Aggregate per-in-game-frame classifications into a daily decision. Needs at least min_votes
    in-game reads to decide; returns None (=UNKNOWN, retry later) if too few in-game frames were seen
    (streamer stayed in lobby), so we DON'T cache 'not master' off a lobby-only sample."""
    votes = [v for v in votes if v in ("PRED", "MASTER", "OTHER")]
    if len(votes) < min_votes:
        return None
    tier, _ = Counter(votes).most_common(1)[0]
    return tier


def should_collect(tier: str | None) -> bool:
    return tier in ("MASTER", "PRED")


# --------------------------------------------------------------------------------------------------
# Daily per-streamer cache
# --------------------------------------------------------------------------------------------------
_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rank_cache.json")


def _today() -> str:
    return datetime.date.today().isoformat()


def _load() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def cached_tier(streamer: str) -> str | None:
    """Return today's cached tier for this streamer, or None if not classified today (or the cached
    entry is from a previous day -> re-classify, they may have ranked up)."""
    e = _load().get((streamer or "").lower())
    if e and e.get("date") == _today():
        return e.get("tier")
    return None


def set_tier(streamer: str, tier: str) -> None:
    """Persist today's classified tier for this streamer. Only call with a DECIDED tier (not None)."""
    d = _load()
    d[(streamer or "").lower()] = {"date": _today(), "tier": tier}
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=0, sort_keys=True)
    os.replace(tmp, _CACHE_PATH)
