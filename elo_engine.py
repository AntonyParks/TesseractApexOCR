"""Pairwise ELO calculation for Apex killfeed match sessions.

ELO model:
    - Each match produces pairwise comparisons between players based on survival order.
    - The player who survived longer (higher kill_order at elimination) wins the matchup.
    - Implicit survival credit: an attacker who made kill at order K is treated as
      outlasting any victim killed at order <= K (if attacker was never eliminated before K).
    - K-factor: 32 for first 20 rated matches, 16 thereafter.
    - Starting ELO: 1000.
"""

import re
from pathlib import Path

from config import APEX_LEGENDS_CANONICAL, COMMON_WORDS, PLAYER_NAME_MIN_LENGTH, PLAYER_NAME_MAX_DIGIT_RATIO
from elo_db import (
    ELO_DB_PATH, get_player_rating, update_player_rating,
    upsert_match, upsert_match_kills, upsert_placement,
)
from match_detector import Match, get_player_survival, CONF_FLOOR

STARTING_ELO = 1000.0
ELO_FLOOR = 100.0
K_HIGH = 32.0   # first 20 matches
K_LOW = 16.0    # after 20 matches
K_THRESHOLD = 20


def k_factor(matches_played: int) -> float:
    return K_HIGH if matches_played < K_THRESHOLD else K_LOW


KILL_DEDUP_WINDOW_SECONDS = 120   # same respawn window the golden harness uses

_KD_NORM = re.compile(r"[^a-z0-9]")

def dedup_kill_rows(kills: list) -> list:
    """Collapse duplicate READS of one death for stat counting: the same (attacker, victim) within
    KILL_DEDUP_WINDOW_SECONDS is ONE kill, not two.

    Why: db_log's sticky-chain keeps up to STICKY_ELO_CHAIN_MAX_ROWS (=2) near-identical rows per
    finish, and match_detector counts rows with no victim-dedup, so total_kills/total_deaths were
    over-counted ~1.5x (measured on the golden game via tools/golden/replay_dblog.py). A GENUINE
    respawn re-death -- same victim re-killed beyond the window, or by a different attacker -- is not
    a duplicate and is preserved, so real re-kills still count. Order-preserving; only the stat
    accumulation uses this. The pairwise ELO rating path is untouched (it reads match.kills directly
    and is already respawn-safe: elimination_order is player-keyed)."""
    def nk(s):
        return _KD_NORM.sub("", (s or "").lower())
    last: dict = {}   # (attacker_norm, victim_norm) -> last kept timestamp
    out = []
    for k in sorted(kills, key=lambda x: x.timestamp):
        key = (nk(k.attacker), nk(k.victim))
        prev = last.get(key)
        if prev is not None and (k.timestamp - prev).total_seconds() <= KILL_DEDUP_WINDOW_SECONDS:
            last[key] = k.timestamp     # extend the window; still the same death, drop it
            continue
        last[key] = k.timestamp
        out.append(k)
    return out


def expected_score(rating_a: float, rating_b: float) -> float:
    """Probability that A beats B."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _get_or_default(player: str, ratings_cache: dict, db_path: Path) -> dict:
    if player not in ratings_cache:
        existing = get_player_rating(player, db_path)
        ratings_cache[player] = existing or {
            "player": player,
            "elo": STARTING_ELO,
            "matches_played": 0,
            "total_kills": 0,
            "total_deaths": 0,
            "peak_elo": STARTING_ELO,
        }
    return ratings_cache[player]


def process_match(match: Match, ratings_cache: dict, db_path: Path = ELO_DB_PATH) -> dict:
    """Calculate pairwise ELO updates for one match.

    Returns a dict of {player: new_elo} for all affected players.
    Also writes match, kills, and placements to the DB.

    ratings_cache is mutated in-place so that ELO updates carry forward
    across a batch of matches processed in chronological order.
    """
    elimination_order, last_alive_order = get_player_survival(match)

    # --- Write match metadata ---
    upsert_match(
        match_id=match.match_id,
        streamer=match.streamer,
        start_time=match.start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=match.end_time.strftime("%Y-%m-%d %H:%M:%S"),
        kill_count=match.kill_count,
        players_observed=match.players_observed,
        merged_from=",".join(getattr(match, "merged_from", []) or []),
        path=db_path,
    )

    # --- Write kill events ---
    kill_rows = [
        {
            "match_id": match.match_id,
            "timestamp": k.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "attacker": k.attacker,
            "victim": k.victim,
            "kill_order": k.kill_order,
            "attacker_conf": k.attacker_conf,
            "victim_conf": k.victim_conf,
        }
        for k in match.kills
    ]
    upsert_match_kills(kill_rows, path=db_path)

    # --- Update kill/death stats (respawn-aware de-dup: one death per (attacker, victim) window,
    # so db_log's cap-2 duplicate rows don't inflate the counts; real re-kills still count) ---
    for k in dedup_kill_rows(match.kills):
        if k.attacker and not _is_legend_name(k.attacker):
            r = _get_or_default(k.attacker, ratings_cache, db_path)
            r["total_kills"] += 1
        if k.victim and not _is_legend_name(k.victim):
            r = _get_or_default(k.victim, ratings_cache, db_path)
            r["total_deaths"] += 1

    # --- Build pairwise comparisons ---
    # Collect all players with a known survival position in this match.
    # Each player gets an "effective_order" for comparison:
    #   - Victims with definitive elimination: their elimination kill_order
    #   - Attacker-only: their last_alive_order (minimum floor — they survived at least this long)
    #     We mark them as "attacker_only=True" so we only use them vs victims, not vs each other.

    # Filter victims by confidence threshold
    eligible_victims = {
        p: order for p, order in elimination_order.items()
        if _victim_conf_ok(p, match)
    }

    # Attacker-only players (never eliminated on stream in this match)
    all_seen = set()
    for k in match.kills:
        if k.attacker:
            all_seen.add(k.attacker)
        if k.victim:
            all_seen.add(k.victim)

    attacker_only = {
        p: last_alive_order[p]
        for p in all_seen
        if p not in elimination_order and p in last_alive_order and not _is_legend_name(p)
    }

    # --- Cap at 60 players (Apex match size) by best OCR confidence ---
    _MAX_PLAYERS = 60
    all_candidates = set(eligible_victims) | set(attacker_only)
    if len(all_candidates) > _MAX_PLAYERS:
        best_conf: dict[str, float] = {}
        for k in match.kills:
            if k.attacker and k.attacker in all_candidates:
                best_conf[k.attacker] = max(best_conf.get(k.attacker, 0.0), k.attacker_conf)
            if k.victim and k.victim in all_candidates:
                best_conf[k.victim] = max(best_conf.get(k.victim, 0.0), k.victim_conf)
        top_players = {p for p, _ in sorted(best_conf.items(), key=lambda x: -x[1])[:_MAX_PLAYERS]}
        eligible_victims = {p: o for p, o in eligible_victims.items() if p in top_players}
        attacker_only    = {p: o for p, o in attacker_only.items()    if p in top_players}

    # Accumulate ELO deltas across all pairwise comparisons
    # Use a temporary delta dict so all updates in this match happen simultaneously
    elo_deltas: dict[str, float] = {}

    victims = list(eligible_victims.items())

    # Case 1: victim vs victim — both have definitive elimination orders
    for i in range(len(victims)):
        p_a, order_a = victims[i]
        for j in range(i + 1, len(victims)):
            p_b, order_b = victims[j]
            if p_a == p_b:
                continue

            r_a = _get_or_default(p_a, ratings_cache, db_path)
            r_b = _get_or_default(p_b, ratings_cache, db_path)

            elo_a = r_a["elo"]
            elo_b = r_b["elo"]

            # Higher order = survived longer = won
            if order_a < order_b:
                # B outlasted A → B won
                e_b = expected_score(elo_b, elo_a)
                k_b = k_factor(r_b["matches_played"])
                k_a = k_factor(r_a["matches_played"])
                elo_deltas[p_b] = elo_deltas.get(p_b, 0.0) + k_b * (1.0 - e_b)
                elo_deltas[p_a] = elo_deltas.get(p_a, 0.0) + k_a * (0.0 - (1.0 - e_b))
            elif order_b < order_a:
                # A outlasted B → A won
                e_a = expected_score(elo_a, elo_b)
                k_a = k_factor(r_a["matches_played"])
                k_b = k_factor(r_b["matches_played"])
                elo_deltas[p_a] = elo_deltas.get(p_a, 0.0) + k_a * (1.0 - e_a)
                elo_deltas[p_b] = elo_deltas.get(p_b, 0.0) + k_b * (0.0 - (1.0 - e_a))
            # Exact tie in kill_order is impossible (unique kill events)

    # Case 2: victim vs attacker-only — implicit survival credit
    for victim, v_order in eligible_victims.items():
        r_v = _get_or_default(victim, ratings_cache, db_path)
        for attacker, a_floor in attacker_only.items():
            if attacker == victim:
                continue
            if a_floor < v_order:
                # Attacker's last known alive was before victim died → unknown, skip
                continue

            # Attacker was alive at a_floor >= v_order → attacker outlasted victim
            r_a = _get_or_default(attacker, ratings_cache, db_path)
            elo_a = r_a["elo"]
            elo_v = r_v["elo"]

            e_a = expected_score(elo_a, elo_v)
            k_a = k_factor(r_a["matches_played"])
            k_v = k_factor(r_v["matches_played"])
            elo_deltas[attacker] = elo_deltas.get(attacker, 0.0) + k_a * (1.0 - e_a)
            elo_deltas[victim] = elo_deltas.get(victim, 0.0) + k_v * (0.0 - (1.0 - e_a))

    # --- Apply deltas and write placements ---
    affected_players = set(elo_deltas) | set(eligible_victims) | set(attacker_only)

    for player in affected_players:
        r = _get_or_default(player, ratings_cache, db_path)
        elo_before = r["elo"]
        delta = elo_deltas.get(player, 0.0)
        elo_after = max(ELO_FLOOR, elo_before + delta)
        r["elo"] = elo_after
        r["matches_played"] = r["matches_played"] + 1

        # Placement row for everyone with a known survival position. Eliminated players
        # store their elimination kill_order; survivors (attacker-only, never definitively
        # eliminated on stream) store their survival FLOOR with survived=1 so the API can
        # distinguish "died as kill N" from "still alive at least until kill N".
        kill_order_out = elimination_order.get(player) or attacker_only.get(player, 0)
        if kill_order_out:
            upsert_placement(
                match_id=match.match_id,
                player=player,
                kill_order_out=kill_order_out,
                elo_before=elo_before,
                elo_after=elo_after,
                survived=1 if player in attacker_only else 0,
                path=db_path,
            )

    return {p: ratings_cache[p]["elo"] for p in affected_players}


def _is_anonymized_player(name: str) -> bool:
    if not name:
        return False
    name_low = name.lower().strip()

    # Build list of legend patterns (canonical names, lowercase, and common abbreviations/typos)
    legend_patterns = set()
    for legend in APEX_LEGENDS_CANONICAL:
        l_low = legend.lower()
        legend_patterns.add(l_low)
        legend_patterns.add(l_low.replace(" ", ""))
        legend_patterns.add(l_low.replace(" ", "-"))
        if l_low == "valkyrie":
            legend_patterns.add("valk")
        if l_low == "madmaggie":
            legend_patterns.add("maggie")

    # Check if name is bracketed legend name (e.g. [valk], (octane))
    clean = re.sub(r'[\[\]\(\)]', '', name_low).strip()
    if clean in legend_patterns:
        return True

    # Check if name starts with a legend name and ends with numbers (e.g. Valkyrie4823, valk683)
    for l_pat in legend_patterns:
        if name_low.startswith(l_pat):
            remainder = name_low[len(l_pat):].strip()
            if remainder and remainder.isdigit():
                return True

    return False


_COMMON_WORDS_SET = {w.lower() for w in COMMON_WORDS}


def _is_valid_player(name: str) -> bool:
    """Return False for legend names, too-short names, common words, and OCR noise."""
    if name in APEX_LEGENDS_CANONICAL:
        return False
    if _is_anonymized_player(name):
        return False
    if len(name) < PLAYER_NAME_MIN_LENGTH:
        return False
    if name.lower() in _COMMON_WORDS_SET:
        return False
    # Filter out repeated-character noise (e.g. "EEEEE", "ieee", "teee")
    alpha = [c for c in name if c.isalpha()]
    if alpha and len(set(alpha)) <= 2 and len(alpha) >= 4:
        return False
    # Filter latency/ping artifacts: "36ms", "3bms", "37nds", "3anns"
    if re.match(r'^\d+[a-z]{1,4}$', name.lower()):
        return False
    # Filter hash/code noise: "48987210s17s5s3b5bcd1" (>40% digits)
    if sum(c.isdigit() for c in name) / len(name) > PLAYER_NAME_MAX_DIGIT_RATIO:
        return False
    # Filter Twitch overlay text: "twitch.tvsto", "twitch.ivstdneyg"
    if name.lower().startswith("twitch."):
        return False
    # Filter ping display artifacts: "ping_34ms", "ping.3bsmk"
    if re.match(r'^ping[_.]', name.lower()):
        return False
    # Filter [Bleed Out] tag fragments leaking into names (e.g. "int[Bleed", "Out]m") --
    # surfaced when BleedOut rows became kill-equivalents (2026-07-04).
    if '[bleed' in name.lower() or 'bleed]' in name.lower():
        return False
    # Filter names that are ONLY a clan tag (e.g. "[x78]") -- a tag with the actual
    # name lost to OCR is not an identity we can rate.
    if re.fullmatch(r'\[[^\]]*\]', name):
        return False
    return True


def _is_legend_name(name: str) -> bool:
    return not _is_valid_player(name)


def _victim_conf_ok(player: str, match: Match) -> bool:
    """Check if this player's victim events pass the confidence threshold."""
    if _is_legend_name(player):
        return False
    for k in match.kills:
        if k.victim == player and k.victim_conf >= CONF_FLOOR:
            return True
    return False


def batch_reprocess(matches: list[Match], db_path: Path = ELO_DB_PATH) -> dict:
    """Process all matches in chronological order, updating ELO incrementally.

    Returns final ratings_cache dict with all player ELO values.
    """
    ratings_cache: dict[str, dict] = {}

    for i, match in enumerate(matches, 1):
        process_match(match, ratings_cache, db_path)
        if i % 10 == 0 or i == len(matches):
            print(f"  Processed {i}/{len(matches)} matches...")

    # Persist all ratings to DB
    print(f"  Writing {len(ratings_cache)} player ratings to DB...")
    for player, r in ratings_cache.items():
        update_player_rating(
            player=player,
            elo=r["elo"],
            matches_played=r["matches_played"],
            total_kills=r["total_kills"],
            total_deaths=r["total_deaths"],
            path=db_path,
        )

    return ratings_cache
