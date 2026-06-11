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
LOG_PATH = Path("killfeed_log.csv")
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
    "sang4tw":    "Sang",
    "apryze":     "Apryze",
    "gentburten": "Gent",
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
EVENT_WINDOW = 3.0  # seconds to group similar events
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
    "fridge", "samsung", "twitch", "subscribe", "crafting", "ready",
    "fall", "spotted", "akimbo", "heavy", "rounds", "elevator", "music",
    "connoisseur", "connnissaiir", "lonnolsseur",  # Typos
    "upgrades", "upgrade", "materials", "items", "supply",
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

# ==================== V2 CONFIG ====================
SAVE_CROPS = True                        # Enable crop saving during live capture
CROP_OUTPUT_DIR = Path("crops")           # Root dir; streamer subdirs created automatically
CROP_DEDUP_WINDOW = 30.0                  # Seconds within which duplicate crops are suppressed
CROP_PHASH_THRESHOLD = 8                  # Max Hamming distance to consider two crops identical

USE_TROCR = True                         # Use fine-tuned TrOCR instead of Tesseract
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

# ==================== HAIKU VALIDATION ====================
HAIKU_VALIDATE       = True  # Real-time Haiku vision validation for low-confidence kills
HAIKU_CONF_THRESHOLD = 0.6   # Validate Kill events where victim_conf < this value

# ==================== LANGUAGE DETECTION ====================
NON_ENGLISH_DETECTION  = True  # Detect streams with non-Latin script (CJK, Cyrillic, etc.)
NON_ENGLISH_THRESHOLD  = 5     # Consecutive non-Latin OCR lines before flagging channel
SKIP_NON_ENGLISH_CROPS = True  # Suppress crop saving for flagged channels

# ==================== AUTO-DISCOVERY ====================
STREAM_REFRESH_INTERVAL = 1800  # Seconds between periodic top-stream re-queries (--top mode)
TOP_STREAMS_COUNT       = 20    # Default number of top Apex streams to watch when no channels specified
