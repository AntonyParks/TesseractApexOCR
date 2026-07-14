"""Killfeed parsing and text normalization."""

import re
from typing import Dict, Optional, Tuple

from config import (
    COMMON_WORDS, TYPO_MAP, PLAYER_NAME_MIN_LENGTH, APEX_LEGENDS_CANONICAL,
    KNOCK_KILL_DISTINCTION
)


# Weapon and attachment keywords to filter out
WEAPON_KEYWORDS = [
    'hcog', 'bruiser', 'rounds', 'mag', 'stock', 'barrel',
    'optic', 'scope', 'laser', 'r-99', 'r-301', 'wingman',
    'prowler', 'volt', 'devotion', 'havoc', 'flatline',
    'hemlock', 'ranger', 'threat', 'lightro', 'heavyro',
    'energyro', 'snipero', 'shotgunbo', 'light', 'heavy',
    'energy', 'sniper', 'shotgun', 'rounds', 'ammo',
    'take', 'mastiff', 'shatgun', 'peacekeeper', 'mozambique',
    'eva', 'triple', 'kraber', 'sentinel', 'longbow', 'charge',
    'bocek', 'rampage', 'spitfire', 'lstar', 'car', 'alternator',
]

# Events that should have no victim (announcements only)
NO_VICTIM_EVENTS = ["SuggestLocation", "Ping", "Audio", "Scan", "ShieldBroken", "MyShieldBroken", "Eliminated", "LootedCarePackage"]


# Game/notification vocabulary that is NEVER a player name, even in a structured kill line.
# This is the BLOCKLIST used in positional mode (segments flanking a gun/kill/[Bleed Out]
# marker, which are names by position): everything NOT in here is allowed as (part of) a name,
# so a real player literally named "I AM HERE" is kept, while a garbled "Enemy Shield Broken"
# line still can't yield a name (enemy/shield/broken are all here). Generic English filler
# (here, you, the, guy, ...) is deliberately EXCLUDED so it can be a name. The overall min-length
# check (applied to the WHOLE joined name, not per token) is what rejects short garble.
_NOTIFICATION_WORDS = {
    "spotted", "pinged", "enemy", "enemies", "reviving", "broken", "bleed", "shield",
    "eliminated", "leader", "kill", "kills", "knocked", "finisher", "killer", "headshot",
    "champion", "care", "package", "loot", "looted", "looting", "contained", "containing",
    "crafting", "materials", "items", "supply", "upgrades", "upgrade", "banner", "location",
    "suggested", "revealed", "map", "audio", "area", "defend", "avoid", "attack", "watch",
    "ping", "hitted", "level", "lvl", "mag", "extended", "dibs", "canceled", "ring", "squad",
    "teammate", "stream", "unreadable", "nerf", "nerfs", "subscribe", "fridge", "samsung",
    "crafting", "akimbo", "rounds", "elevator", "connoisseur", "connnissaiir", "lonnolsseur",
    "melee", "compromise", "compromised", "lacation", "focation", "beam", "out", "revealedl",
    "directly", "clearly", "gettingt", "pushed", "gettingshotun", "spottedan", "anenemy",
}
_LEGENDS_LOWER = {l.lower() for l in APEX_LEGENDS_CANONICAL}


def _extract_positional_name(segment: str, relax_floor: bool = False):
    """Extract a player name from a segment KNOWN to flank a kill/gun/[Bleed Out] marker, so it
    is a name BY POSITION. Keeps every token except game/notification vocabulary, clan tags,
    standalone legends and weapons; joins the survivors; then applies the minimum length to the
    WHOLE joined name (so "I AM HERE" = 7 chars qualifies as ONE name -- the min-length is
    overall, not per token). Short OCR/clan garble ("reo", "TV", "bta") fails the overall gate.

    relax_floor: drop the bare-name floor to the Apex 3-char minimum. Set ONLY by split_by_gun_icon
    when an icon and a valid opposite-slot name are already confirmed, so the short token sits in a
    trustworthy victim/attacker slot (e.g. "Shu" in "[BTA] reo <KILL_ICON> Shu") rather than being
    free-floating 3-char OCR noise."""
    tokens = re.findall(r'[\[\(]?[A-Za-z0-9_./-]+[\]\)]?', segment)
    kept = []
    saw_clan = False                              # a clan tag flanks the name -> it's a real player
    for token in tokens:
        had_brackets = token.startswith('[') or token.startswith('(')
        clean = re.sub(r'[\[\]\(\)]', '', token).strip('.,;:!?-_=|')
        if not clean:
            continue
        low = clean.lower()
        if low in _NOTIFICATION_WORDS:            # game vocab is never a name
            continue
        if 'icon' in low and len(low) <= 12:      # gun/kill-icon marker fragment leak
            continue
        if is_weapon_or_attachment(clean):
            continue
        if low in _LEGENDS_LOWER:                 # legend name: keep only if anonymized
            if had_brackets:
                kept.append(token)                # bracketed anonymized legend, e.g. [valk]
            elif re.search(r'\d{4}$', clean):
                kept.append(clean)                # legend+4digits, e.g. Octane1234
            continue                              # standalone legend -> skip
        if had_brackets and len(clean) <= 4:      # short bracketed non-legend token = clan tag
            saw_clan = True
            continue
        kept.append(clean)
    if not kept:
        return None
    name = ' '.join(kept)
    # OVERALL minimum length: the min-4 floor exists to reject 1-3 char OCR-noise names. A clan
    # tag counts toward the length ("[BTA] reo" is really an 8-char name), so a clan-tagged name
    # is a confirmed real player -- allow it down to the Apex 3-char name minimum. Bare (untagged)
    # names still need 4, blocking untagged 3-char OCR garble (box/otc/lam) validated as still present.
    floor = 3 if (saw_clan or relax_floor) else PLAYER_NAME_MIN_LENGTH
    if len(re.sub(r'\s', '', name)) < floor:
        return None
    return name


def extract_player_from_segment(segment: str, allow_compound: bool = False, positional: bool = False,
                                relax_floor: bool = False):
    """Extract player name from a segment.

    Handles:
    - Anonymized players: [valk], octane1234, bangalore5678
    - Real names: twitch.tv/bubblegum, Samsung Fridge
    - Filters out: standalone legend names without brackets/numbers

    Args:
        segment: Text segment to extract from
        allow_compound: If True, join multiple valid tokens for compound names

    Returns:
        Extracted player name or None
    """
    if not segment or len(segment) < 3:
        return None

    # Structured kill line (segment flanks a gun/kill/[Bleed Out] marker): it is a name by
    # position, so keep short/common-word names ("I AM HERE") and apply the min-length to the
    # WHOLE joined name, not per token. Free-floating text still uses the strict path below.
    if positional:
        return _extract_positional_name(segment, relax_floor=relax_floor)

    # Extract tokens (including brackets)
    tokens = re.findall(r'[\[\(]?[A-Za-z0-9_./-]+[\]\)]?', segment)

    valid_tokens = []
    _short_prefix = None  # 1-3 char fragment that may be start of a split name
    for token in tokens:
        # Clean brackets but remember they were there
        had_brackets = token.startswith('[') or token.startswith('(')
        clean_token = re.sub(r'[\[\]\(\)]', '', token)

        # Strip leading AND trailing dots, commas, special chars
        clean_token = clean_token.strip('.,;:!?-_=|')

        if len(clean_token) < PLAYER_NAME_MIN_LENGTH:
            # Save as potential name prefix (e.g. "ge" from "ge aredmah")
            # Limit to ≤2 chars and no brackets (avoids merging clan tag remnants like "idp" from [1UP])
            if 1 <= len(clean_token) <= 2 and not had_brackets and clean_token.lower() not in COMMON_WORDS:
                _short_prefix = clean_token
            else:
                _short_prefix = None
            continue
        if clean_token.lower() in COMMON_WORDS:
            continue

        # Check if it's a legend name
        is_legend = clean_token in APEX_LEGENDS_CANONICAL or clean_token.lower() in [l.lower() for l in APEX_LEGENDS_CANONICAL]

        if is_legend:
            # Legend name is VALID if:
            # 1. It had brackets: [valk] ✅
            # 2. It has numbers: valk1234 ✅
            # 3. Otherwise: valk ❌

            if had_brackets:
                # Keep bracketed legend names (anonymized players)
                valid_tokens.append(token)  # Keep with brackets
                continue

            # Check if it has 4 digits attached
            if re.search(r'\d{4}$', clean_token):
                # Legend + 4 digits = anonymized player
                valid_tokens.append(clean_token)
                continue

            # Standalone legend name without context - skip
            continue

        # Not a legend name, check if invalid
        if is_invalid_player_name(clean_token, positional=positional):
            _short_prefix = None
            continue

        # If a short prefix was saved, try prepending it (e.g. "ge"+"aredmah" → "gearedmah")
        if _short_prefix and not had_brackets:
            clean_token = _short_prefix + clean_token
        _short_prefix = None

        valid_tokens.append(clean_token)

    if not valid_tokens:
        return None

    if allow_compound:
        # Join all valid tokens for compound names
        return ' '.join(valid_tokens)
    else:
        # If the name starts with twitch/ttv, keep it compound
        if len(valid_tokens) > 1 and valid_tokens[0].lower() in ('twitch', 'ttv'):
            return ' '.join(valid_tokens)
        # Return first valid token
        return valid_tokens[0]


def split_by_gun_icon(text: str, positional: bool = False) -> Tuple[str, Optional[str]]:
    """Split killfeed text into attacker and victim using gun icons as separators.

    Strategy:
    - Split on ALL gun icon markers
    - Extract first valid player name as attacker
    - Extract last valid player name as victim
    - Anonymized players ([valk], octane1234) are kept as valid names
    """

    # Normalise all gun icon representations to uppercase marker.
    # align_and_vote() lowercases OCR variants, turning <GUN_ICON> into <gun_icon>.
    text = re.sub(r'\bgunicon\b', '<GUN_ICON>', text, flags=re.IGNORECASE)
    text = re.sub(r'<gun_icon>', '<GUN_ICON>', text, flags=re.IGNORECASE)
    # <KILL_ICON> (red elimination skull detected in the gap) splits identically -- the
    # knock/kill distinction is handled by the caller via has_kill_marker(), not here.
    text = re.sub(r'\bkillicon\b', '<GUN_ICON>', text, flags=re.IGNORECASE)
    text = re.sub(r'<kill_icon>', '<GUN_ICON>', text, flags=re.IGNORECASE)

    # Split on gun icon markers or multiple spaces
    if '<GUN_ICON>' in text:
        segments = text.split('<GUN_ICON>')
    elif re.search(r'\s{3,}', text):
        segments = re.split(r'\s{3,}', text)
    else:
        return text, None

    # Extract player names from all segments
    player_names = []
    for seg in segments:
        seg = seg.strip()

        # Skip very short segments
        if len(seg) < 3:
            continue

        # Try to extract player name from segment
        player = extract_player_from_segment(seg, allow_compound=False, positional=positional)
        if player:
            player_names.append((seg, player))  # Keep original segment + extracted name

    # Structural short-name recovery: an icon split that produced exactly ONE valid name means the
    # opposite slot is a real player in a confirmed kill/knock structure, but its name is only 3
    # chars (below the bare floor) -- e.g. "[BTA] reo <KILL_ICON> Shu". Retry every segment with the
    # relaxed 3-char floor; the icon + one confirmed name makes the short token trustworthy (not the
    # free-floating 3-char OCR noise the bare floor is there to reject). positional only.
    # Notification banners (location/supply/care-package reveals, spotted/shield lines) sometimes
    # leak a spurious icon glyph; the strict floor normally starves them of a second name, but the
    # relaxed retry below could rescue a 3-char fragment ("Set" <- "...RESET") into a fake kill. Gate
    # the relaxed pass off whenever the line carries banner vocabulary, so relax only ever fires on a
    # genuine two-name kill/knock structure.
    _banner = bool(re.search(r'revealed|location|supply|spotted|shield|looted|care\s*package', text, re.IGNORECASE))
    used_relax = False
    if len(player_names) == 1 and positional and '<GUN_ICON>' in text and not _banner:
        relaxed = []
        for seg in segments:
            seg = seg.strip()
            if len(seg) < 3:
                continue
            p = extract_player_from_segment(seg, allow_compound=False, positional=True, relax_floor=True)
            if p:
                relaxed.append((seg, p))
        if len(relaxed) >= 2:
            player_names = relaxed
            used_relax = True

    # Need at least 2 player names for attacker + victim
    if len(player_names) < 2:
        return text, None

    # First player name = attacker
    # Last player name = victim (may be compound name, so re-extract with allow_compound=True)
    attacker_segment, attacker_name = player_names[0]
    victim_segment, _ = player_names[-1]

    # Re-extract victim with compound name support (carry the relaxed floor if we used it above so a
    # 3-char victim like "Shu" survives the compound re-extract instead of being re-dropped).
    victim_name = extract_player_from_segment(victim_segment, allow_compound=True, positional=positional,
                                              relax_floor=used_relax)

    return attacker_name, victim_name


def parse_kill_event(attacker_text: str, victim_text: str, db, timestamp, positional: bool = False):
    """Parse attacker and victim from split killfeed text.

    Args are already-extracted player names, just need normalization.
    Returns: (attacker, victim, attacker_conf, victim_conf)
    """
    attacker = None
    victim = None
    attacker_conf = 0.0
    victim_conf = 0.0

    if attacker_text and not is_invalid_player_name(attacker_text, positional=positional):
        attacker, attacker_conf = db.normalize_player_name_with_confidence(attacker_text, timestamp)

    if victim_text and not is_invalid_player_name(victim_text, positional=positional):
        victim, victim_conf = db.normalize_player_name_with_confidence(victim_text, timestamp)

    return attacker, victim, attacker_conf, victim_conf


# Matches any single kill/gun icon-gap marker, including char-vote-mangled forms.
_ICON_ANY_RE = re.compile(r'<\s*(?:kill|gun)[_\s]*icon\s*>|\b(?:gun|kill)icon\b', re.IGNORECASE)


def _is_pure_digit_name(name: str) -> bool:
    """True for a pure-digit PLAYER name (4+ digits, no letters), e.g. "949930934".

    Distinct from the 1-3 digit ammo/HUD counts rejected by is_weapon_or_attachment and the
    blanket all-digit reject in is_invalid_player_name (a HUD-noise guard). A 4+-digit run is
    too long to be an ammo/kill/squad counter and is, in a clean kill line, a player who chose
    a numeric name.
    """
    return bool(re.fullmatch(r'\d{4,}', (name or '').strip()))


def _try_clean_digit_kill(line: str, canonical: str, db, timestamp, gun_line_type: str):
    """Recover a kill dropped only because one side is a pure-digit player name (bead 2b5).

    parse_kill_event / split_by_gun_icon reject all-digit tokens via is_invalid_player_name (a
    HUD-noise guard for RP totals / kill+squad counters), which also discards legit numeric
    player names -- losing the WHOLE skull-confirmed kill even though the other side is a valid
    name. In a STRUCTURE-CLEAN line (exactly ONE icon marker splitting into exactly two
    non-empty sides, >=1 side a real alphabetic name) an all-digit token is a real name, not
    HUD noise, so accept it and assign both sides to their correct slots.

    The single-icon gate keeps multi-icon HUD rows out: RP-counter rows ("11 8 <GUN> 3043
    <KILL> +138 RP ...") and kill-leader banners carry 2-3 markers -> not handled here, still
    parse to None. Returns a parsed-dict on recovery, else None (existing logic proceeds).
    Splits on the RAW line, not canonical, to avoid fix_missing_spaces camel-splitting a name
    like "Pathfinder8438" into "Pathfinder 8438".
    """
    if not KNOCK_KILL_DISTINCTION:
        return None
    if len(_ICON_ANY_RE.findall(line)) != 1:
        return None
    parts = _ICON_ANY_RE.split(line)
    if len(parts) != 2:
        return None
    left, right = clean_player_name(parts[0].strip()), clean_player_name(parts[1].strip())
    if not left or not right:
        return None
    ld, rd = _is_pure_digit_name(left), _is_pure_digit_name(right)
    if not (ld or rd):
        return None  # no digit side -> the normal path already handles (or correctly rejects) it
    lv = not ld and not is_invalid_player_name(left) and not is_weapon_or_attachment(left)
    rv = not rd and not is_invalid_player_name(right) and not is_weapon_or_attachment(right)
    if not ((lv or ld) and (rv or rd)):
        return None  # some side is neither a valid name nor a clean digit name -> HUD noise/garbage
    if not (lv or rv):
        return None  # require >=1 REAL alphabetic name (reject digit-vs-digit, e.g. counter rows)
    attacker, attacker_conf = db.normalize_player_name_with_confidence(left, timestamp)
    victim, victim_conf = db.normalize_player_name_with_confidence(right, timestamp)
    if not attacker or not victim or attacker.lower() == victim.lower():
        return None
    return {
        "raw_line": line,
        "canonical": canonical,
        "event_type": gun_line_type,
        "attacker": attacker,
        "victim": victim,
        "attacker_conf": attacker_conf,
        "victim_conf": victim_conf,
        # Tells the parse_killfeed_line wrapper not to re-reject a pure-digit name it just
        # deliberately accepted (the wrapper otherwise re-runs is_invalid_player_name).
        "_digit_ok": True,
    }


def remove_clan_tag(name: str) -> str:
    """Remove clan tags like [CLAN], (CLAN), {CLAN}, |CLAN| from player names."""
    if not name:
        return name

    # Remove bracketed tags anywhere in name
    name = re.sub(r'[\[\(\{\|][A-Z0-9]{2,6}[\]\)\}\|]', '', name)

    # Remove OCR misreads of brackets at start
    name = re.sub(r'^I[A-Z0-9]{2,6}[I\]\)]', '', name)

    # Remove standalone uppercase tags followed by underscore/dash
    name = re.sub(r'^[A-Z]{2,6}[_-]', '', name)

    # DON'T remove streaming prefixes - they're part of the player name!
    # Examples: ttv_playername, twitch.tv/playername, yt-playername
    # These are intentional and should be kept

    # Remove trailing/leading separators
    name = name.strip('-_|[](){}')

    # Remove multiple consecutive separators
    name = re.sub(r'[-_]{2,}', '-', name)

    return name.strip()


def fix_typos(s: str) -> str:
    """Apply known OCR typo corrections."""
    lower = s.lower()
    for wrong, right in TYPO_MAP.items():
        lower = lower.replace(wrong, right)
    return lower


def fix_missing_spaces(s: str) -> str:
    """Insert missing spaces."""
    fixed = s

    # Split before common words
    for w in COMMON_WORDS:
        pattern = rf"([A-Za-z0-9_])({w})"
        fixed = re.sub(pattern, r"\1 \2", fixed, flags=re.IGNORECASE)

    # CamelCase split
    fixed = re.sub(r"([a-z])([A-Z])", r"\1 \2", fixed)

    return fixed


def normalize_brackets(s: str) -> str:
    """Normalize bracket tags."""
    # Tolerate OCR garble of the "Out" tail: the last 't' is frequently mis-read (Our/Oul/Oux)
    # or dropped (Ou), and the 'O' reads as o/0/q/d/g/c. Anchored on "bleed", so widening the
    # tail is safe. Catches [Bleed Out]/[Bleed Our]/[Bleed Ou]/[Bleed 0ut]/bracketless variants.
    s = re.sub(r"\[?\s*b[il]ee?d\s*[oqdgc0]?u[a-z]?\s*\]?", "[Bleed Out]", s, flags=re.IGNORECASE)
    s = re.sub(r"\[bla?ed\s*out\]", "[Bleed Out]", s, flags=re.IGNORECASE)
    s = re.sub(r"\[blg\s", "[Bleed ", s, flags=re.IGNORECASE)

    return s


def normalize_common_phrases(s: str) -> str:
    """Full normalization pipeline."""
    s = normalize_brackets(s)
    s = fix_typos(s)
    s = fix_missing_spaces(s)

    # Final cleanup
    s = re.sub(r"enemy\s+shield\s+broken", "Enemy Shield Broken", s, flags=re.IGNORECASE)
    s = re.sub(r"my\s+shield\s+broken", "My Shield Broken", s, flags=re.IGNORECASE)
    s = re.sub(r"bleed\s+out", "Bleed Out", s, flags=re.IGNORECASE)

    return " ".join(s.split())


def is_weapon_or_attachment(name: str) -> bool:
    """Check if name is a weapon or attachment."""
    if not name:
        return False

    name_low = name.lower()
    name_compact = re.sub(r'[^a-z0-9]', '', name_low)

    # Match weapon keywords as WHOLE tokens, not substrings. Substring matching (the old behavior)
    # false-flagged any player name CONTAINING a short weapon keyword -- e.g. "mag" (magazine) inside
    # "maggie8793" (Mad Maggie anonymized player), "eva" in "evan", "car" in "oscar", "volt" in
    # "revolt" -- silently dropping real names (measured: a large share of empty-side BleedOut rows,
    # 2026-07-11 audit). Flag only when the whole name IS the weapon (compact, so "r-99"=="r99") or the
    # weapon appears delimited by non-alphanumerics.
    for weapon in WEAPON_KEYWORDS:
        wl = weapon.lower()
        if name_compact == re.sub(r'[^a-z0-9]', '', wl):
            return True
        if re.search(rf'(?<![a-z0-9]){re.escape(wl)}(?![a-z0-9])', name_low):
            return True

    # Check for ammo patterns like "48", "96", "120" (common ammo counts)
    if re.fullmatch(r'\d{1,3}', name):
        return True

    return False


def is_invalid_player_name(name: str, positional: bool = False) -> bool:
    """Check if a name is actually a game event artifact.

    positional=True: the name came from a segment flanking a gun/kill/[Bleed Out] marker, so it
    is a player by structure. Generic filler words (here/you/the/...) are then valid names;
    only true notification/game vocabulary (_NOTIFICATION_WORDS) still rejects, and the 3+-word
    guard is lifted (multi-word names are real). Non-positional keeps the strict legacy behavior
    (rejects the whole invalid_patterns list, 3+-word names) for free-floating text.
    """
    if not name:
        return True

    name_low = name.lower().strip()

    # Filter out standalone legend names (case-insensitive)
    name_title = name.title()
    if name_title in APEX_LEGENDS_CANONICAL or name in APEX_LEGENDS_CANONICAL:
        return True
    if name_low in {l.lower() for l in APEX_LEGENDS_CANONICAL}:
        return True

    # Filter out gun icon markers, including partial OCR reads of the marker (e.g. "un_icon"
    # from a dropped leading "g" -- the marker is a tiny weapon-icon graphic, not real text, so
    # character-level OCR accuracy on it is unreliable). "icon" is not a plausible substring of
    # a real Apex player name, so treat any short fragment containing it as the marker leaking
    # through rather than requiring an exact/full match.
    if 'icon' in name_low and len(name_low) <= 12:
        return True

    if name_low in ['<gun_icon>', 'gunicon', 'gun_icon', 'gun', 'icon']:
        return True

    # [Bleed Out] tag fragments leaking into names (e.g. "int[Bleed", "Out]m")
    if '[bleed' in name_low or 'bleed]' in name_low:
        return True

    # Names that are ONLY a clan tag (e.g. "[x78]") -- the actual name was lost to OCR
    if re.fullmatch(r'\[[^\]]*\]', name.strip()):
        return True

    # Filter names that start with special chars (like /sg, -name, etc.)
    if name and name[0] in ['.', ',', '/', '-', '_', '=', '|', ':', ';']:
        return True

    invalid_patterns = [
        "broken-", "broken", "suggested", "-suggested", "spotted-", "spotted",
        "location", "locationd", "out", "bleed", "ring", "the", "you",
        "enemy", "shield", "audio", "here", "care", "package", "containing",
        "eliminated", "leader", "kill", "new", "with", "kills", "looted",
        "that", "contained", "has", "been", "pinged", "loot", "reviving",
        "champion", "are", "and", "guy", "looking", "people", "compromise",
        "nerf", "nerfs",  # recurring 'nerf seer pls' banner misparsed as a Kill (bead az2)
        "compromised", "lacation", "focation", "area", "over", "defend",
        "avoid", "this", "looting", "from", "full", "beam", "for", "since",
        "fridge", "samsung", "subscribe", "crafting", "ready",
        "fall", "akimbo", "heavy", "rounds", "elevator", "music",
        "connoisseur", "connnissaiir", "lonnolsseur", "melee", "edge",
        "sakuras", "sakurasedge",
        "directly", "clearly", "getting", "gettingt", "pushed",
    ]

    # In a structured kill line, a generic-filler name (I AM HERE, "you", ...) is a real player;
    # only genuine notification/game vocabulary (in _NOTIFICATION_WORDS) still rejects it there.
    if name_low in invalid_patterns and not (positional and name_low not in _NOTIFICATION_WORDS):
        return True

    # Check if it's a weapon/attachment
    if is_weapon_or_attachment(name):
        return True

    if name_low.endswith("-suggested") or name_low.endswith("suggested"):
        return True

    if name_low.endswith("-spotted") or name_low.endswith("spotted"):
        return True

    if re.fullmatch(r"[-_:@\s0-9]+", name):
        return True

    # Reject names containing characters impossible in Apex player names
    # Apex allows: letters, digits, -, _, .  (not / \ | etc.)
    # Note: [ ] are excluded here because clan tags haven't been stripped yet
    if re.search(r'[/<>\\|]', name):
        return True

    # Sentence fragments: Apex names don't have 3+ words -- relaxed for a structured kill line,
    # where a multi-word name ("I AM HERE", "225 Bench Press Manifestation") is a real player.
    if not positional and len(name.split()) >= 3:
        return True

    if len(name) < 3:
        return True

    return False


def clean_player_name(name: str) -> str:
    """Clean up player name by removing event suffixes and clan tags."""
    if not name:
        return name

    # Strip leading AND trailing dots first
    name = name.strip('.')

    # Remove bracketed legend indicators like [DENM], [TheRing], [UB]
    # But keep anonymized player brackets like [valk]
    # Only remove if it's ALL CAPS or mixed case clan tags
    name = re.sub(r'^\[[A-Z][A-Za-z0-9]{2,6}\]', '', name)

    name = remove_clan_tag(name)
    name = re.sub(r"[-_]+(suggested|spotted|reviving|pinged).*$", "", name, flags=re.IGNORECASE)
    name = name.rstrip("-_:@")

    return name


def parse_killfeed_line(line: str, db, timestamp: Optional[float] = None) -> Dict[str, Optional[str]]:
    res = _parse_killfeed_line_raw(line, db, timestamp)
    if not res:
        return res

    attacker = res.get("attacker")
    victim = res.get("victim")
    event_type = res.get("event_type")
    attacker_conf = res.get("attacker_conf", 0.0)
    victim_conf = res.get("victim_conf", 0.0)

    # A pure-digit name deliberately accepted by _try_clean_digit_kill must not be re-rejected
    # here by the all-digit guard inside is_invalid_player_name (bead 2b5).
    digit_ok = res.get("_digit_ok", False)
    # A filler/short name deliberately accepted from a structured kill line (gun/bleed marker)
    # must be re-checked in positional mode too, or the wrapper re-rejects "I AM HERE" etc.
    positional_ok = res.get("_positional_ok", False)

    # Clean up legend leaks/weapons/invalid names from normalized attacker
    if attacker and not (digit_ok and _is_pure_digit_name(attacker)):
        if is_invalid_player_name(attacker, positional=positional_ok) or is_weapon_or_attachment(attacker):
            attacker = None
            attacker_conf = 0.0

    # Clean up legend leaks/weapons/invalid names from normalized victim
    if victim and not (digit_ok and _is_pure_digit_name(victim)):
        if is_invalid_player_name(victim, positional=positional_ok) or is_weapon_or_attachment(victim):
            victim = None
            victim_conf = 0.0

    # Check for self-kills (OCR splitting failures or suicides)
    if attacker and victim and attacker.lower() == victim.lower():
        attacker = None
        victim = None
        event_type = None
        attacker_conf = 0.0
        victim_conf = 0.0

    res["attacker"] = attacker
    res["victim"] = victim
    res["event_type"] = event_type
    res["attacker_conf"] = attacker_conf
    res["victim_conf"] = victim_conf
    return res


def _parse_killfeed_line_raw(line: str, db, timestamp: Optional[float] = None) -> Dict[str, Optional[str]]:
    """Extract attacker, victim, and event_type from killfeed line.

    Args:
        line: Raw OCR text from killfeed
        db: PlayerDatabase instance for name normalization
        timestamp: Event timestamp for temporal clustering

    Returns:
        Dict with keys: raw_line, canonical, event_type, attacker, victim
    """
    canonical = normalize_common_phrases(line)

    event_type = None
    low = canonical.lower()

    # Knock vs kill: the OCR layer emits <KILL_ICON> when the red elimination skull was
    # detected in the icon gap, <GUN_ICON> otherwise. A skull line is a confirmed
    # elimination ('Kill'); a plain gap line is a knockdown ('Knock') -- the victim may be
    # revived and the pair may legitimately recur (knock+finish, revive/respawn cycles).
    # With KNOCK_KILL_DISTINCTION off, everything stays 'Kill' (legacy behavior; required
    # for OCR paths that never emit <KILL_ICON>, e.g. TrOCR/Tesseract).
    has_kill_marker = bool(re.search(r"<\s*kill[_\s]*icon\s*>|\bkillicon\b", low))
    gun_line_type = "Kill" if (has_kill_marker or not KNOCK_KILL_DISTINCTION) else "Knock"

    # Ignore HUD banners/prompts that can contain a gun icon glyph but are
    # never kill lines (player-join banner, ability-upgrade prompt). Must run
    # before the gun-icon split below, or these get misclassified as Kill.
    if re.search(r"\bentered\b", low) or "abilit" in low:
        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": None,
            "attacker": None,
            "victim": None,
            "attacker_conf": 0.0,
            "victim_conf": 0.0,
        }

    # EARLY CHECK: Try to split by gun icon for kill events
    # This handles format: "attacker <gun_icon> victim"
    attacker_part, victim_part = split_by_gun_icon(canonical, positional=True)

    if victim_part:  # Successfully split into two parts
        # This is likely a kill event
        attacker, victim, attacker_conf, victim_conf = parse_kill_event(attacker_part, victim_part, db, timestamp, positional=True)

        if attacker and victim:
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": gun_line_type,
                "attacker": attacker,
                "victim": victim,
                "attacker_conf": attacker_conf,
                "victim_conf": victim_conf,
                "_positional_ok": True,
            }

    # Structure-clean digit-name kill recovery (bead 2b5): the normal split above drops kills
    # where one side is a pure-digit player name (is_invalid_player_name's all-digit HUD-noise
    # guard). Recover those in a clean single-icon 2-side line before falling through to the
    # event-type detection below. No-op for every non-digit line (returns None).
    _dk = _try_clean_digit_kill(line, canonical, db, timestamp, gun_line_type)
    if _dk:
        return _dk

    # Special handling for care packages - no victim
    if "care package" in low and "containing" in low:
        attacker = None
        # Extract player who opened it
        m = re.search(r"([A-Za-z0-9_.\-/]+):\s*c?are\s+package", canonical, re.IGNORECASE)
        if m:
            attacker_raw = clean_player_name(m.group(1))
            if not is_invalid_player_name(attacker_raw):
                attacker = db.normalize_player_name(attacker_raw, timestamp)

        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": "CarePackage",
            "attacker": attacker,
            "victim": None,
            "attacker_conf": 0.0,
            "victim_conf": 0.0,
        }

    # Detect event type
    if "enemy shield broken" in low or "shield broken" in low:
        event_type = "ShieldBroken"
    elif "my shield broken" in low:
        event_type = "MyShieldBroken"
    elif "bleed out" in low:
        event_type = "BleedOut"
    elif "spotted" in low:
        event_type = "Ping"
    elif "reviving" in low:
        event_type = "Revive"
    elif "suggested" in low and "location" in low:
        event_type = "SuggestLocation"
    elif "revealed" in low and ("enemies" in low or "map" in low):
        event_type = "Scan"
    elif "enemy audio" in low:
        event_type = "Audio"
    elif "pinged" in low and "loot" in low:
        event_type = "PingLoot"
    elif "eliminated" in low:
        event_type = "Eliminated"
    elif "looted" in low and "care package" in low:
        event_type = "LootedCarePackage"

    attacker = None
    victim = None
    attacker_conf = 0.0
    victim_conf = 0.0

    # "Champion <name> has been eliminated" / "<name> has been eliminated" banner: the NAMED player
    # is the one who DIED. Some deaths carry no clean killfeed kill line (only a knock + this banner,
    # e.g. Capn_Krispy), so surface the death with victim = the named player. Attacker is unknown from
    # the banner, so use a dedicated event type that ELO and match-detection ignore (they gate strictly
    # on Kill/BleedOut) -- this is a death SIGNAL for coverage, not an ELO-crediting kill.
    if event_type == "Eliminated":
        m = re.search(r'(?:champion\s+)?(.+?)\s+has\s*been\s*[ae]l[il1]m[il1]n', canonical, re.IGNORECASE)
        if m:
            vic_raw = re.sub(r'^(?:the\s+)?champion\s+', '', clean_player_name(m.group(1)),
                             flags=re.IGNORECASE).strip()
            if vic_raw and not is_invalid_player_name(vic_raw):
                victim, victim_conf = db.normalize_player_name_with_confidence(vic_raw, timestamp)
                if victim:
                    return {
                        "raw_line": line,
                        "canonical": canonical,
                        "event_type": "ChampionEliminated",
                        "attacker": None,
                        "victim": victim,
                        "attacker_conf": 0.0,
                        "victim_conf": victim_conf,
                    }

    # For events that should have no victim (announcements)
    if event_type in NO_VICTIM_EVENTS:
        if "you" in low:
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": event_type,
                "attacker": "You",
                "victim": None,
                "attacker_conf": 1.0,
                "victim_conf": 0.0,
            }

        # Extract first valid player name as attacker only
        all_tokens = re.findall(r"[A-Za-z0-9_.\-/\[\]]+", canonical)
        for token in all_tokens:
            if len(token) < PLAYER_NAME_MIN_LENGTH:
                continue
            if token.lower() in COMMON_WORDS:
                continue

            # Check if it's anonymized player
            if re.match(r'\[.+\]', token):
                attacker, attacker_conf = db.normalize_player_name_with_confidence(token, timestamp)
                return {
                    "raw_line": line,
                    "canonical": canonical,
                    "event_type": event_type,
                    "attacker": attacker,
                    "victim": None,
                    "attacker_conf": attacker_conf,
                    "victim_conf": 0.0,
                }

            # Skip legend names unless it's legend+4digits
            if not db.is_legend_with_number(token) and db.is_legend_name(token):
                continue

            if is_invalid_player_name(token):
                continue

            cleaned = clean_player_name(token)
            if cleaned and len(cleaned) >= PLAYER_NAME_MIN_LENGTH:
                attacker, attacker_conf = db.normalize_player_name_with_confidence(cleaned, timestamp)
                return {
                    "raw_line": line,
                    "canonical": canonical,
                    "event_type": event_type,
                    "attacker": attacker,
                    "victim": None,
                    "attacker_conf": attacker_conf,
                    "victim_conf": 0.0,
                }

        # No valid player found
        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": event_type,
            "attacker": None,
            "victim": None,
            "attacker_conf": 0.0,
            "victim_conf": 0.0,
        }

    # For PingLoot events, only extract attacker, no victim
    if event_type == "PingLoot":
        if "you" in low:
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": event_type,
                "attacker": "You",
                "victim": None,
                "attacker_conf": 1.0,
                "victim_conf": 0.0,
            }
        # Try to extract player name before "pinged"
        m = re.search(r"([A-Za-z0-9_.\-/]+)\s+pinged", canonical, re.IGNORECASE)
        if m:
            attacker_raw = clean_player_name(m.group(1))
            if not is_invalid_player_name(attacker_raw):
                attacker, attacker_conf = db.normalize_player_name_with_confidence(attacker_raw, timestamp)
        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": event_type,
            "attacker": attacker,
            "victim": None,
            "attacker_conf": attacker_conf,
            "victim_conf": 0.0,
        }

    # Extract [Bleed Out] kills. Format is "attacker [Bleed Out] victim". The old approach required a
    # single literal-bracketed token on each side, which silently dropped a name whenever OCR garbled
    # the brackets, jammed them against a name ("name[Bleed Out]"), or left noise tokens adjacent to
    # the marker ("murmu m Bleed Out odk") -- measured 2026-07-11 as ~57% of BleedOut rows losing a
    # side. Instead split on the marker (bracket-optional, OCR-tolerant) and reuse
    # extract_player_from_segment on each side, which already skips noise/short-prefix tokens and
    # keeps legend+number anonymized players (e.g. Wraith1234) that the old path rejected.
    if "bleed" in low:
        bo = re.split(r"\[?\s*b[li]ee?d\s*[oqdgc0]?ut\s*\]?", canonical, maxsplit=1, flags=re.IGNORECASE)
        if len(bo) == 2:
            left_name = extract_player_from_segment(bo[0], allow_compound=False, positional=True)
            right_name = extract_player_from_segment(bo[1], allow_compound=True, positional=True)
            if left_name and not is_invalid_player_name(left_name, positional=True):
                attacker, attacker_conf = db.normalize_player_name_with_confidence(left_name, timestamp)
            if right_name and not is_invalid_player_name(right_name, positional=True):
                victim, victim_conf = db.normalize_player_name_with_confidence(right_name, timestamp)
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": event_type,
                "attacker": attacker,
                "victim": victim,
                "attacker_conf": attacker_conf,
                "victim_conf": victim_conf,
                "_positional_ok": True,
            }

    # Extract "PlayerName: Reviving PlayerName2"
    if event_type == "Revive":
        m = re.search(r"reviving\s*([A-Za-z0-9_.\-/]+)", canonical, re.IGNORECASE)
        if m:
            victim_raw = clean_player_name(m.group(1))
            if not is_invalid_player_name(victim_raw):
                victim, victim_conf = db.normalize_player_name_with_confidence(victim_raw, timestamp)
            # Try to extract the reviver from before the colon
            m_atk = re.search(r"^([A-Za-z0-9_.\-/]+)\s*:", canonical)
            if m_atk:
                atk_raw = clean_player_name(m_atk.group(1))
                if atk_raw and not is_invalid_player_name(atk_raw):
                    attacker, attacker_conf = db.normalize_player_name_with_confidence(atk_raw, timestamp)
            if not attacker:
                attacker = "You"
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": event_type,
                "attacker": attacker,
                "victim": victim,
                "attacker_conf": 1.0,
                "victim_conf": victim_conf,
            }

    # Extract "You spotted X" or "You pinged loot"
    if "you" in low and (event_type == "Ping" or event_type == "PingLoot"):
        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": event_type,
            "attacker": "You",
            "victim": None,
            "attacker_conf": 1.0,
            "victim_conf": 0.0,
        }

    # Extract player names from general format
    all_tokens = re.findall(r"[A-Za-z0-9_.\-/\[\]]+", canonical)

    player_tokens = []
    for token in all_tokens:
        if len(token) < PLAYER_NAME_MIN_LENGTH:
            continue
        if token.lower() in COMMON_WORDS:
            continue

        # Keep anonymized players with brackets
        if re.match(r'\[.+\]', token):
            player_tokens.append(token)
            continue

        # Keep legend+4digits (anonymized players like "Octane1234")
        if not db.is_legend_with_number(token) and db.is_legend_name(token):
            continue

        if is_invalid_player_name(token):
            continue

        cleaned = clean_player_name(token)
        if cleaned and len(cleaned) >= PLAYER_NAME_MIN_LENGTH:
            player_tokens.append(cleaned)

    # Handle compound legend+player names (but NOT legend+4digits)
    processed_tokens = []
    for token in player_tokens:
        if db.is_legend_with_number(token):
            processed_tokens.append(token)
            continue

        # Keep bracketed names as-is
        if re.match(r'\[.+\]', token):
            processed_tokens.append(token)
            continue

        token_low = token.lower()
        found_legend = False

        for legend in APEX_LEGENDS_CANONICAL:
            legend_low = legend.lower()
            if token_low.startswith(legend_low) and len(token) > len(legend):
                player_part = token[len(legend):]

                # Only split if it's NOT exactly 4 digits
                if not re.fullmatch(r'\d{4}', player_part):
                    if len(player_part) >= PLAYER_NAME_MIN_LENGTH and not is_invalid_player_name(player_part):
                        processed_tokens.append(player_part)
                        found_legend = True
                        break

        if not found_legend:
            processed_tokens.append(token)

    # Extract attacker (first) and victim (last)
    if len(processed_tokens) >= 2:
        attacker_raw = processed_tokens[0]
        victim_raw = processed_tokens[-1]

        if not is_invalid_player_name(attacker_raw):
            attacker, attacker_conf = db.normalize_player_name_with_confidence(attacker_raw, timestamp)
        if not is_invalid_player_name(victim_raw):
            victim, victim_conf = db.normalize_player_name_with_confidence(victim_raw, timestamp)
    elif len(processed_tokens) == 1:
        name_raw = processed_tokens[0]
        if not is_invalid_player_name(name_raw):
            if event_type == "ShieldBroken":
                victim, victim_conf = db.normalize_player_name_with_confidence(name_raw, timestamp)
            else:
                attacker, attacker_conf = db.normalize_player_name_with_confidence(name_raw, timestamp)

    # Final cleanup - double-check attacker/victim aren't weapons or common words
    if attacker:
        if is_invalid_player_name(attacker) or is_weapon_or_attachment(attacker):
            attacker = None
            attacker_conf = 0.0

    if victim:
        if is_invalid_player_name(victim) or is_weapon_or_attachment(victim):
            victim = None
            victim_conf = 0.0

    # Discard self-kills (OCR name-splitting failure)
    if attacker and victim and attacker.lower() == victim.lower():
        event_type = None
        attacker = None
        victim = None
        attacker_conf = 0.0
        victim_conf = 0.0

    return {
        "raw_line": line,
        "canonical": canonical,
        "event_type": event_type,
        "attacker": attacker,
        "victim": victim,
        "attacker_conf": attacker_conf,
        "victim_conf": victim_conf,
    }


# ──────────────────────────────────────────────── Debug / Noise Audit ─────────


def _debug_trace_segment(
    segment: str,
    allow_compound: bool,
    role: str,
    trace: list,
) -> "str | None":
    """Instrumented replica of extract_player_from_segment for parse tracing."""
    trace.append(f'[{role} SEG]  Processing: "{segment}"')

    if not segment or len(segment) < 3:
        trace.append(f'[{role}]      segment too short (len {len(segment) if segment else 0}) -> None')
        return None

    tokens = re.findall(r'[\[\(]?[A-Za-z0-9_./-]+[\]\)]?', segment)
    valid_tokens = []
    _short_prefix = None

    for token in tokens:
        had_brackets = token.startswith('[') or token.startswith('(')
        clean_token = re.sub(r'[\[\]\(\)]', '', token)
        clean_token = clean_token.strip('.,;:!?-_=|')

        if len(clean_token) < PLAYER_NAME_MIN_LENGTH:
            if 1 <= len(clean_token) <= 2 and not had_brackets and clean_token.lower() not in COMMON_WORDS:
                trace.append(f'[TOKEN]   "{token}" -> saved as short_prefix (len {len(clean_token)} <= 2)')
                _short_prefix = clean_token
            else:
                if _short_prefix is not None:
                    trace.append(f'[TOKEN]   "{token}" -> FAIL: len {len(clean_token)} < {PLAYER_NAME_MIN_LENGTH}  (prefix cleared)')
                    _short_prefix = None
                else:
                    trace.append(f'[TOKEN]   "{token}" -> FAIL: len {len(clean_token)} < {PLAYER_NAME_MIN_LENGTH}')
            continue

        if clean_token.lower() in COMMON_WORDS:
            trace.append(f'[TOKEN]   "{token}" -> FAIL: in COMMON_WORDS')
            continue

        is_legend = (
            clean_token in APEX_LEGENDS_CANONICAL
            or clean_token.lower() in [l.lower() for l in APEX_LEGENDS_CANONICAL]
        )

        if is_legend:
            if had_brackets:
                trace.append(f'[TOKEN]   "{token}" -> PASS  (bracketed legend)')
                valid_tokens.append(token)
                continue
            if re.search(r'\d{4}$', clean_token):
                trace.append(f'[TOKEN]   "{token}" -> PASS  (legend+4digits)')
                valid_tokens.append(clean_token)
                continue
            trace.append(f'[TOKEN]   "{token}" -> FAIL: standalone legend name (no brackets/4-digit suffix)')
            continue

        if is_invalid_player_name(clean_token):
            trace.append(f'[TOKEN]   "{token}" -> FAIL: is_invalid_player_name')
            _short_prefix = None
            continue

        # Valid token
        if _short_prefix and not had_brackets:
            merged = _short_prefix + clean_token
            trace.append(f'[TOKEN]   "{token}" -> PASS  short_prefix "{_short_prefix}" prepended -> "{merged}"')
            clean_token = merged
        else:
            trace.append(f'[TOKEN]   "{token}" -> PASS  -> accepted: "{clean_token}"')
        _short_prefix = None
        valid_tokens.append(clean_token)

    if not valid_tokens:
        trace.append(f'[{role}]      no valid tokens found -> None')
        return None

    if allow_compound and len(valid_tokens) > 1:
        joined = ' '.join(valid_tokens)
        parts = ' + '.join(f'"{t}"' for t in valid_tokens)
        trace.append(f'[{role}]      compound: {parts} -> "{joined}"')
        trace.append(f'[{role}]      {role}_raw = "{joined}"')
        return joined

    trace.append(f'[{role}]      {role}_raw = "{valid_tokens[0]}"')
    return valid_tokens[0]


def debug_parse_line(line: str) -> tuple:
    """Trace the parse pipeline for debugging. Returns (trace, attacker_raw, victim_raw).

    No db required — shows structural parse decisions only (no fuzzy name matching).
    """
    trace = []
    trace.append(f'[INPUT]   "{line}"')

    canonical = normalize_common_phrases(line)
    if canonical != line:
        trace.append(f'[NORM]    -> "{canonical}"')
    else:
        trace.append(f'[NORM]    (no change)')

    # Inline segment split — do NOT call split_by_gun_icon (it swallows the segments)
    text = re.sub(r'\bgunicon\b', '<GUN_ICON>', canonical, flags=re.IGNORECASE)
    text = re.sub(r'<gun_icon>', '<GUN_ICON>', text, flags=re.IGNORECASE)

    if '<GUN_ICON>' in text:
        raw_segs = [s.strip() for s in text.split('<GUN_ICON>')]
        trace.append(f'[SPLIT]   method: <GUN_ICON> ({len(raw_segs)} segments)')
    elif re.search(r'\s{3,}', text):
        raw_segs = [s.strip() for s in re.split(r'\s{3,}', text)]
        trace.append(f'[SPLIT]   method: 3+ space gap ({len(raw_segs)} segments)')
    else:
        raw_segs = [text]
        trace.append(f'[SPLIT]   no separator — single segment')

    for i, s in enumerate(raw_segs):
        trace.append(f'[SPLIT]   segment[{i}]: "{s}"')

    valid_segs = [s for s in raw_segs if len(s) >= 3]

    attacker_raw = None
    victim_raw   = None

    if valid_segs:
        attacker_raw = _debug_trace_segment(valid_segs[0], False, "ATK", trace)
    if len(valid_segs) >= 2:
        victim_raw = _debug_trace_segment(valid_segs[-1], True, "VIC", trace)

    trace.append(f'[RESULT]  attacker_raw={attacker_raw!r}  victim_raw={victim_raw!r}')
    return trace, attacker_raw, victim_raw
