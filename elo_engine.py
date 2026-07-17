"""Glicko-2 rating calculation for Apex killfeed match sessions.

Rating model:
    - Each match produces pairwise-survival comparisons: the player who survived longer (higher
      kill_order at elimination) beats the other. A survivor (never observed dying) is credited
      against ALL confirmed-dead and never compared to other survivors (rma decision #5).
    - All of a match's comparisons for one player are pooled into a SINGLE Glicko-2 update, so
      swing magnitude tracks opponent STRENGTH while opponent COUNT shrinks the deviation (rd) —
      see glicko.py. This replaces the old sum-of-per-opponent K-factor ELO (which let a well-
      observed match spike a rating; that was the small-sample inflation bug, 2026-07-16).
    - Leaderboard ranks by the conservative estimate mu - 2*rd (elo_db.get_rankings).
    - Starting rating 1000, rd 350, vol 0.06 (glicko defaults).
"""

import re
from pathlib import Path

import glicko
from config import APEX_LEGENDS_CANONICAL, COMMON_WORDS, PLAYER_NAME_MIN_LENGTH, PLAYER_NAME_MAX_DIGIT_RATIO
from elo_db import (
    ELO_DB_PATH, get_player_rating, update_player_rating,
    upsert_match, upsert_match_kills, upsert_placement,
)
from match_detector import Match, get_player_survival

STARTING_ELO = glicko.RATING0
ELO_FLOOR = 100.0


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


def _get_or_default(player: str, ratings_cache: dict, db_path: Path) -> dict:
    if player not in ratings_cache:
        existing = get_player_rating(player, db_path)
        if existing:
            existing.setdefault("rd", glicko.RD0)
            existing.setdefault("vol", glicko.VOL0)
        ratings_cache[player] = existing or {
            "player": player,
            "elo": glicko.RATING0,
            "rd": glicko.RD0,
            "vol": glicko.VOL0,
            "matches_played": 0,
            "total_kills": 0,
            "total_deaths": 0,
            "peak_elo": glicko.RATING0,
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

    # Collect each player's pairwise results for this match, then run ONE Glicko-2 update per
    # player (below). Opponent ratings are read PRE-match (ratings_cache isn't mutated until the
    # apply step), so every comparison in a match uses the same fixed opponent ratings — a proper
    # Glicko rating period. A result is (opp_rating, opp_rd, score): score 1.0 = outlasted them.
    results: dict[str, list] = {}

    def _record(winner: str, loser: str) -> None:
        rw = _get_or_default(winner, ratings_cache, db_path)
        rl = _get_or_default(loser, ratings_cache, db_path)
        results.setdefault(winner, []).append((rl["elo"], rl["rd"], 1.0))
        results.setdefault(loser, []).append((rw["elo"], rw["rd"], 0.0))

    victims = list(eligible_victims.items())

    # Case 1: victim vs victim — both have definitive elimination orders; higher order outlasted.
    for i in range(len(victims)):
        p_a, order_a = victims[i]
        for j in range(i + 1, len(victims)):
            p_b, order_b = victims[j]
            if p_a == p_b:
                continue
            if order_a < order_b:
                _record(p_b, p_a)       # B outlasted A
            elif order_b < order_a:
                _record(p_a, p_b)       # A outlasted B
            # Exact tie in kill_order is impossible (unique kill events)

    # Case 2: survivor vs victim — a survivor (never observed dying) outlasted every confirmed-dead
    # player (decision #5: their floor is the match end, so a_floor >= v_order always holds). The
    # guard is kept for safety. Survivors are never compared to each other (their order is unknown),
    # so a survivor only ever gains here and is never punished by the streamer dying early.
    for victim, v_order in eligible_victims.items():
        for attacker, a_floor in attacker_only.items():
            if attacker == victim or a_floor < v_order:
                continue
            _record(attacker, victim)

    # --- Apply one Glicko-2 update per player and write placements ---
    affected_players = set(results) | set(eligible_victims) | set(attacker_only)

    for player in affected_players:
        r = _get_or_default(player, ratings_cache, db_path)
        elo_before = r["elo"]
        new = glicko.update(r["elo"], r["rd"], r["vol"], results.get(player, []))
        elo_after = max(ELO_FLOOR, new["rating"])
        r["elo"] = elo_after
        r["rd"] = new["rd"]
        r["vol"] = new["vol"]
        r["peak_elo"] = max(r.get("peak_elo", elo_after), elo_after)
        r["matches_played"] = r["matches_played"] + 1

        # Placement row for everyone with a known survival position. Eliminated players store their
        # elimination kill_order; survivors store their survival FLOOR (match end) with survived=1
        # so the API can distinguish "died as kill N" from "still alive through the observed match".
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

    # OCR-robust catch: an anonymized 'Legend####' player whose legend prefix is GARBLED
    # (miraqe6442, oct ane1728, ballstic4142, catalvst4032) or whose digits were mis-read as
    # letters. PlayerDatabase._anon_key fuzzy-matches the prefix to a canonical legend (>=0.6)
    # and digit-recovers the 3-4 char suffix; a non-None result means this is an anonymized
    # label. Such labels are NON-IDENTIFYING -- Apex's hide-names setting can hand the SAME
    # Legend#### to different real players, so they must never be rated / appear on the
    # leaderboard. The exact-legend checks above still handle bare/bracketed legends with no
    # digits (e.g. [valk], octane), which _anon_key does not catch. (bead: anon-not-identifying)
    from database import PlayerDatabase
    if PlayerDatabase._anon_key(name) is not None:
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
    """Return True if this player is a valid, rateable victim in this match.

    This used to also require victim_conf >= CONF_FLOOR, but that 'confidence' is the database
    NAME-MATCH score (how well the name matches an already-seen player), NOT OCR read quality --
    it is bimodal 0.0 (first-time / unmatched name) vs >=0.5 (seen before). Gating on it silently
    dropped every clean FIRST-TIME player from ELO (audit 2026-07-16: ~40% of eliminations, incl.
    perfect reads like 'neverglow', 'hello vincent') and is a prime cause of sparse placement
    coverage. Structural garbage is still excluded by _is_valid_player (via _is_legend_name) --
    noise, ping/twitch artifacts, high-digit-ratio, tag-only names -- so we drop only the
    familiarity penalty, not the garbage defense. (bead 7ls)
    """
    if _is_legend_name(player):
        return False
    return any(k.victim == player for k in match.kills)


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
            rd=r.get("rd", glicko.RD0),
            vol=r.get("vol", glicko.VOL0),
            path=db_path,
        )

    return ratings_cache
