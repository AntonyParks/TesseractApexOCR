"""Group killfeed Kill events into match sessions.

Supports both CSV (legacy) and SQLite (primary) sources.
A gap of >GAP_SECONDS between consecutive Kill events from the same streamer
signals a new match. Within a match, kill_order is assigned chronologically.
"""

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Optional

GAP_SECONDS = 90        # 1.5 minutes between matches
MIN_KILLS = 3           # discard matches shorter than this
CONF_FLOOR = 0.5        # minimum victim_conf to participate in ELO
LEGACY_CONF = 0.5       # assigned to rows without a confidence column

# Cross-streamer merge: two streamers in the same lobby each produce a match record of the
# same real game (their feeds overlap wherever the same kills were visible to both). Merge
# criteria below; confirmed real example: Zuni & Matafe_ 2026-07-02 09:38-09:44, same kills
# in both feeds with 0-30s skew from differing Twitch delays.
MERGE_MAX_SKEW_SECONDS = 60    # max timestamp difference for two events to be "the same kill"
MERGE_MIN_SHARED = 3           # shared kills required before two matches merge
MERGE_NAME_SIM = 0.75          # SequenceMatcher threshold on "attacker|victim" (OCR garbles)
MERGE_WINDOW_SLOP = 120        # extra seconds of window overlap tolerance


@dataclass
class KillEvent:
    timestamp: datetime
    attacker: Optional[str]
    victim: Optional[str]
    attacker_conf: float
    victim_conf: float
    kill_order: int = 0
    # Provenance back-link to the source killfeed.db events row and its crop. Populated only on the
    # DB rebuild path (detect_matches_from_db); the legacy CSV path leaves the defaults.
    source_event_id: Optional[int] = None
    crop_filename: str = ""


@dataclass
class Match:
    match_id: str
    streamer: str
    start_time: datetime
    end_time: datetime
    kills: list[KillEvent] = field(default_factory=list)
    # match_ids of other streamers' match records merged into this one (same real lobby)
    merged_from: list[str] = field(default_factory=list)
    # Corroborating events (Knock, KillUnverified) within this match's time window. Used
    # ONLY as extra evidence when fingerprinting whether two streamers watched the same
    # lobby -- never fed into ELO and never persisted. Knocks are ideal for this: both
    # streams render the same knock lines, and there are far more of them than finishes.
    fingerprints: list[KillEvent] = field(default_factory=list)

    @property
    def kill_count(self) -> int:
        return len(self.kills)

    @property
    def players_observed(self) -> int:
        seen = set()
        for k in self.kills:
            if k.attacker:
                seen.add(k.attacker)
            if k.victim:
                seen.add(k.victim)
        return len(seen)


def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _make_match_id(streamer: str, start_time: datetime) -> str:
    return f"{streamer}_{int(start_time.timestamp())}"


def _resolve_survival_order(kills: list[KillEvent]) -> dict[str, int]:
    """Compute each player's true elimination order within a match.

    Returns: {player_name: kill_order_of_elimination}

    Rules:
    - Only players who appear as victims can have a definitive elimination order.
    - If a player appears as attacker at order K2 AFTER appearing as victim at order K1
      (K2 > K1), they were revived — the K1 victim entry is invalidated.
    - Final elimination = last valid victim appearance.
    """
    # Track the latest attacker order seen per player
    last_attacker_order: dict[str, int] = {}
    for kill in kills:
        if kill.attacker:
            last_attacker_order[kill.attacker] = max(
                last_attacker_order.get(kill.attacker, 0), kill.kill_order
            )

    # For each player who appears as victim, find the true elimination
    # (their last victim appearance where no attacker appearance comes after it)
    victim_appearances: dict[str, list[int]] = {}
    for kill in kills:
        if kill.victim:
            victim_appearances.setdefault(kill.victim, []).append(kill.kill_order)

    elimination_order: dict[str, int] = {}
    for player, orders in victim_appearances.items():
        last_victim_order = max(orders)
        attacker_floor = last_attacker_order.get(player, 0)

        if attacker_floor > last_victim_order:
            # Player appeared as attacker after last victim entry → revived,
            # then survived beyond our data. No definitive elimination this match.
            continue

        elimination_order[player] = last_victim_order

    return elimination_order


def _compute_last_alive_order(kills: list[KillEvent]) -> dict[str, int]:
    """Survival floor for every player who appears in the match (as attacker OR victim).

    Rule (rma decision #5): a player we never observe dying is assumed to have outlasted the WHOLE
    observed field, so their floor is the match's LAST observed kill-order, not their own last
    action. The caller drops anyone with a definitive elimination, leaving true survivors — each
    then credited (positively) against ALL confirmed-dead, and never compared to other survivors,
    so a streamer dying early can only help or leave a survivor flat, never punish them. Extending
    "seen" to victim appearances (not just attacker kills) means revived/knocked players who never
    finally die also get their survivor floor."""
    if not kills:
        return {}
    match_end = max(k.kill_order for k in kills)
    seen: set[str] = set()
    for kill in kills:
        if kill.attacker:
            seen.add(kill.attacker)
        if kill.victim:
            seen.add(kill.victim)
    return {player: match_end for player in seen}
def _split_chunk_recursive(kills_chunk: list[tuple[datetime, dict]], max_players: int = 62, max_duration: float = 1500.0) -> list[list[tuple[datetime, dict]]]:
    """Recursively split a grouped kills chunk if it exceeds unique player or duration bounds."""
    if len(kills_chunk) < 2:
        return [kills_chunk]

    # Count unique players observed
    seen_players = set()
    for _, d in kills_chunk:
        if d.get("attacker"):
            seen_players.add(d["attacker"])
        if d.get("victim"):
            seen_players.add(d["victim"])

    duration_sec = (kills_chunk[-1][0] - kills_chunk[0][0]).total_seconds()

    # If within bounds, no further splitting needed
    if len(seen_players) < max_players and duration_sec < max_duration:
        return [kills_chunk]

    # Find the largest internal time gap
    max_gap = -1.0
    split_idx = -1
    for i in range(len(kills_chunk) - 1):
        gap = (kills_chunk[i + 1][0] - kills_chunk[i][0]).total_seconds()
        if gap > max_gap:
            max_gap = gap
            split_idx = i + 1

    if split_idx == -1:
        return [kills_chunk]

    # Split into left and right sub-chunks and recurse
    left = kills_chunk[:split_idx]
    right = kills_chunk[split_idx:]
    
    return (
        _split_chunk_recursive(left, max_players, max_duration) +
        _split_chunk_recursive(right, max_players, max_duration)
    )


def detect_matches(
    csv_path: Path,
    gap_seconds: int = GAP_SECONDS,
    min_kills: int = MIN_KILLS,
) -> list[Match]:
    """Read killfeed_log.csv and group Kill events into match sessions.

    Returns a list of Match objects, sorted chronologically by start_time.
    """
    if not csv_path.exists():
        return []

    # Read only Kill events, grouped by streamer
    by_streamer: dict[str, list[tuple[datetime, dict]]] = {}

    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_conf = "attacker_conf" in fieldnames

        for row in reader:
            etype = (row.get("event_type") or "").strip()
            attacker = row.get("attacker") or None
            victim = row.get("victim") or None
            # A Kill needs a VICTIM (a death drives survival-order placement); the ATTACKER is optional
            # -- a real kill whose attacker OCR dropped/garbled still tells us the victim died at this
            # kill_order, and it credits no one (elo_engine's stat loop guards on k.attacker). But an
            # empty-VICTIM "Kill" means nobody died: a persistent HUD/sticky line OCR'd as a kill
            # (measured: 10.7% of Kill rows, 16% of credited leaderboard kills, whole players fabricated
            # -- bead o1o) -> drop it. BleedOut still needs BOTH: a single-sided bleed-out name is
            # ambiguous which side (the lone name may be the victim sitting in the attacker field).
            if not ((etype == "Kill" and victim) or (etype == "BleedOut" and attacker and victim)):
                continue

            ts = _parse_ts(row.get("timestamp", ""))
            if not ts:
                continue

            if not attacker and not victim:
                continue

            if has_conf:
                try:
                    a_conf = float(row.get("attacker_conf") or LEGACY_CONF)
                except ValueError:
                    a_conf = LEGACY_CONF
                try:
                    v_conf = float(row.get("victim_conf") or LEGACY_CONF)
                except ValueError:
                    v_conf = LEGACY_CONF
            else:
                a_conf = LEGACY_CONF
                v_conf = LEGACY_CONF

            streamer = row.get("streamer", "unknown").strip()
            by_streamer.setdefault(streamer, []).append((ts, {
                "attacker": attacker,
                "victim": victim,
                "attacker_conf": a_conf,
                "victim_conf": v_conf,
            }))

    matches: list[Match] = []

    for streamer, events in by_streamer.items():
        events.sort(key=lambda x: x[0])

        current_kills: list[tuple[datetime, dict]] = []

        def _flush(kills_chunk: list[tuple[datetime, dict]]) -> None:
            if len(kills_chunk) < min_kills:
                return
            
            # Apply recursive stitch splitter
            split_chunks = _split_chunk_recursive(kills_chunk, max_players=62, max_duration=1500.0)
            
            for chunk in split_chunks:
                if len(chunk) < min_kills:
                    continue
                start = chunk[0][0]
                end = chunk[-1][0]
                match_id = _make_match_id(streamer, start)

                kill_events = []
                for order, (ts, data) in enumerate(chunk, start=1):
                    kill_events.append(KillEvent(
                        timestamp=ts,
                        attacker=data["attacker"],
                        victim=data["victim"],
                        attacker_conf=data["attacker_conf"],
                        victim_conf=data["victim_conf"],
                        kill_order=order,
                    ))

                matches.append(Match(
                    match_id=match_id,
                    streamer=streamer,
                    start_time=start,
                    end_time=end,
                    kills=kill_events,
                ))

        for ts, data in events:
            if current_kills:
                gap = (ts - current_kills[-1][0]).total_seconds()
                if gap > gap_seconds:
                    _flush(current_kills)
                    current_kills = []
            current_kills.append((ts, data))

        _flush(current_kills)

    matches.sort(key=lambda m: m.start_time)
    return matches


def get_player_survival(match: Match) -> tuple[dict[str, int], dict[str, int]]:
    """Return (elimination_order, last_alive_order) for a match.

    elimination_order: {player: kill_order} — players with definitive eliminations.
    last_alive_order:  {player: kill_order} — minimum survival floor for all players.
    """
    elimination_order = _resolve_survival_order(match.kills)
    last_alive_order = _compute_last_alive_order(match.kills)
    return elimination_order, last_alive_order


def detect_matches_from_db(
    db_path: Path,
    gap_seconds: int = GAP_SECONDS,
    min_kills: int = MIN_KILLS,
) -> list[Match]:
    """Read Kill events from killfeed.db and group into match sessions.

    Only reads source='trocr' rows so Gemini corrections aren't double-counted.
    Returns a list of Match objects sorted chronologically by start_time.
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # BleedOut lines are kill-equivalents: the game credits the bleed-out death to the
    # knocker, rendered as "Knocker [Bleed Out] Victim". Only both-name rows are included --
    # single-sided BleedOut rows are ambiguous (a truncated read, or an unattributed
    # ring/fall death where the lone name is actually the VICTIM sitting in the attacker
    # field), and crediting those would hand kills to dying players.
    rows = conn.execute(
        """
        SELECT id, streamer, timestamp, attacker, victim, attacker_conf, victim_conf, crop_filename
        FROM events
        WHERE ((event_type = 'Kill' AND victim IS NOT NULL AND TRIM(victim) != '')
               OR (event_type = 'BleedOut' AND attacker IS NOT NULL AND TRIM(attacker) != ''
                   AND victim IS NOT NULL AND TRIM(victim) != ''))
          AND source IN ('trocr', 'easyocr')
        ORDER BY timestamp ASC
        """
    ).fetchall()

    # Corroborating events for cross-streamer lobby fingerprinting only (never ELO):
    # knocks and unverified kills render identically on every stream watching the lobby,
    # and are far more numerous than confirmed finishes.
    fp_rows = conn.execute(
        """
        SELECT streamer, timestamp, attacker, victim
        FROM events
        WHERE event_type IN ('Knock', 'KillUnverified')
          AND attacker != '' AND victim != ''
          AND source IN ('trocr', 'easyocr')
        ORDER BY timestamp ASC
        """
    ).fetchall()
    conn.close()

    by_streamer: dict[str, list[tuple[datetime, dict]]] = {}
    for (event_id, streamer, ts_str, attacker, victim, a_conf, v_conf, crop_filename) in rows:
        ts = _parse_ts(ts_str)
        if not ts:
            continue
        if not attacker and not victim:
            continue
        try:
            a_conf = float(a_conf or LEGACY_CONF)
        except (TypeError, ValueError):
            a_conf = LEGACY_CONF
        try:
            v_conf = float(v_conf or LEGACY_CONF)
        except (TypeError, ValueError):
            v_conf = LEGACY_CONF
        by_streamer.setdefault(streamer, []).append((ts, {
            "attacker": attacker or None,
            "victim":   victim   or None,
            "attacker_conf": a_conf,
            "victim_conf":   v_conf,
            "source_event_id": event_id,
            "crop_filename":   crop_filename or "",
        }))

    # Glue events join the same per-streamer stream, flagged with '_fp'. They hold match
    # windows together (confirmed finishes are sparse -- 90s without a FINISH is common in
    # a real game, knocks fill the gaps) and serve as cross-streamer fingerprints, but are
    # never part of Match.kills / ELO.
    for (streamer, ts_str, attacker, victim) in fp_rows:
        ts = _parse_ts(ts_str)
        if not ts:
            continue
        by_streamer.setdefault(streamer, []).append((ts, {
            "attacker": attacker or None,
            "victim":   victim   or None,
            "attacker_conf": LEGACY_CONF,
            "victim_conf":   LEGACY_CONF,
            "_fp": True,
        }))

    return _build_matches(by_streamer, gap_seconds, min_kills)


def _build_matches(
    by_streamer: dict,
    gap_seconds: int,
    min_kills: int,
) -> list[Match]:
    """Shared match-building logic from a by_streamer dict.

    Entries flagged with '_fp' (knocks, unverified kills) participate in gap grouping and
    the stitch splitter -- they define the game's temporal continuity -- but end up in
    Match.fingerprints rather than Match.kills, so they never touch ELO.

    The min_kills floor is applied AFTER cross-streamer merging: a thin single-stream view
    of a lobby (1-2 confirmed finishes) survives if another streamer's view of the same
    lobby corroborates and merges with it; standalone fragments below the floor are still
    discarded.
    """
    matches: list[Match] = []

    for streamer, events in by_streamer.items():
        events.sort(key=lambda x: x[0])
        current_events: list[tuple[datetime, dict]] = []

        def _flush(event_chunk: list[tuple[datetime, dict]]) -> None:
            # Apply recursive stitch splitter
            split_chunks = _split_chunk_recursive(event_chunk, max_players=62, max_duration=1500.0)

            for chunk in split_chunks:
                kill_entries = [(ts, d) for ts, d in chunk if not d.get("_fp")]
                if not kill_entries:
                    continue
                start = kill_entries[0][0]
                end   = kill_entries[-1][0]
                match_id = _make_match_id(streamer, start)
                kill_events = [
                    KillEvent(
                        timestamp=ts, attacker=d["attacker"], victim=d["victim"],
                        attacker_conf=d["attacker_conf"], victim_conf=d["victim_conf"],
                        kill_order=order,
                        source_event_id=d.get("source_event_id"),
                        crop_filename=d.get("crop_filename", ""),
                    )
                    for order, (ts, d) in enumerate(kill_entries, start=1)
                ]
                fingerprints = [
                    KillEvent(
                        timestamp=ts, attacker=d["attacker"], victim=d["victim"],
                        attacker_conf=d["attacker_conf"], victim_conf=d["victim_conf"],
                    )
                    for ts, d in chunk if d.get("_fp")
                ]
                matches.append(Match(
                    match_id=match_id, streamer=streamer,
                    start_time=start, end_time=end, kills=kill_events,
                    fingerprints=fingerprints,
                ))

        for ts, data in events:
            if current_events and (ts - current_events[-1][0]).total_seconds() > gap_seconds:
                _flush(current_events)
                current_events = []
            current_events.append((ts, data))
        _flush(current_events)

    matches = merge_cross_streamer_matches(matches)
    matches = [m for m in matches if m.kill_count >= min_kills]
    matches.sort(key=lambda m: m.start_time)
    return matches


# ---------------------------------------------------------------------------
# Cross-streamer merging (same real lobby observed by multiple streamers)
# ---------------------------------------------------------------------------

def _shared_event_pairs(events1: list[KillEvent], events2: list[KillEvent]) -> list[tuple[KillEvent, KillEvent]]:
    """Greedily pair up events that appear in BOTH lists: same (attacker, victim) up to
    OCR garbling (fuzzy 'attacker|victim' match) within MERGE_MAX_SKEW_SECONDS."""
    pairs = []
    used = set()
    for k1 in events1:
        if not (k1.attacker and k1.victim):
            continue
        sig1 = f"{k1.attacker}|{k1.victim}".lower()
        best_j, best_ratio = None, 0.0
        for j, k2 in enumerate(events2):
            if j in used or not (k2.attacker and k2.victim):
                continue
            if abs((k1.timestamp - k2.timestamp).total_seconds()) > MERGE_MAX_SKEW_SECONDS:
                continue
            ratio = SequenceMatcher(None, sig1, f"{k2.attacker}|{k2.victim}".lower()).ratio()
            if ratio >= MERGE_NAME_SIM and ratio > best_ratio:
                best_j, best_ratio = j, ratio
        if best_j is not None:
            pairs.append((k1, events2[best_j]))
            used.add(best_j)
    return pairs


def _merge_pair(primary: Match, secondary: Match,
                all_pairs: list[tuple[KillEvent, KillEvent]],
                kill_pairs: list[tuple[KillEvent, KillEvent]]) -> Match:
    """Merge secondary's view of the lobby into primary's.

    The two streams disagree on wall-clock time (Twitch delay differs per stream), so the
    median offset over ALL shared events (kills + fingerprints) is used to shift the
    secondary's unshared events onto the primary's clock before interleaving. Shared kills
    keep the primary's copy but take the max confidence per side (observed by two
    independent streams = corroborated).
    """
    skew = median((k2.timestamp - k1.timestamp).total_seconds() for k1, k2 in all_pairs)
    matched_secondary = {id(k2) for _, k2 in kill_pairs}

    merged_kills = []
    conf_boost = {id(k1): k2 for k1, k2 in kill_pairs}
    for k in primary.kills:
        other = conf_boost.get(id(k))
        if other is not None:
            k = KillEvent(
                timestamp=k.timestamp, attacker=k.attacker, victim=k.victim,
                attacker_conf=max(k.attacker_conf, other.attacker_conf),
                victim_conf=max(k.victim_conf, other.victim_conf),
                # keep the primary stream's provenance back-link (its copy is retained)
                source_event_id=k.source_event_id, crop_filename=k.crop_filename,
            )
        merged_kills.append(k)
    for k in secondary.kills:
        if id(k) in matched_secondary:
            continue
        merged_kills.append(KillEvent(
            timestamp=k.timestamp - timedelta(seconds=skew),
            attacker=k.attacker, victim=k.victim,
            attacker_conf=k.attacker_conf, victim_conf=k.victim_conf,
            source_event_id=k.source_event_id, crop_filename=k.crop_filename,
        ))

    merged_kills.sort(key=lambda k: k.timestamp)
    for order, k in enumerate(merged_kills, start=1):
        k.kill_order = order

    merged_fp = primary.fingerprints + [
        KillEvent(timestamp=k.timestamp - timedelta(seconds=skew),
                  attacker=k.attacker, victim=k.victim,
                  attacker_conf=k.attacker_conf, victim_conf=k.victim_conf)
        for k in secondary.fingerprints
    ]

    return Match(
        match_id=primary.match_id,
        streamer=primary.streamer,
        start_time=merged_kills[0].timestamp,
        end_time=merged_kills[-1].timestamp,
        kills=merged_kills,
        merged_from=primary.merged_from + secondary.merged_from + [secondary.match_id],
        fingerprints=merged_fp,
    )


def merge_cross_streamer_matches(matches: list[Match]) -> list[Match]:
    """Collapse match records that are the same real lobby seen from different streams.

    Without this, a lobby watched by two tracked streamers produces two match records and
    every shared kill is rated twice. Merging also widens lobby coverage: each stream only
    shows kills near its player, so the union observes more of the (up to 60) participants.
    Transitive merges (A~B, B~C) are handled by rescanning until stable.
    """
    matches = list(matches)
    slop = timedelta(seconds=MERGE_WINDOW_SLOP)
    changed = True
    while changed:
        changed = False
        for i in range(len(matches)):
            for j in range(i + 1, len(matches)):
                a, b = matches[i], matches[j]
                if a.streamer == b.streamer:
                    continue
                if a.start_time - slop > b.end_time or b.start_time - slop > a.end_time:
                    continue
                primary, secondary = (a, b) if (a.kill_count, b.start_time) >= (b.kill_count, a.start_time) else (b, a)
                # Fingerprint on kills AND corroborating events (knocks etc.) -- the same
                # lobby is identifiable long before both streams have 3 shared FINISHES.
                all_pairs = _shared_event_pairs(
                    primary.kills + primary.fingerprints,
                    secondary.kills + secondary.fingerprints,
                )
                if len(all_pairs) < MERGE_MIN_SHARED:
                    continue
                kill_pairs = _shared_event_pairs(primary.kills, secondary.kills)
                merged = _merge_pair(primary, secondary, all_pairs, kill_pairs)
                print(f"  [merge] {secondary.match_id} ({secondary.kill_count} kills) merged into "
                      f"{merged.match_id} -> {merged.kill_count} kills, "
                      f"{merged.players_observed} players "
                      f"({len(all_pairs)} shared events, {len(kill_pairs)} shared kills)")
                matches[i] = merged
                del matches[j]
                changed = True
                break
            if changed:
                break
    return matches


if __name__ == "__main__":
    import argparse
    from config import KILLFEED_DB_PATH, LOG_PATH

    parser = argparse.ArgumentParser(description="Detect match sessions from killfeed DB / CSV")
    parser.add_argument("--gap",       type=int, default=GAP_SECONDS, help="Gap in seconds between matches")
    parser.add_argument("--min-kills", type=int, default=MIN_KILLS,   help="Minimum kills per match")
    parser.add_argument("--csv",       action="store_true",            help="Force CSV source (legacy)")
    args = parser.parse_args()

    if not args.csv and KILLFEED_DB_PATH.exists():
        print(f"Reading from SQLite: {KILLFEED_DB_PATH}")
        matches = detect_matches_from_db(KILLFEED_DB_PATH, gap_seconds=args.gap, min_kills=args.min_kills)
        src = KILLFEED_DB_PATH
    else:
        print(f"Reading from CSV: {LOG_PATH}")
        matches = detect_matches(LOG_PATH, gap_seconds=args.gap, min_kills=args.min_kills)
        src = LOG_PATH

    print(f"\nDetected {len(matches)} matches from {src}\n")
    total_players = 0
    for m in matches:
        elim, _ = get_player_survival(m)
        print(
            f"  [{m.match_id}] {m.streamer:15s} | "
            f"{m.start_time.strftime('%Y-%m-%d %H:%M')} -> {m.end_time.strftime('%H:%M')} | "
            f"{m.kill_count:3d} kills | {m.players_observed:3d} players | "
            f"{len(elim):3d} with definitive placement"
        )
        total_players += m.players_observed
    print(f"\nTotal player appearances: {total_players}")

