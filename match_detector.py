"""Group killfeed_log.csv Kill events into match sessions.

A gap of >GAP_SECONDS between consecutive Kill events from the same streamer
signals a new match. Within a match, kill_order is assigned chronologically.

Knock vs kill handling:
    The Apex killfeed shows both knocks and eliminations with identical OCR text.
    A player can appear as victim twice (knocked at order 3, eliminated at order 9).
    We resolve this by:
    - Using the LAST victim appearance as the true elimination order.
    - If a player appears as attacker AFTER an earlier victim entry, they were revived;
      that earlier victim entry is discarded.
"""

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

GAP_SECONDS = 300       # 5 minutes between matches
MIN_KILLS = 3           # discard matches shorter than this
CONF_FLOOR = 0.5        # minimum victim_conf to participate in ELO
LEGACY_CONF = 0.5       # assigned to rows without a confidence column


@dataclass
class KillEvent:
    timestamp: datetime
    attacker: Optional[str]
    victim: Optional[str]
    attacker_conf: float
    victim_conf: float
    kill_order: int = 0


@dataclass
class Match:
    match_id: str
    streamer: str
    start_time: datetime
    end_time: datetime
    kills: list[KillEvent] = field(default_factory=list)

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
    """For attacker-only players, the last kill they made is their minimum survival floor."""
    last_alive: dict[str, int] = {}
    for kill in kills:
        if kill.attacker:
            last_alive[kill.attacker] = max(
                last_alive.get(kill.attacker, 0), kill.kill_order
            )
    return last_alive


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
            if (row.get("event_type") or "").strip() != "Kill":
                continue

            ts = _parse_ts(row.get("timestamp", ""))
            if not ts:
                continue

            attacker = row.get("attacker") or None
            victim = row.get("victim") or None
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
            start = kills_chunk[0][0]
            end = kills_chunk[-1][0]
            match_id = _make_match_id(streamer, start)

            kill_events = []
            for order, (ts, data) in enumerate(kills_chunk, start=1):
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


if __name__ == "__main__":
    import argparse
    from config import LOG_PATH

    parser = argparse.ArgumentParser(description="Detect match sessions from killfeed_log.csv")
    parser.add_argument("--gap", type=int, default=GAP_SECONDS, help="Gap in seconds between matches")
    parser.add_argument("--min-kills", type=int, default=MIN_KILLS, help="Minimum kills per match")
    args = parser.parse_args()

    matches = detect_matches(LOG_PATH, gap_seconds=args.gap, min_kills=args.min_kills)
    print(f"\nDetected {len(matches)} matches from {LOG_PATH}\n")

    total_players = 0
    for m in matches:
        elim, _ = get_player_survival(m)
        print(
            f"  [{m.match_id}] {m.streamer:15s} | "
            f"{m.start_time.strftime('%Y-%m-%d %H:%M')} → {m.end_time.strftime('%H:%M')} | "
            f"{m.kill_count:3d} kills | {m.players_observed:3d} players | "
            f"{len(elim):3d} with definitive placement"
        )
        total_players += m.players_observed

    print(f"\nTotal player appearances: {total_players}")
