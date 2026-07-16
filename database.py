"""Database management for player names and legend typos with temporal clustering."""

import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Tuple, Optional
from collections import defaultdict

from config import (
    PLAYER_DB_PATH, LEGEND_TYPO_DB_PATH, APEX_LEGENDS_CANONICAL,
    DEFAULT_LEGEND_TYPOS, PLAYER_NAME_MIN_LENGTH, FUZZY_MATCH_THRESHOLD,
    LEGEND_FUZZY_THRESHOLD, NAME_CONFIDENCE_THRESHOLD,
    TEMPORAL_WINDOW, TEMPORAL_THRESHOLD,
)

# Path to the ranked leaderboard CSV (put apex_ranked_leaderboard.csv next to your scripts)
APEX_LEADERBOARD_PATH = Path("apex_ranked_leaderboard.csv")


class PlayerDatabase:
    """Manages player name database with temporal fuzzy matching and variant tracking."""

    def __init__(self):
        self.player_database = {}
        self.legend_typo_database = {}

        # Temporal tracking for recent names
        self.recent_names = defaultdict(list)  # {canonical_name: [(timestamp, variant), ...]}
        self.temporal_window = TEMPORAL_WINDOW
        self.temporal_threshold = TEMPORAL_THRESHOLD

    def load_databases(self):
        """Load both player and legend databases from disk."""
        self.load_player_database()
        self.load_legend_typo_database()
        self.seed_legend_names()
        self.load_top_players()  # NEW: seed pro players

    def load_player_database(self):
        """Load existing player database from disk."""
        if PLAYER_DB_PATH.exists():
            try:
                with PLAYER_DB_PATH.open("r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        self.player_database = json.loads(content)
                        print(f"Loaded {len(self.player_database)} players from database.")
                    else:
                        print("Player database file is empty. Starting fresh.")
                        self.player_database = {}
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Error loading player database: {e}")
                print("Starting with empty player database.")
                self.player_database = {}
        else:
            self.player_database = {}
            print("Starting with empty player database.")

    def save_player_database(self):
        """Save player database to disk."""
        with PLAYER_DB_PATH.open("w", encoding="utf-8") as f:
            json.dump(self.player_database, f, indent=2, ensure_ascii=False)

    def load_legend_typo_database(self):
        """Load learned legend typo mappings from disk."""
        if LEGEND_TYPO_DB_PATH.exists():
            try:
                with LEGEND_TYPO_DB_PATH.open("r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        self.legend_typo_database = json.loads(content)
                        print(f"Loaded {len(self.legend_typo_database)} legend typo mappings.\n")
                    else:
                        print("Legend typo database file is empty. Using defaults.\n")
                        self.legend_typo_database = DEFAULT_LEGEND_TYPOS.copy()
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Error loading legend typo database: {e}")
                print("Using default legend typo mappings.\n")
                self.legend_typo_database = DEFAULT_LEGEND_TYPOS.copy()
        else:
            self.legend_typo_database = DEFAULT_LEGEND_TYPOS.copy()
            print("Starting with default legend typo mappings.\n")

    def save_legend_typo_database(self):
        """Save learned legend typo mappings to disk."""
        with LEGEND_TYPO_DB_PATH.open("w", encoding="utf-8") as f:
            json.dump(self.legend_typo_database, f, indent=2, ensure_ascii=False)

    def seed_legend_names(self):
        """Add all legend names to the player database as protected entries."""
        for legend in APEX_LEGENDS_CANONICAL:
            if legend not in self.player_database:
                self.player_database[legend] = {
                    "variants": {legend: 999},
                    "total_seen": 999,
                    "last_seen": time.time(),
                    "protected": True
                }

    def load_top_players(self):
        """Load top Apex players from leaderboard CSV and seed as protected 'pro' entries."""
        if not APEX_LEADERBOARD_PATH.exists():
            print("Top players leaderboard file not found, skipping pro seeding.")
            return

        try:
            import csv as _csv
            with APEX_LEADERBOARD_PATH.open("r", encoding="utf-8", newline="") as f:
                # Expect header: Rank,Player,RP. Proper CSV parsing -- player names
                # containing commas are quoted by update_leaderboard.py's writer.
                reader = _csv.reader(f)
                next(reader, None)
                added = 0

                for parts in reader:
                    if len(parts) < 2:
                        continue

                    player = parts[1].strip()
                    if not player or player == "- Empty name -":
                        continue

                    # Already in DB: just mark as pro/protected
                    if player in self.player_database:
                        entry = self.player_database[player]
                        entry.setdefault("protected", True)
                        entry["pro"] = True
                        continue

                    # Seed as a high-confidence protected pro player
                    now = time.time()
                    self.player_database[player] = {
                        "variants": {player: 999},
                        "total_seen": 999,
                        "last_seen": now,
                        "protected": True,
                        "pro": True,
                    }
                    added += 1

            print(f"Seeded {added} top players from leaderboard as protected 'pro' entries.")
        except Exception as e:
            print(f"Error loading top players from leaderboard: {e}")

    def cleanup_recent_names(self, now: float):
        """Remove old entries from recent names cache."""
        cutoff = now - self.temporal_window

        for canonical_name in list(self.recent_names.keys()):
            # Filter out old timestamps
            self.recent_names[canonical_name] = [
                (ts, variant) for ts, variant in self.recent_names[canonical_name]
                if ts > cutoff
            ]

            # Remove empty entries
            if not self.recent_names[canonical_name]:
                del self.recent_names[canonical_name]

    def add_to_recent(self, canonical_name: str, variant: str, timestamp: float):
        """Track a name in the recent names cache."""
        self.recent_names[canonical_name].append((timestamp, variant))

        # Keep only last 20 entries per canonical name to prevent memory bloat
        if len(self.recent_names[canonical_name]) > 20:
            self.recent_names[canonical_name] = self.recent_names[canonical_name][-20:]

    @staticmethod
    def fuzzy_match_ratio(s1: str, s2: str) -> float:
        """Return similarity ratio between two strings (0.0 to 1.0)."""
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    # ---- Confusion-aware canonicalization (bead 1gn; advisor-vetted split freeform/anon) ----
    # Font-confusion classes are HAND-SPECIFIED (not mined from jitter pairs, which contain distinct
    # anon players = the vmu failure). Freeform names fold letter/shape confusions; anonymized
    # Legend#### names get STRUCTURED handling so digit distinctions that separate players are kept.
    _FREEFORM_FOLD = str.maketrans({
        'e': 'a',                          # a<->e (top confusion)
        '0': 'o', 'c': 'o', 'd': 'o',      # rounded: o<->0<->c<->d
        '1': 'i', 'l': 'i', 't': 'i',      # vertical stroke: i<->l<->1<->t
        'v': 'u', 'y': 'u',                # u<->v<->y
        '8': 'b', '6': 'b',                # curvy: b<->8<->6
        '9': 'g', 'q': 'g',                # g<->9<->q
        '5': 's',                          # s<->5
    })
    _DIGIT_RECOVER = str.maketrans('ilostbgqz', '110518992')  # letters OCR'd from digits -> digits

    @classmethod
    def _freeform_key(cls, name: str) -> str:
        """Confusion-folded key for a freeform name: alnum-only, doubled-char-collapsed, class-folded."""
        s = ''.join(ch for ch in name.lower() if ch.isalnum())
        s = re.sub(r'(.)\1+', r'\1', s)          # collapse doubled letters (ll->l, ee->e) for indel jitter
        return s.translate(cls._FREEFORM_FOLD)

    @classmethod
    def _anon_key(cls, name: str):
        """Structured identity key for an anonymized Legend#### player, else None.
        Returns (corrected_legend, digit_block). The 4-digit block is digit-shape-recovered (i->1,
        o->0, s->5, b->8, g->9) but NOT fuzzed -- two clean-but-different numbers stay distinct, so
        this never merges distinct players (vmu-safe by construction)."""
        n = name.strip().lower()
        m = re.match(r'^([a-z .\-_]{3,}?)[ .\-_]*([a-z0-9]{3,4})$', n)
        if not m:
            return None
        prefix = m.group(1).replace(' ', '')
        dig = m.group(2).translate(cls._DIGIT_RECOVER)
        if not re.fullmatch(r'\d{3,4}', dig):
            return None
        best, best_r = None, 0.0
        for legend in APEX_LEGENDS_CANONICAL:
            r = SequenceMatcher(None, prefix, legend.lower().replace(' ', '')).ratio()
            if r > best_r:
                best, best_r = legend.lower().replace(' ', ''), r
        if best_r < 0.6:
            return None
        return (best, dig)

    @classmethod
    def _confusion_same_identity(cls, a: str, b: str) -> bool:
        """True if a and b are the same identity under confusion-aware canonicalization."""
        ka, kb = cls._anon_key(a), cls._anon_key(b)
        if ka is not None and kb is not None:
            return ka == kb                       # both anon: structured key must match exactly
        if ka is not None or kb is not None:
            return False                          # one anon, one not: never the same
        return cls._freeform_key(a) == cls._freeform_key(b) and len(cls._freeform_key(a)) >= 4

    @staticmethod
    def _anon_digit_conflict(a: str, b: str) -> bool:
        """True if a and b are DISTINCT anonymized players that must not be merged.

        Anonymized Apex players render as 'Legend####' (e.g. maggie8793, wraith5812). Two such
        names sharing the legend prefix score a high fuzzy ratio (maggie5636 vs maggie8793 = 0.70)
        and were being wrongly merged by the relaxed temporal matcher -- collapsing two real,
        different players into one identity and inflating that player's ELO (measured 2026-07-11).
        Guard: when BOTH names end in a >=3-digit run, the digit suffixes must themselves be
        OCR-compatible (ratio >= 0.6). This blocks 8793 vs 5636 (ratio 0.0 -> conflict) while still
        allowing genuine OCR variants of the SAME player -- 8793 vs 8783 (0.75), or wraith5.812 vs
        wraith5812 (suffix 812 vs 5812 = 0.86).
        """
        ma = re.search(r'(\d{3,})$', a or '')
        mb = re.search(r'(\d{3,})$', b or '')
        if not (ma and mb):
            return False
        return SequenceMatcher(None, ma.group(1), mb.group(1)).ratio() < 0.6

    def find_recent_match(self, name: str, now: float) -> Optional[Tuple[str, float]]:
        """Check if this name matches any recently seen names with relaxed threshold.

        Returns: (canonical_name, confidence) if match found, else None
        """
        cutoff = now - self.temporal_window
        best_match = None
        best_ratio = 0.0
        len1 = len(name)

        for canonical_name, recent_list in self.recent_names.items():
            len2 = len(canonical_name)
            if max(len1, len2) <= 1.86 * min(len1, len2):
                # Check against canonical name
                ratio = self.fuzzy_match_ratio(name, canonical_name)
                if (ratio > best_ratio and ratio >= self.temporal_threshold
                        and not self._anon_digit_conflict(name, canonical_name)):
                    best_match = canonical_name
                    best_ratio = ratio

            # Check against recent variants
            for ts, variant in recent_list:
                if ts < cutoff:
                    continue

                len2 = len(variant)
                if max(len1, len2) > 1.86 * min(len1, len2):
                    continue

                ratio = self.fuzzy_match_ratio(name, variant)
                if (ratio > best_ratio and ratio >= self.temporal_threshold
                        and not self._anon_digit_conflict(name, variant)):
                    best_match = canonical_name
                    best_ratio = ratio

        if best_match:
            return best_match, best_ratio

        return None

    def detect_and_learn_legend_typo(self, token: str) -> Optional[str]:
        """Check if token is a typo of a legend name.

        Returns: canonical legend name if matched, otherwise None
        """
        if not token or len(token) < 3:
            return None

        # Don't process legend+number combinations (anonymized players) or tokens with digits
        if self.is_legend_with_number(token) or any(c.isdigit() for c in token):
            return None

        token_low = token.lower().replace(" ", "")

        # Check if we already know this typo
        if token_low in self.legend_typo_database:
            return self.legend_typo_database[token_low]

        # Check for fuzzy match to any legend
        best_legend = None
        best_ratio = 0.0
        len1 = len(token)

        for legend in APEX_LEGENDS_CANONICAL:
            len2 = len(legend)
            if max(len1, len2) > 1.5 * min(len1, len2):
                continue
            ratio = self.fuzzy_match_ratio(token, legend)
            if ratio > best_ratio and ratio > LEGEND_FUZZY_THRESHOLD:
                best_legend = legend
                best_ratio = ratio

        if best_legend:
            # Learn this new typo!
            self.legend_typo_database[token_low] = best_legend
            print(f"  [LEGEND] Learned new typo: '{token}' → '{best_legend}' (similarity: {best_ratio:.2%})")
            self.save_legend_typo_database()
            return best_legend

        return None

    @staticmethod
    def is_legend_with_number(token: str) -> bool:
        """Check if token is a legend name followed by 4 digits (anonymized player)."""
        if not token or len(token) < 5:
            return False

        import re
        for legend in APEX_LEGENDS_CANONICAL:
            legend_low = legend.lower()
            token_low = token.lower()

            if token_low.startswith(legend_low):
                remainder = token[len(legend):]
                if re.fullmatch(r'\d{4}', remainder):
                    return True

        return False

    def is_legend_name(self, token: str) -> bool:
        """Check if a token is a legend name (should be filtered out)."""
        # Keep legend+number combinations (anonymized players)
        if self.is_legend_with_number(token):
            return False

        # If it has any digits, it's a player name variant, not a legend name!
        if any(c.isdigit() for c in token):
            return False

        token_low = token.lower().replace(" ", "")

        # Check exact match
        if token.title() in APEX_LEGENDS_CANONICAL or token in APEX_LEGENDS_CANONICAL:
            return True

        # Check typo database
        if token_low in self.legend_typo_database:
            return True

        # Check fuzzy match to any legend
        len1 = len(token)
        for legend in APEX_LEGENDS_CANONICAL:
            len2 = len(legend)
            if max(len1, len2) > 1.5 * min(len1, len2):
                continue
            if self.fuzzy_match_ratio(token, legend) > LEGEND_FUZZY_THRESHOLD:
                return True

        return False

    def get_name_confidence_score(self, canonical_name: str) -> float:
        """Calculate confidence score for a canonical name."""
        if canonical_name not in self.player_database:
            return 0.0

        entry = self.player_database[canonical_name]
        total_seen = entry["total_seen"]
        time_since = time.time() - entry.get("last_seen", 0)

        # Higher frequency = higher confidence
        frequency_score = min(total_seen / 10.0, 1.0)

        # More recent = higher confidence (decay over 7 days)
        recency_score = max(0, 1.0 - (time_since / (7 * 24 * 3600)))

        return 0.7 * frequency_score + 0.3 * recency_score

    def find_best_canonical_match(self, name: str, timestamp: Optional[float] = None) -> Tuple[str, float]:
        """Find best matching canonical name from database with temporal awareness.

        Args:
            name: The name to match
            timestamp: Optional timestamp for temporal matching

        Returns: (canonical_name, confidence_score)
        """
        if not name or len(name) < PLAYER_NAME_MIN_LENGTH:
            return name, 0.0

        now = timestamp if timestamp else time.time()

        # STEP 1: Check recent names first with relaxed threshold
        recent_match = self.find_recent_match(name, now)
        if recent_match:
            canonical, confidence = recent_match
            if confidence < 1.0:  # skip logging exact matches (100%) — too noisy
                print(f"  [TEMPORAL] Matched '{name}' -> '{canonical}' (similarity: {confidence:.2%}, recent)")
            return canonical, confidence

        # STEP 2: Check if this is a legend name variant (but not legend+number)
        if not self.is_legend_with_number(name):
            canonical_legend = self.detect_and_learn_legend_typo(name)
            if canonical_legend:
                return canonical_legend, 1.0

        # STEP 3: Check against all names and historical variants in database
        best_match = None
        best_score = 0.0
        len1 = len(name)

        for canonical_name, entry in self.player_database.items():
            if canonical_name == name:
                if entry.get("protected", False):
                    return name, 1.0
                confidence = self.get_name_confidence_score(canonical_name)
                return name, 0.6 + 0.4 * confidence

            # Check similarity against canonical name
            similarity = 0.0
            len2 = len(canonical_name)
            if max(len1, len2) <= 1.36 * min(len1, len2):
                similarity = self.fuzzy_match_ratio(name, canonical_name)

            # Also check against all historical variants learned for this canonical name
            best_variant_similarity = 0.0
            for variant in entry.get("variants", {}):
                len2 = len(variant)
                if max(len1, len2) <= 1.36 * min(len1, len2):
                    var_similarity = self.fuzzy_match_ratio(name, variant)
                    if var_similarity > best_variant_similarity:
                        best_variant_similarity = var_similarity

            # Pick the higher similarity of the canonical name or any variant
            match_similarity = max(similarity, best_variant_similarity)

            # Confusion-aware canonicalization (bead 1gn): if name and this canonical (or any of its
            # variants) share a confusion key, treat as a strong match even when raw fuzzy fell below
            # threshold -- this folds systematic OCR jitter (axle8b44/axlebb44, Wraith1052/Wraithios2)
            # that plain SequenceMatcher misses. Anon names use the structured digit-exact key, so this
            # cannot merge distinct players.
            if (self._confusion_same_identity(name, canonical_name)
                    or any(self._confusion_same_identity(name, v) for v in entry.get("variants", {}))):
                match_similarity = max(match_similarity, 0.95)

            if match_similarity < FUZZY_MATCH_THRESHOLD:
                continue

            # Never merge two distinct anonymized players (Legend#### with different digits).
            if self._anon_digit_conflict(name, canonical_name):
                continue

            confidence = self.get_name_confidence_score(canonical_name)
            is_pro = entry.get("pro", False)
            is_protected = entry.get("protected", False)

            # Weighting: prefer pro players, then legends, then normal names
            if is_pro:
                combined_score = 0.8 * match_similarity + 0.2 * confidence
            elif is_protected:
                combined_score = 0.4 * match_similarity + 0.6 * 1.0
            else:
                combined_score = 0.6 * match_similarity + 0.4 * confidence

            if combined_score > best_score:
                best_match = canonical_name
                best_score = combined_score

        return best_match or name, best_score

    def add_name_observation(self, name: str, timestamp: Optional[float] = None):
        """Record a name observation and update the database with temporal tracking.

        Args:
            name: The player name to record
            timestamp: Optional timestamp for temporal clustering
        """
        if not name or len(name) < PLAYER_NAME_MIN_LENGTH:
            return

        now = timestamp if timestamp else time.time()

        # Cleanup old recent names periodically
        self.cleanup_recent_names(now)

        # Check if this is a legend name variant (but not legend+number)
        if not self.is_legend_with_number(name):
            canonical_legend = self.detect_and_learn_legend_typo(name)

            if canonical_legend:
                if canonical_legend not in self.player_database:
                    self.player_database[canonical_legend] = {
                        "variants": {canonical_legend: 999},
                        "total_seen": 999,
                        "last_seen": now,
                        "protected": True
                    }

                entry = self.player_database[canonical_legend]
                entry["variants"][name] = entry["variants"].get(name, 0) + 1
                return

        # Find if this matches an existing canonical name
        canonical, match_score = self.find_best_canonical_match(name, now)

        if canonical == name or match_score < 0.5:
            # This is a new canonical name or weak match - create/update entry
            if name not in self.player_database:
                self.player_database[name] = {
                    "variants": {},
                    "total_seen": 0,
                    "last_seen": now,
                    "protected": False
                }

            entry = self.player_database[name]
            entry["variants"][name] = entry["variants"].get(name, 0) + 1
            entry["total_seen"] += 1
            entry["last_seen"] = now

            # Add to recent names
            self.add_to_recent(name, name, now)
        else:
            # This is a variant of an existing canonical name
            entry = self.player_database[canonical]
            entry["variants"][name] = entry["variants"].get(name, 0) + 1
            entry["total_seen"] += 1
            entry["last_seen"] = now

            # Add to recent names
            self.add_to_recent(canonical, name, now)

            # Check if this variant should become the new canonical
            # BUT: never replace protected (legend/pro) names
            if not entry.get("protected", False):
                variant_count = entry["variants"][name]
                canonical_count = entry["variants"].get(canonical, 0)

                if variant_count > canonical_count and variant_count >= NAME_CONFIDENCE_THRESHOLD:
                    print(f"  [DB] Promoting '{name}' over '{canonical}' ({variant_count} vs {canonical_count})")

                    # Transfer all data to new canonical
                    self.player_database[name] = self.player_database.pop(canonical)

                    # Update recent names cache
                    if canonical in self.recent_names:
                        self.recent_names[name] = self.recent_names.pop(canonical)

    def normalize_player_name(self, name: str, timestamp: Optional[float] = None) -> Optional[str]:
        """Normalize a player name using the database with temporal awareness.

        Args:
            name: The player name to normalize
            timestamp: Optional timestamp for temporal clustering

        Returns: Canonical player name or None
        """
        canonical, _ = self.normalize_player_name_with_confidence(name, timestamp)
        return canonical

    def normalize_player_name_with_confidence(
        self, name: str, timestamp: Optional[float] = None
    ) -> Tuple[Optional[str], float]:
        """Normalize a player name and return the fuzzy match confidence score.

        Returns: (canonical_name, confidence) where confidence is 0.0–1.0.
            1.0 = exact match or legend/pro entry
            0.0 = name too short or not found
        """
        if not name or len(name) < PLAYER_NAME_MIN_LENGTH:
            return name, 0.0

        now = timestamp if timestamp else time.time()

        # Check manual player name corrections first
        from config import PLAYER_NAME_CORRECTIONS
        name_low = name.lower()
        if name_low in PLAYER_NAME_CORRECTIONS:
            corrected = PLAYER_NAME_CORRECTIONS[name_low]
            # Ensure the corrected canonical name is added to the database
            if corrected not in self.player_database:
                self.player_database[corrected] = {
                    "variants": {},
                    "total_seen": 0,
                    "last_seen": now,
                    "protected": True
                }
            entry = self.player_database[corrected]
            entry["variants"][name] = entry["variants"].get(name, 0) + 1
            entry["total_seen"] += 1
            entry["last_seen"] = now
            self.add_to_recent(corrected, name, now)
            return corrected, 1.0

        canonical, confidence = self.find_best_canonical_match(name, now)
        self.add_name_observation(name, now)
        return canonical, round(confidence, 4)

    def print_summary(self):
        """Print database summary."""
        players = {k: v for k, v in self.player_database.items() if not v.get("protected", False)}
        print(f"\n\n=== Player Database Summary ===")
        print(f"Total unique players: {len(players)}")
        print(f"Total legend typo mappings learned: {len(self.legend_typo_database)}")
        print(f"Active temporal clusters: {len(self.recent_names)}\n")

        sorted_players = sorted(
            players.items(),
            key=lambda x: x[1]["total_seen"],
            reverse=True
        )

        print("Top 50 players:")
        for name, data in sorted_players[:50]:
            variants_str = ", ".join(
                f"{v}({c})" for v, c in sorted(data["variants"].items(), key=lambda x: x[1], reverse=True)[:3]
            )
            print(f"  {name:20s} - seen {data['total_seen']:3d}x - variants: {variants_str}")
