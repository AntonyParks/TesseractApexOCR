"""Killfeed parsing and text normalization."""

import re
from typing import Dict, Optional, Tuple

from config import (
    COMMON_WORDS, TYPO_MAP, PLAYER_NAME_MIN_LENGTH, APEX_LEGENDS_CANONICAL
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
NO_VICTIM_EVENTS = ["SuggestLocation", "Ping", "Audio", "Scan"]


def extract_player_from_segment(segment: str, allow_compound: bool = False):
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

    # If segment starts with "twitch" (any form), it's a Twitch URL overlay.
    # Reject the whole segment — mangled reads like "twitch (vst oneyg" must not
    # produce fake player names like "oney" or "dneve".
    if segment.lower().lstrip().startswith('twitch'):
        return None

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
        if is_invalid_player_name(clean_token):
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
        # Return first valid token
        return valid_tokens[0]


def split_by_gun_icon(text: str) -> Tuple[str, Optional[str]]:
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
        player = extract_player_from_segment(seg, allow_compound=False)
        if player:
            player_names.append((seg, player))  # Keep original segment + extracted name

    # Need at least 2 player names for attacker + victim
    if len(player_names) < 2:
        return text, None

    # First player name = attacker
    # Last player name = victim (may be compound name, so re-extract with allow_compound=True)
    attacker_segment, attacker_name = player_names[0]
    victim_segment, _ = player_names[-1]

    # Re-extract victim with compound name support
    victim_name = extract_player_from_segment(victim_segment, allow_compound=True)

    return attacker_name, victim_name


def parse_kill_event(attacker_text: str, victim_text: str, db, timestamp):
    """Parse attacker and victim from split killfeed text.

    Args are already-extracted player names, just need normalization.
    Returns: (attacker, victim, attacker_conf, victim_conf)
    """
    attacker = None
    victim = None
    attacker_conf = 0.0
    victim_conf = 0.0

    if attacker_text and not is_invalid_player_name(attacker_text):
        attacker, attacker_conf = db.normalize_player_name_with_confidence(attacker_text, timestamp)

    if victim_text and not is_invalid_player_name(victim_text):
        victim, victim_conf = db.normalize_player_name_with_confidence(victim_text, timestamp)

    return attacker, victim, attacker_conf, victim_conf


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
    s = re.sub(r"\[b[il]ee?d\s*[qodgc]ut\]", "[Bleed Out]", s, flags=re.IGNORECASE)
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

    # Check against weapon keywords
    for weapon in WEAPON_KEYWORDS:
        if weapon in name_low:
            return True

    # Check for ammo patterns like "48", "96", "120" (common ammo counts)
    if re.fullmatch(r'\d{1,3}', name):
        return True

    return False


def is_invalid_player_name(name: str) -> bool:
    """Check if a name is actually a game event artifact."""
    if not name:
        return True

    name_low = name.lower().strip()

    # Filter out gun icon markers
    if 'gun' in name_low and 'icon' in name_low:
        return True

    if name_low in ['<gun_icon>', 'gunicon', 'gun_icon', 'gun', 'icon']:
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
        "compromised", "lacation", "focation", "area", "over", "defend",
        "avoid", "this", "looting", "from", "full", "beam", "for", "since",
        "fridge", "samsung", "twitch", "subscribe", "crafting", "ready",
        "fall", "akimbo", "heavy", "rounds", "elevator", "music",
        "connoisseur", "connnissaiir", "lonnolsseur", "melee", "edge",
        "sakuras", "sakurasedge",
    ]

    if name_low in invalid_patterns:
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

    # Sentence fragments: Apex names don't have 3+ words
    if len(name.split()) >= 3:
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

    # EARLY CHECK: Try to split by gun icon for kill events
    # This handles format: "attacker <gun_icon> victim"
    attacker_part, victim_part = split_by_gun_icon(canonical)

    if victim_part:  # Successfully split into two parts
        # This is likely a kill event
        attacker, victim, attacker_conf, victim_conf = parse_kill_event(attacker_part, victim_part, db, timestamp)

        if attacker and victim:
            return {
                "raw_line": line,
                "canonical": canonical,
                "event_type": "Kill",
                "attacker": attacker,
                "victim": victim,
                "attacker_conf": attacker_conf,
                "victim_conf": victim_conf,
            }

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

    # Extract [Bleed Out] kills
    m = re.search(
        r"([A-Za-z0-9_.\-/\[\]]+)\s+\[bleed out\]\s*([A-Za-z0-9_.\-/\[\]]+)",
        canonical,
        re.IGNORECASE,
    )
    if m:
        attacker_raw = clean_player_name(m.group(1))
        victim_raw = clean_player_name(m.group(2))

        if not is_invalid_player_name(attacker_raw):
            attacker, attacker_conf = db.normalize_player_name_with_confidence(attacker_raw, timestamp)
        if not is_invalid_player_name(victim_raw):
            victim, victim_conf = db.normalize_player_name_with_confidence(victim_raw, timestamp)

        return {
            "raw_line": line,
            "canonical": canonical,
            "event_type": event_type,
            "attacker": attacker,
            "victim": victim,
            "attacker_conf": attacker_conf,
            "victim_conf": victim_conf,
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

    if segment.lower().lstrip().startswith('twitch'):
        trace.append(f"[SKIP]    segment starts with 'twitch' -> entire segment rejected")
        trace.append(f'[{role}]      {role}_raw = None')
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
