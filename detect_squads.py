"""Read the 'N SQUADS LEFT' count from the top-right in-match HUD.

This is the observed-placement signal (bead TesseractApexOCR-hmz): when a squad is eliminated, the
squads-left value at that moment is its placement. Far better than the current kill-order inference
(match_detector.get_player_survival), which never observes placement. The game stops showing exact
counts at ~3-4 teams / ~10 players left, so the top few placements stay unreadable -- fall back to
inference there.

Validated 2026-07-11 on 6 fresh 1080p frames: squads count 6/6 correct (in-match 14/17/10/19; menu/
inventory frames correctly return None). Player-count is best-effort only (the region also contains
FPS/ping/RP numbers).
"""
import re

# Note: this module intentionally imports NOTHING from ocr.py -- ocr.py is the program entry point
# (__main__), so importing it back here creates a circular import that deadlocks at startup. The OCR
# capability is passed in as a callable instead (ocr_region), keeping this a leaf module.

# Generous top-right region (fractions of frame) that contains the 'N SQUADS LEFT [icon]NN' banner
# across the HUD-scale variation seen between streamers. The 'SQUADS' word anchors the count, so the
# surrounding FPS/ping/RP/badge noise in this box does not matter for the squads read.
REGION = (0.68, 0.0, 1.0, 0.16)   # x0, y0, x1, y1
# y1 extended 0.11 -> 0.16 (2026-07-12): when a player is AWAITING RESPAWN, the green respawn banner
# takes the top slot and pushes the 'N SQUADS LEFT' counter DOWN, out of the old 0.11 crop -- so the
# reader saw only 'AWAITING RESPAWN' and returned None for the whole respawn window (verified on real
# frames). The taller strip captures the counter in both its normal (high) and displaced (respawn)
# positions. Safe: the killfeed sits just below and never contains 'SQUADS LEFT', so the anchor-based
# extractor can't false-match it; extra OCR area is marginal at the ~8s read cadence.

# EasyOCR (with the killfeed-tuned apex.pth) heavily mangles the word "SQUADS": observed live as
# SOUADS, SDUADS, 3QUADS, 3UADS, MUADS, UAS, SQUADSLEF, etc. So anchor on the STABLE core "UA[D/S]"
# and take the 1-2 digit count that precedes it. Crucially, require whitespace + at most ~2 junk
# chars between the count and the token: this recovers the manglings (74% of live reads vs 54% for a
# strict 's?quad' match) WITHOUT grabbing a mangled leading 'S'->digit glued to the word as the count
# -- e.g. "20 3QUADS LEFT 60" must read 20, never 3. Validated offline on 340 real captured strings:
# no systematic false positives (residual off-by-one/tens-drop misreads are minority per stream and
# filtered by the caller's multi-read smoothing); all menu/respawn/browser junk stays None.
_SQUADS_RE = re.compile(r"(\d{1,2})\s+\S{0,2}?[uU][aA]\S{0,1}?(?:[dD]|[sS])", re.IGNORECASE)
_HUD_RE = re.compile(r"qua|uad|uas|left|lef|lft|eft|squa", re.IGNORECASE)


def read_squads_left(frame_bgra, ocr_region):
    """Return (squads_left:int|None, players_left:int|None, raw_ocr:str).

    ocr_region: callable(bgra_region) -> str  (the pipeline's preprocess+OCR of a crop).
    squads_left is None when the squads HUD is not present (menus, loading, endgame count hidden).
    Only squads_left is reliable; players_left is best-effort (region contains other numbers).
    """
    h, w = frame_bgra.shape[:2]
    x0, y0, x1, y1 = REGION
    region = frame_bgra[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
    if region.size == 0:
        return None, None, ""
    text = ocr_region(region)

    if not _HUD_RE.search(text):
        return None, None, text.strip()

    squads = players = None
    m = _SQUADS_RE.search(text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 20:                       # a BR lobby is 20 squads max
            squads = n
    if m:
        pm = re.search(r"(\d{1,3})", text[m.end():])
        if pm:
            p = int(pm.group(1))
            if 1 <= p <= 60:                   # 60 players max
                players = p
    return squads, players, text.strip()
