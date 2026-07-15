"""Configuration file for Apex Legends killfeed OCR system."""

from pathlib import Path
import os as _os

# Auto-load .env file if present (for TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _os.environ.setdefault(_k.strip(), _v.strip())

# ==================== FILE PATHS ====================
LOG_PATH         = Path("killfeed_log.csv")   # legacy CSV — kept for reference
KILLFEED_DB_PATH = Path("killfeed.db")         # primary SQLite event log

PLAYER_DB_PATH = Path("player_names.json")
LEGEND_TYPO_DB_PATH = Path("legend_typos.json")

# ==================== TESSERACT SETTINGS ====================
# Uncomment and set this if tesseract.exe is not on PATH
# TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# (TESSERACT_CONFIG is set at the bottom based on USE_CUSTOM_APEX_MODEL)

# ==================== STREAM CAPTURE ====================
# Streams to never watch (VOD reruns, bots, non-gameplay, etc.)
STREAM_BLOCKLIST = {
    "hiswattson247",  # VOD rerun channel
}

TWITCH_CHANNELS = {
    "faide":      "Faide",
    "sang":       "Sang",       # renamed from sang4tw
    "apryze":     "Apryze",
    "gent":       "Gent",       # renamed from gentburten
    "hiswattson": "HisWattson",
    "shivfps":    "ShivFPS",
}
STREAM_QUALITY = "best"          # streamlink quality: "best", "1080p60", "720p60"
FRAME_PROCESS_INTERVAL = 0.5     # seconds between processed frames per channel

# ==================== STREAMER DETECTION ====================
STREAMER_ALIASES = {
    "sang": "Sang",
    "seng": "Sang",
    "apryze": "Apryze",
    "aprize": "Apryze",
    "apryse": "Apryze",
    "faide": "Faide",
    "day 8 roller": "Faide",
    "day8roller": "Faide",
    "faide roller": "Faide",
    # Gent variations
    "gent": "Gent",
    "gentburten": "Gent",
    "twitchvgent": "Gent",
    "twitchtvgent": "Gent",
    "wwwtwitchtvgent": "Gent",
    "httpstwitchtvgent": "Gent",
    # HisWattson variations
    "hiswattson": "HisWattson",
    "hiswattson247": "HisWattson",
    "twitchvhiswattson": "HisWattson",
    "twitchtvhiswattson": "HisWattson",
    "hiswattson247twitch": "HisWattson",
    "hiswattsor": "HisWattson",
    "twitchtvhiswattsor": "HisWattson",
    "wwwtwitchtvhiswattsor": "HisWattson",
    "httpstwitchtvhiswattsor": "HisWattson",
    "hittpswwwtwitchtvhiswattsor": "HisWattson",
    # ShivFPS variations - ADD THIS
    "shivfps": "ShivFPS",
    "shiv": "ShivFPS",
    "shivfos": "ShivFPS",
    "shivfbs": "ShivFPS",
    "twitchtvshivfps": "ShivFPS",
    "twitchtvshiv": "ShivFPS",
    "wwwtwitchtvshivfps": "ShivFPS",
    "httpstwitchtvshivfps": "ShivFPS",
}

PLAYER_NAME_CORRECTIONS = {
    "shoes": "Sihess",
    "sheet": "Sihess",
    "sheess": "Sihess",
    "shes": "Sihess",
    "sheets": "Sihess",
    "shiess": "Sihess",
}



# ==================== KILLFEED COORDINATES ====================
# Killfeed coordinates per streamer
STREAMER_KILLFEED_CONFIGS = {
    "Sang": [
        {"left": 1740, "top": 332, "width": 550, "height": 41},
        {"left": 1741, "top": 375, "width": 549, "height": 33},
        {"left": 1773, "top": 410, "width": 519, "height": 40},
        {"left": 1588, "top": 451, "width": 701, "height": 37},
    ],
    "Apryze": [
        {"left": 1661, "top": 309, "width": 626, "height": 35},
        {"left": 1615, "top": 345, "width": 673, "height": 34},
        {"left": 1572, "top": 379, "width": 717, "height": 35},
        {"left": 1674, "top": 414, "width": 615, "height": 33},
    ],
    "Faide": [
        {"left": 1707, "top": 309, "width": 582, "height": 36},
        {"left": 1600, "top": 345, "width": 689, "height": 35},
        {"left": 1556, "top": 380, "width": 734, "height": 33},
        {"left": 1596, "top": 414, "width": 694, "height": 35},
    ],
    "Gent": [
        {"left": 1707, "top": 309, "width": 582, "height": 36},
        {"left": 1600, "top": 345, "width": 689, "height": 35},
        {"left": 1556, "top": 380, "width": 734, "height": 33},
        {"left": 1596, "top": 414, "width": 694, "height": 35},
    ],
    "HisWattson": [
        {"left": 1488, "top": 332, "width": 800, "height": 41},
        {"left": 1456, "top": 374, "width": 832, "height": 37},
        {"left": 1537, "top": 411, "width": 752, "height": 39},
    ],
    # ADD THIS - ShivFPS (4 lines)
    "ShivFPS": [
        {"left": 1493, "top": 333, "width": 794, "height": 38},
        {"left": 1460, "top": 372, "width": 827, "height": 38},
        {"left": 1480, "top": 410, "width": 807, "height": 38},
        {"left": 1469, "top": 449, "width": 819, "height": 40},
    ],
}
# NOTE: STREAMER_KILLFEED_CONFIGS above is never imported/used anywhere in the codebase.
# It was calibrated at 2560x1440 (right edges all land ~2287-2292px), not 1080p as the
# comment below in detect_killfeed.py once assumed. STREAMER_SEARCH_ZONES below converts
# it to resolution-independent fractions and is what detect_killfeed.py actually consumes.
# Kept here unused for reference / in case it needs re-deriving.

# Per-streamer killfeed search-zone override, as fractions of (content_width, frame_height).
# Converted from STREAMER_KILLFEED_CONFIGS (divide left/width by 2560, top/height by 1440),
# then padded: ~4% left margin, ~1.5% top margin, and bottom extended by one extra
# line-height's worth to allow growth to a 5th killfeed line. Verified against a fresh 1080p
# VOD for Apryze (2026-07-01) — converted y-range (0.200-0.337) matched the real observed
# killfeed rows (0.190-0.310) far better than the un-converted 1080p-assumed fractions did.
# Streamers not in this dict fall back to the generic _SEARCH_X_FRAC/_SEARCH_Y0_FRAC/
# _SEARCH_Y1_FRAC box in detect_killfeed.py (e.g. unconfigured/ad-hoc streamers).
# Apryze's y1_frac was tightened from the formula-derived 0.337 to 0.30 after live
# verification showed real killfeed content ending ~0.25 but a "SUB GOAL 0/20" webcam
# overlay widget starting to bleed in at ~0.303 — 0.30 leaves ~4-line headroom above the
# zone's y0 while staying below the confirmed contamination point.
# ShivFPS's x0_frac was pulled back from the formula-derived 0.530 (4% left pad) to 0.560
# (1% pad) after live verification showed the wider box occasionally catching a world-space
# ping/waypoint distance marker (e.g. "297M") that happened to sit at x~0.64 in one test
# frame. Unlike the squad-counter/FPS-diagnostic HUD (fixed screen position, see
# _HUD_EXCLUDE_ZONES in detect_killfeed.py), in-world ping markers move with camera angle and
# can't be excluded by a fixed mask — this residual risk is accepted and left to downstream
# OCR-text-plausibility filtering (e.g. PLAYER_NAME_MAX_DIGIT_RATIO already rejects
# digit-heavy garbage like "297M"), not something detection alone can fully solve. The
# left-clipping concern that motivated widening (Finding 2) was evidenced for Apryze
# specifically, not confirmed for ShivFPS, so less padding is justified here.
# Faide's y0_frac was corrected from the formula-derived 0.200 to 0.12 after live
# verification (2026-07-01 VOD spot-check) showed the converted-from-1440p top boundary was
# STILL wrong for this streamer specifically — real killfeed text ("pineappledude <GUN_ICON>
# [TKTK] MissKillaQueen0") measured at y~0.13, well above 0.200, meaning the zone was clipping
# the top line entirely (a false negative introduced by the conversion, not fixed by it).
# y1 generously widened to 0.30 to compensate for the uncertainty this discrepancy implies.
# Gent shares byte-identical calibration source data with Faide in STREAMER_KILLFEED_CONFIGS
# (literally the same 4 line entries) — this looks like it may have been copy-pasted rather
# than independently calibrated. The same y0/y1 correction is applied here as a reasonable
# inference, but UNVERIFIED — Gent's Twitch account (gentburten) no longer resolves via the
# Helix API (likely renamed/deleted), so this streamer cannot currently be live-spot-checked
# at all. Flag for re-verification if/when a working channel name is found.
# Sang's y0_frac was ALSO corrected from the formula-derived 0.216, for the same reason as
# Faide (2026-07-01 VOD spot-check): real content ("Ghostttt_KR pinged loot: [Hemlok Breach
# AR].") measured at y~0.19-0.21 (936p stream — Sang does not broadcast at 1080p), so the old
# boundary clipped it; the only region the old zone found there was empty background. This is
# now a confirmed PATTERN (2 of 3 empirically-tested streamers had this exact top-clipping
# issue), not a one-off — treat HisWattson's still-unverified 0.216 with real suspicion too.
STREAMER_SEARCH_ZONES = {
    "Sang":       {"x0_frac": 0.580, "x1_frac": 1.00, "y0_frac": 0.160, "y1_frac": 0.320},
    "HisWattson": {"x0_frac": 0.529, "x1_frac": 1.00, "y0_frac": 0.216, "y1_frac": 0.339},
    "ShivFPS":    {"x0_frac": 0.560, "x1_frac": 1.00, "y0_frac": 0.216, "y1_frac": 0.366},
}
# "Apryze" and "Gent" entries REMOVED 2026-07-02: a 155-crop hand-labeled audit found these
# hardcoded zones catching mostly non-killfeed content (Twitch chat overlay + promo carousel
# for Apryze, ping banners/stream title card/unrelated app UI for Gent) -- 67-82% garbage, not
# the "human-verified, safest" zones they were assumed to be earlier this session. Both now
# fall through to Gemini auto-calibration instead (calibrate_zone.py), which measured 87-100%
# real-content rate on every other streamer in the same audit, including ones where calibration
# itself failed and fell back to the generic box. "Faide" also removed pre-emptively: it shared
# these EXACT coordinates with Gent's broken zone (not a coincidence) but was offline all
# session so could not be independently verified -- treat with the same suspicion until tested.
# Re-add only after a fresh hand-verified spot-check, not a blind re-derivation.

# ==================== AUTO-CALIBRATION (CLAUDE VISION) ====================
# For streamers with no STREAMER_SEARCH_ZONES entry, run a bounded, self-verifying
# vision calibration loop (see calibrate_zone.py) instead of always falling back to
# the loose generic box in detect_killfeed.py. Grounded in real detect_for_stream() output —
# the vision model classifies candidate regions, it does not invent pixel coordinates from scratch.
# Vision runs on Claude (Anthropic): ANTHROPIC_API_KEY is read from .env. (The Gemini free tier
# it originally used was retired; only this classifier moved to Claude — the async OCR-correction
# queue in gemini_queue.py is a separate system and is unaffected.)
CALIBRATE_VISION_MODEL       = "claude-sonnet-5"  # Anthropic model for region classification
# Re-enabled 2026-07-09 after adding the COVERAGE GUARD (see KILLFEED_TOP_MAX_FRAC below and
# _derive_zone_from_regions): candidate zones that anchor below the killfeed's fixed upper band
# (the facecam/HUD mislabel that used to drop kills) are now rejected, and zone height is capped.
# Cache was cleared so every zone re-derives with the guard.
AUTO_CALIBRATE_ZONES         = True    # Master toggle; False = generic-box-only behavior
# Live in-loop calibration: an unconfigured streamer starts on the generic box and fires ONE
# Claude classification roughly this often (only on frames that actually have killfeed candidates)
# until a clean killfeed is captured, then the tight zone is locked in live and polling stops.
AUTO_CALIBRATE_INTERVAL_SECONDS = 60   # Seconds between live calibration attempts while uncalibrated
AUTO_CALIBRATE_MAX_ATTEMPTS     = 10   # Give up for THIS run after this many attempts (caps cost
                                       # ~10 calls/streamer/day); stays on generic box and keeps
                                       # collecting. Not cached as a bypass — a fresh run (next day)
                                       # retries calibration from scratch.
# A single frame can mislead: on a sparse/empty feed the detected boxes can land on a facecam and
# be mislabeled 'killfeed'. So a zone is only LOCKED after TWO independent attempts (>= a minute
# apart) agree on an overlapping zone — a one-off mislabel won't reproduce and lock. Until then the
# streamer stays on the generic box.
AUTO_CALIBRATE_MIN_KILLFEED_LINES = 2  # Min killfeed regions in one frame for that attempt to count
AUTO_CALIBRATE_CONFIRM_OVERLAP    = 0.5  # Min y-overlap fraction between two attempts to confirm
# COVERAGE GUARD: the Apex killfeed always STARTS in the upper-right band (hand-verified zones top
# out at y0=0.216). When the vision model mislabels a facecam/HUD element as killfeed on a sparse
# feed, the derived zone anchors BELOW the real killfeed (measured mis-anchors: y0 0.28-0.42) and
# silently drops kills. So reject any candidate whose topmost killfeed region starts below
# KILLFEED_TOP_MAX_FRAC, and cap total zone height at KILLFEED_MAX_SPAN_FRAC so it can't extend
# down into the facecam. Grounded in the fixed screen position of the killfeed, which no
# classification error can move.
KILLFEED_TOP_MAX_FRAC   = 0.26   # Reject calibration if killfeed appears to start below this
KILLFEED_MAX_SPAN_FRAC  = 0.24   # Max calibrated zone height (hand-verified zones span <= ~0.20)
# Generous default zone height: kills stack DOWNWARD from a fixed top, so a quiet 1-2 line catch
# must still cover a full multi-kill burst — a too-tight y1 would silently drop later lines. y1 =
# max(observed bottom, y0 + this), then pulled up only by a contaminant actually detected below.
# ~0.16 matches the hand-verified STREAMER_SEARCH_ZONES spans (Sang 0.16, ShivFPS 0.15).
AUTO_CALIBRATE_KILLFEED_HEIGHT_FRAC = 0.17
CALIBRATION_CACHE_PATH       = Path("calibration_cache.json")  # Persisted successful auto-zones
AUTO_CALIBRATE_TIME_BUDGET   = 45.0    # Hard wall-clock ceiling (seconds) per streamer, per run
AUTO_CALIBRATE_MAX_ROUNDS    = 3       # Max propose/verify rounds before giving up
AUTO_CALIBRATE_MAX_CONCURRENT = 2      # Max ChannelWorkers calibrating via Gemini simultaneously
AUTO_CALIBRATE_PAD_X_FRAC    = 0.015   # Horizontal headroom added when deriving zone from regions
AUTO_CALIBRATE_PAD_Y_FRAC    = 0.015   # Vertical headroom added when deriving zone from regions
AUTO_CALIBRATE_RANKED_WAIT_FRAMES = 30 # Frames sampled while waiting to confirm ranked gameplay
AUTO_CALIBRATE_VERIFY_SAMPLES = 2      # Extra time-spaced re-checks of a seemingly-clean zone
                                        # before trusting it — one clean frame isn't proof a
                                        # persistent overlay (e.g. a sub-goal bar, browser
                                        # watermark) won't show up in the very next sample.
                                        # If contamination persists through the full round
                                        # budget, the streamer is bypassed (not OCR'd at all)
                                        # rather than falling back to the noisy generic box —
                                        # see calibrate_zone.py's bypass cache.

# ==================== APEX LEGENDS ====================
# Canonical legend names
APEX_LEGENDS_CANONICAL = {
    "Octane", "Bangalore", "Valkyrie", "Bloodhound", "Wraith", "Pathfinder",
    "Lifeline", "Caustic", "Mirage", "Gibraltar", "Wattson", "Crypto",
    "Revenant", "Loba", "Rampart", "Horizon", "Fuse", "Seer", "Ash",
    "MadMaggie", "Newcastle", "Vantage", "Catalyst", "Ballistic", "Alter", "Conduit"
}

# Default legend typo mappings
DEFAULT_LEGEND_TYPOS = {
    "maggies": "MadMaggie",
    "maggieb": "MadMaggie",
    "maggie": "MadMaggie",
    "magie": "MadMaggie",
    "bangaiore": "Bangalore",
    "bangalare": "Bangalore",
    "valkrie": "Valkyrie",
    "bioodhound": "Bloodhound",
    "wraich": "Wraith",
    "pathfnder": "Pathfinder",
    "lifelne": "Lifeline",
    "causitc": "Caustic",
    "mriage": "Mirage",
    "gibralter": "Gibraltar",
    "watson": "Wattson",
    "crypta": "Crypto",
    "revanant": "Revenant",
    "ramprt": "Rampart",
    "horzon": "Horizon",
    "newcastie": "Newcastle",
    "vantge": "Vantage",
    "catalst": "Catalyst",
    "balistic": "Ballistic",
    "condiut": "Conduit",
}

# ==================== PLAYER NAME SETTINGS ====================
PLAYER_NAME_MIN_LENGTH = 4
PLAYER_NAME_MAX_DIGIT_RATIO = 0.4  # reject names where >40% of chars are digits
FUZZY_MATCH_THRESHOLD = 0.85
LEGEND_FUZZY_THRESHOLD = 0.80
NAME_CONFIDENCE_THRESHOLD = 3  # Need 3+ sightings to become canonical

# ==================== EVENT TRACKING ====================
EVENT_WINDOW = 6.0  # seconds to group similar events -- was 3.0, too short relative to how long
                     # killfeed banners actually stay visible (revive sequences, queued kills),
                     # causing premature flush + re-detection of the same still-visible event as
                     # "new" (confirmed: one banner produced 109 DB rows over 44s). Still a soft
                     # window, not a guarantee -- db_log.py's DEDUP_WINDOW_SECONDS is the real
                     # safety net at the DB layer.

# ---- Persistence-aware icon vote (kill/knock) — see DESIGN_persistence_aware_icon_vote.md ----
# All default OFF: the merge layer keeps the legacy per-marker split until thresholds are
# calibrated on labeled data (per-read icon distributions are not in the DB). Do NOT enable on a
# real run without the calibration step in the design doc.
ICON_VOTE_ENABLED   = False  # Layer 1: merge kill/knock reads of one line, decide label by vote
ICON_KILL_MIN_RUN   = 3      # contiguous kill-icon reads required to call a merged line a Kill
ICON_KILL_MIN_FRAC  = 0.50   # kill-icon reads as a fraction of the merged line's marker-reads
ICON_VOTE_LOG       = True   # instrument: log per-flushed-track icon tallies (safe with vote off)
STICKY_CHAIN_MERGE_TYPES = False  # Layer 2: key db_log sticky chain by name-pair, not event_type
# Stem-aware multikill guard: when sticky suppression would drop an ELO row, KEEP it if its victim
# shares a kept victim's alpha-stem but has clearly different digits (a default-name multikill like
# gibraltar2127 vs gibraltar1619 -- one attacker beaming two different default-named players fast).
# Measured (scratch/measure, 2026-07-10): re-admits only 3 rows on run2 (keeps 99.7% of
# sticky-catching) while protecting the genuine multikill. Off by default: plain cap=2 is the
# validated baseline; residual risk is a number-garbling sticky whose victim-digits jitter (leaks
# ~1 row per distinct garble). See db_log._distinct_default_name_victim.
SAME_VICTIM_GUARD = False

SAVE_INTERVAL = 30  # Save database every 30 seconds
STREAMER_CHECK_INTERVAL = 60  # Check streamer name every 60 seconds

# ==================== OCR PREPROCESSING ====================
BRIGHTNESS_THRESHOLD = 180  # Threshold for isolating bright text
EMPTY_LINE_VARIANCE = 50  # Variance threshold for empty lines
MIN_TEXT_LENGTH = 8  # Minimum text length to not be noise
MIN_ALPHA_RATIO = 0.6  # Minimum alphanumeric ratio

# ==================== COMMON WORDS ====================
# Words to filter out from player names
COMMON_WORDS = [
    "spotted", "pinged", "enemy", "reviving", "care", "package",
    "broken", "bleed", "out", "shield", "dibs", "extended", "light",
    "mag", "level", "suggested", "location", "canceled", "ping",
    "revealed", "enemies", "map", "audio", "here", "you", "the", "ring",
    "word", "meow", "containing", "eliminated", "leader", "kill", "new",
    "with", "kills", "looted", "that", "contained", "has", "been", "champion",
    "are", "and", "guy", "looking", "people", "compromise", "compromised",
    "lacation", "focation", "area", "over", "defend", "avoid", "this",
    "looting", "from", "full", "beam", "for", "ane", "tee", "ees", "since", "ram",
    # ADD THESE:
    "fridge", "samsung", "subscribe", "crafting", "ready",
    "fall", "spotted", "akimbo", "heavy", "rounds", "elevator", "music",
    "connoisseur", "connnissaiir", "lonnolsseur",  # Typos
    "upgrades", "upgrade", "materials", "items", "supply",
    "directly", "clearly", "getting", "gettingt", "pushed",
    # Game notifications that slip through as player names
    "killer", "lvl", "knocked", "finisher", "squad", "teammate",
    # Ping misparse artifacts
    "shit", "haqqn", "haqqq",
    # Ping notification words ("Attack here", "Defend here", etc.)
    "attack", "hitted", "defend", "watch",
    # OCR noise fragments (not valid player names)
    "tion", "elem", "epher", "headshot", "unreadable", "stream", "gettingshotun",
]

# ==================== OCR TYPO CORRECTIONS ====================
TYPO_MAP = {
    "shiald": "shield",
    "shieid": "shield",
    "shietd": "shield",
    "shiiald": "shield",
    "shigi": "shield",
    "shisit": "shield",
    "shigld": "shield",
    "brpken": "broken",
    "braken": "broken",
    "brnken": "broken",
    "brokan": "broken",
    "enpmy": "enemy",
    "enomy": "enemy",
    "ehemy": "enemy",
    "enamy": "enemy",
    "anenemy": "an enemy",
    "ananemy": "an enemy",
    "ansnemy": "an enemy",
    "aneriemy": "an enemy",
    "spottedansnemy": "spotted an enemy",
    "spottedan enemy": "spotted an enemy",
    "spottedanenery": "spotted an enemy",
    "spottedanenen": "spotted an enemy",
    "spottedanenamy": "spotted an enemy",
    "spottedaneriemy": "spotted an enemy",
    "spottedananemy": "spotted an enemy",
    "spottedanenem": "spotted an enemy",
    "spatted": "spotted",
    "spottad": "spotted",
    "pingedtoot": "pinged loot",
    "pingedloot": "pinged loot",
    "suggestedalocation": "suggested a location",
    "suggesteda location": "suggested a location",
    "suggestedalacation": "suggested a location",
    "suggestedalecation": "suggested a location",
    "suggestedalocatian": "suggested a location",
    "suggestedalocatiom": "suggested a location",
    "suggasteda": "suggested a",
    "sugdsted": "suggested",
    "suiggesteda": "suggested a",
    "donotreviveme": "do not revive me",
    "donctreviveme": "do not revive me",
    "itisnotsafe": "it is not safe",
    "itisnot safe": "it is not safe",
    "bleedqut": "bleed out",
    "bleedgut": "bleed out",
    "bleeddut": "bleed out",
    "bleeddout": "bleed out",
    "blaedout": "bleed out",
    "bieedout": "bleed out",
    "blsedout": "bleed out",
    "ieleed": "bleed",
    "revealedenemies": "revealed enemies",
    "enemyaudio": "enemy audio",
    "enemyaudiohere": "enemy audio here",
    "reviwing": "reviving",
    "pingedloot": "pinged loot",
    "pingedtoot": "pinged loot",
    "pingedtont": "pinged loot",
    "pingedioont": "pinged loot",
    "pingedlant": "pinged loot",
    "pingedlook": "pinged loot",
    "pingedfoat": "pinged loot",
    "pingedioat": "pinged loot",
    # "upgrades" OCR variants (crafting notification leaking as attacker name)
    "upgaoes": "upgrades",
    "upgr40es": "upgrades",
    "urga0e5": "upgrades",
    "upgr4065": "upgrades",
    "urga1065": "upgrades",
    "uffgaoes": "upgrades",
    "upgr4oes": "upgrades",
    # "spotted" OCR variants
    "skyted": "spotted",
    "skyed": "spotted",

}

# ==================== TEMPORAL CLUSTERING ====================
TEMPORAL_WINDOW = 10.0  # seconds - names within this window get higher tolerance
TEMPORAL_THRESHOLD = 0.70  # Lower threshold for recent names (vs 0.85 default)

# ==================== CUSTOM TESSERACT MODEL ====================
USE_CUSTOM_APEX_MODEL = False

if USE_CUSTOM_APEX_MODEL:
    TESSERACT_CONFIG = "--oem 3 --psm 7 -l apex"
else:
    TESSERACT_CONFIG = "--oem 3 --psm 7 -l eng"

# ==================== SQUADS-LEFT / OBSERVED PLACEMENT (bead hmz) ====================
# Read the top-right 'N SQUADS LEFT' HUD to observe placement directly instead of inferring it from
# kill-order (match_detector.get_player_survival). Reader validated 6/6 on fresh frames (detect_squads).
# Currently LOG-ONLY: it tracks the per-stream squads-left time series so the monotonic-decrease
# behavior and killfeed correlation can be validated live before wiring placement into the ELO engine
# (that step also needs the squad-wipe boundary from bead 0ef).
SQUADS_TRACK_ENABLED          = True
SQUADS_TRACK_INTERVAL_SECONDS = 8        # per-stream cadence; bounds the extra OCR cost
SQUADS_MIN_FRAME_HEIGHT       = 900      # skip squads reads below this vertical res (720p etc.): the
                                         # small HUD counter OCRs too poorly on low-res streams to trust
                                         # (verified: a 720p 'N SQUADS LEFT' reads as '81sobs LeRT'),
                                         # and a false decrement corrupts an ELO placement.

# ==================== V2 CONFIG ====================
SAVE_CROPS = True                        # Enable crop saving during live capture
CROP_OUTPUT_DIR = Path("crops")           # Root dir; streamer subdirs created automatically
CROP_DEDUP_WINDOW = 30.0                  # Seconds within which duplicate crops are suppressed
CROP_PHASH_THRESHOLD = 8                  # Max Hamming distance to consider two crops identical

# ==================== MATCH-BOUNDARY FRAME CAPTURE (data collection) ====================
# Matches are currently segmented purely by a kill-event time gap (see match_detector.GAP_SECONDS),
# which over-fragments real games during long lulls (looting/rotating with no kills). The plan is
# to instead detect actual game-UI state transitions -- "YOUR SQUAD HAS BEEN ELIMINATED", the
# post-match placement/summary screen, the main lobby/menu -- as ground-truth match boundaries.
# No classifier exists for these yet; this is step 1, collecting raw full-frame examples to look at
# before designing one (OCR keyword search vs extending calibrate_zone.py's Claude vision
# classifier -- undecided, see KNOWN_ISSUES.md / bd issue for match-boundary detection).
SAVE_BOUNDARY_FRAMES = False                      # Master toggle for this capture step
# Disabled 2026-07-11: the collection run (step 1) is complete — the gameplay-vs-not-gameplay binary
# design was validated against the captured sample (see bd TesseractApexOCR-0ef). Re-enable only if a
# fresh full-frame sample is needed (e.g. to train/eval the eventual vision classifier).
BOUNDARY_FRAME_INTERVAL_SECONDS = 20              # Per-streamer cadence; keeps disk/volume bounded
BOUNDARY_FRAME_DIR = Path("boundary_frames")      # Root dir; streamer subdirs created automatically

# ==================== EASYOCR ====================
USE_EASYOCR = True                       # Use EasyOCR (CRAFT + CRNN) as the primary local OCR engine
EASYOCR_LANGUAGES = ['en']               # Languages for EasyOCR reader (add 'ko', 'ja' if needed)
EASYOCR_GPU = True                       # Use GPU if available; auto-falls back to CPU
EASYOCR_GAP_THRESHOLD = 80               # Horizontal pixel gap to insert <GUN_ICON> between words
EASYOCR_CUSTOM_MODEL_DIR = Path("models/easyocr_custom")  # Path to user_network_directory containing apex.pth

# ==================== PREDATOR LEADERBOARD REFRESH ====================
# apex_ranked_leaderboard.csv (ground truth of current Predator names, seeded as protected
# 'pro' entries at pipeline start) is refreshed from apexlegendsstatus.com by
# update_leaderboard.py. The site sits behind Cloudflare, which permanently blocks headless
# browsers -- the refresh drives a HEADED Edge window for ~10-15s. With auto-refresh on,
# ocr.py runs the (age-gated) refresh at startup, right before pro seeding; a stale or
# failed scrape falls back to the existing CSV and never blocks the pipeline.
LEADERBOARD_AUTO_REFRESH   = True
LEADERBOARD_MAX_AGE_HOURS  = 24     # refresh only when the CSV is older than this

# Knock vs kill distinction (2026-07-04). The Apex killfeed marks an actual elimination with a
# small RED skull glyph next to the victim's name; a knockdown line shows only the weapon icon.
# (A separate ORANGE circular badge marks the kill leader on either line type -- not a kill
# signal.) When the EasyOCR path has the original color crop, it checks each icon gap for the
# red skull and emits <KILL_ICON> instead of <GUN_ICON>. With this flag on, parsers.py maps
# skull lines to event_type='Kill' and plain gap lines to 'Knock'; match_detector/elo only
# consume 'Kill', so knockdowns no longer inflate kill counts. Only meaningful for the EasyOCR
# path -- TrOCR/Tesseract paths never emit <KILL_ICON>, so leave this False if reverting to
# those engines or every kill would be classified as a knock.
KNOCK_KILL_DISTINCTION = True

# ==================== TROCR (DISABLED) ====================
USE_TROCR = False                        # Use fine-tuned TrOCR instead of Tesseract (DISABLED)
TROCR_MODEL_PATH = Path("models/trocr_apex")  # Path to saved fine-tuned model + processor
TROCR_CONF_THRESHOLD = 0.30              # Min mean per-token confidence to accept TrOCR output (0–1)
SAVE_NOISE_CROPS = True                  # Save noise/low-conf crops for negative training examples
NOISE_CROP_OUTPUT_DIR = Path("crops_noise")  # Root dir for noise crops

# ==================== KILLFEED DETECTION ====================
DETECT_N_FRAMES          = 60    # Frames to sample per detection attempt (~1s at 60fps)
DETECT_BRIGHTNESS_THRESH = 190   # Pixel grayscale value to count as "bright text" (0-255)
DETECT_MIN_LINES         = 1     # Minimum detected lines to consider detection successful
DETECT_MAX_ATTEMPTS      = 50    # Hard cap on detection attempts before giving up and rotating

# ==================== RANKED DETECTION ====================
RANKED_ONLY_CROPS        = True   # Only save crops when a ranked badge is detected
RANKED_MIN_SAT_FRAC      = 0.05   # Min fraction of badge region with saturated pixels
RANKED_STREAMS_ONLY      = True   # Skip OCR while streamer is not in a ranked game
RANKED_CHECK_FRAMES      = 3      # Frames sampled per ranked check during detection
RANKED_NOT_RANKED_WAIT   = 120    # Seconds to wait when not in ranked before retrying
RANKED_NOT_RANKED_STREAK = 3      # Consecutive not-ranked checks before waiting
RANKED_MAX_WAIT_CYCLES   = 5      # Max wait cycles before giving up and rotating to next streamer

# Master/Predator-only collection (bead 2mo). When True, the ranked-stream gate additionally requires
# the streamer to be MASTER or PREDATOR (badge colour: purple / red -- see rank_gate.py), classified
# ONCE per day per streamer from IN-GAME frames and cached (rank_cache.json). Below-Master streamers
# are re-checked the next day (they may rank up). Requires RANKED_STREAMS_ONLY.
MASTER_PRED_ONLY         = True   # Only collect Master/Predator streamers
MASTER_PRED_MAX_FRAMES   = 40     # Frames to sample per daily classification before giving up
MASTER_PRED_MIN_INGAME   = 3      # In-game frames (squads-HUD present) needed to decide a tier
# Debug: save each classified streamer's badge crop + red/purple fractions to rank_samples/ (incl. the
# OTHER bucket -> Diamond/Plat/Gold), so a collection run passively builds a labelled cross-rank set to
# validate the purple/blue boundary. Turn off once the boundary is confirmed.
RANK_GATE_SAMPLE_LOG     = True

# ==================== GEMINI VALIDATION ====================
GEMINI_VALIDATE           = False  # DISABLED 2026-07-09: Gemini free tier is dead (429s); leaving
                                   # it on just hammers a dead API and spams errors every kill. The
                                   # calibration classifier uses Claude separately (CALIBRATE_VISION_MODEL).

# Async queue settings
GEMINI_QUEUE_COOLDOWN    = 12.0   # Seconds between API calls (5 RPM free tier, confirmed via
                                   # AI Studio dashboard 2026-07-02 -- NOT 15 RPM as previously
                                   # assumed here. Free tier is also capped at 20 RPD; this
                                   # cooldown alone does not enforce that daily cap.
GEMINI_QUEUE_MAX_SIZE    = 200    # Max pending crops before dropping (workers never block)
GEMINI_AGREE_THRESHOLD   = 0.85  # SequenceMatcher ratio to count TrOCR + Gemini as "agreed"
GEMINI_CORRECTION_DIR    = Path("labels/gemini_corrections")  # Crops where Gemini disagrees
GEMINI_CONFIRMED_DIR     = Path("labels/gemini_confirmed")    # Crops where both agree

# ==================== LANGUAGE DETECTION ====================
# Restrict auto-discovery to a single Twitch broadcaster language at the SOURCE (Helix /streams
# `language` filter, BCP-47 code e.g. "en"). Set to None/"" to watch all languages. Chosen "en"
# 2026-07-10: non-English clients render game-UI banners in that language (e.g. Japanese
# "敵を11回スキャン"), which breaks OCR-keyword match-boundary detection and pollutes the
# leaderboard with non-EN-region play. This gates stream SELECTION only; the reactive
# NON_ENGLISH_DETECTION below is a second line of defense for killfeed CONTENT that slips through
# (a broadcaster tagged "en" can still have CJK/Cyrillic player names in the feed).
STREAM_LANGUAGE        = "en"  # Twitch broadcaster_language filter for --top discovery; None = all
NON_ENGLISH_DETECTION  = True  # Detect streams with non-Latin script (CJK, Cyrillic, etc.)
NON_ENGLISH_THRESHOLD  = 5     # Consecutive non-Latin OCR lines before flagging channel
SKIP_NON_ENGLISH_CROPS = True  # Suppress crop saving for flagged channels

# ==================== AUTO-DISCOVERY ====================
STREAM_REFRESH_INTERVAL = 1800  # Seconds between periodic top-stream re-queries (--top mode)
TOP_STREAMS_COUNT       = 20    # Default number of top Apex streams to watch when no channels specified
LIVENESS_CHECK_INTERVAL = 120   # Seconds between liveness sweeps (all modes) — catches streams that
                                 # ended mid-session without the demux loop raising, which otherwise
                                 # silently freezes OCR on the last on-screen content indefinitely
                                 # (confirmed 2026-07-01: Apryze's worker stayed "alive" and kept
                                 # OCR-ing/parsing a stale post-match results screen for 25+ minutes
                                 # after Twitch confirmed the stream had actually ended).
