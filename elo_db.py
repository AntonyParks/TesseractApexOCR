"""SQLite database manager for the ELO ranking system.

Schema:
    matches          — one row per detected match session
    match_kills      — kill events linked to a match with kill_order
    match_placements — per-player ELO delta for each match
    player_ratings   — current ELO + career stats per player
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ELO_DB_PATH = Path("elo.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id         TEXT PRIMARY KEY,
    streamer         TEXT NOT NULL,
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,
    kill_count       INTEGER NOT NULL,
    players_observed INTEGER NOT NULL,
    merged_from      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS match_kills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    attacker      TEXT,
    victim        TEXT,
    kill_order    INTEGER NOT NULL,
    attacker_conf REAL NOT NULL DEFAULT 0.0,
    victim_conf   REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS match_placements (
    match_id       TEXT NOT NULL,
    player         TEXT NOT NULL,
    kill_order_out INTEGER NOT NULL,
    survived       INTEGER NOT NULL DEFAULT 0,
    elo_before     REAL NOT NULL,
    elo_after      REAL NOT NULL,
    elo_change     REAL NOT NULL,
    PRIMARY KEY (match_id, player),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS player_ratings (
    player         TEXT PRIMARY KEY,
    elo            REAL NOT NULL DEFAULT 1000.0,   -- Glicko-2 rating (mu)
    rd             REAL NOT NULL DEFAULT 350.0,    -- Glicko-2 deviation (uncertainty); leaderboard ranks by elo - 2*rd
    vol            REAL NOT NULL DEFAULT 0.06,     -- Glicko-2 volatility (sigma)
    matches_played INTEGER NOT NULL DEFAULT 0,
    total_kills    INTEGER NOT NULL DEFAULT 0,
    total_deaths   INTEGER NOT NULL DEFAULT 0,
    peak_elo       REAL NOT NULL DEFAULT 1000.0,
    last_updated   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_match_kills_match ON match_kills(match_id);
CREATE INDEX IF NOT EXISTS idx_match_kills_victim ON match_kills(victim);
CREATE INDEX IF NOT EXISTS idx_match_kills_attacker ON match_kills(attacker);
CREATE INDEX IF NOT EXISTS idx_placements_player ON match_placements(player);
CREATE INDEX IF NOT EXISTS idx_ratings_elo ON player_ratings(elo DESC);
"""


def get_conn(path: Path = ELO_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path = ELO_DB_PATH) -> None:
    """Create tables and indexes if they don't exist."""
    with get_conn(path) as conn:
        conn.executescript(_SCHEMA)


def drop_db(path: Path = ELO_DB_PATH) -> None:
    """Wipe all data (used by reprocess --reset)."""
    with get_conn(path) as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS match_placements;
            DROP TABLE IF EXISTS match_kills;
            DROP TABLE IF EXISTS matches;
            DROP TABLE IF EXISTS player_ratings;
        """)
    init_db(path)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def upsert_match(match_id: str, streamer: str, start_time: str, end_time: str,
                 kill_count: int, players_observed: int, merged_from: str = "",
                 path: Path = ELO_DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO matches
                (match_id, streamer, start_time, end_time, kill_count, players_observed, merged_from)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (match_id, streamer, start_time, end_time, kill_count, players_observed, merged_from))


def upsert_match_kills(kills: list[dict], path: Path = ELO_DB_PATH) -> None:
    """Bulk insert kill events. Each dict must have keys:
    match_id, timestamp, attacker, victim, kill_order, attacker_conf, victim_conf.
    """
    with get_conn(path) as conn:
        # Delete existing kills for these match_ids to allow idempotent re-runs
        match_ids = list({k["match_id"] for k in kills})
        conn.executemany(
            "DELETE FROM match_kills WHERE match_id = ?",
            [(mid,) for mid in match_ids]
        )
        conn.executemany("""
            INSERT INTO match_kills
                (match_id, timestamp, attacker, victim, kill_order, attacker_conf, victim_conf)
            VALUES (:match_id, :timestamp, :attacker, :victim, :kill_order,
                    :attacker_conf, :victim_conf)
        """, kills)


def upsert_placement(match_id: str, player: str, kill_order_out: int,
                     elo_before: float, elo_after: float, survived: int = 0,
                     path: Path = ELO_DB_PATH) -> None:
    """survived=1 means the player was never (definitively) eliminated on stream this
    match; kill_order_out is then their survival FLOOR (last kill they were seen making),
    not an elimination position."""
    elo_change = elo_after - elo_before
    with get_conn(path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO match_placements
                (match_id, player, kill_order_out, survived, elo_before, elo_after, elo_change)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (match_id, player, kill_order_out, survived, elo_before, elo_after, elo_change))


def update_player_rating(player: str, elo: float, matches_played: int,
                         total_kills: int, total_deaths: int,
                         rd: float = 350.0, vol: float = 0.06,
                         path: Path = ELO_DB_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(path) as conn:
        existing = conn.execute(
            "SELECT peak_elo FROM player_ratings WHERE player = ?", (player,)
        ).fetchone()
        peak_elo = max(elo, existing["peak_elo"] if existing else elo)
        conn.execute("""
            INSERT INTO player_ratings
                (player, elo, rd, vol, matches_played, total_kills, total_deaths, peak_elo, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player) DO UPDATE SET
                elo            = excluded.elo,
                rd             = excluded.rd,
                vol            = excluded.vol,
                matches_played = excluded.matches_played,
                total_kills    = excluded.total_kills,
                total_deaths   = excluded.total_deaths,
                peak_elo       = excluded.peak_elo,
                last_updated   = excluded.last_updated
        """, (player, elo, rd, vol, matches_played, total_kills, total_deaths, peak_elo, now))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_player_rating(player: str, path: Path = ELO_DB_PATH) -> Optional[dict]:
    with get_conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM player_ratings WHERE player = ?", (player,)
        ).fetchone()
        return dict(row) if row else None


def get_rankings(limit: int = 100, offset: int = 0, min_matches: int = 1,
                 path: Path = ELO_DB_PATH) -> list[dict]:
    with get_conn(path) as conn:
        # Rank by the CONSERVATIVE estimate (Glicko-2 mu - 2*rd), not raw mu: unproven players
        # (large rd) sit lower until they've earned certainty, so a lucky small sample can't top
        # the board. elo (mu) stays the displayed headline number.
        rows = conn.execute("""
            SELECT * FROM player_ratings
            WHERE matches_played >= ?
            ORDER BY (elo - 2.0 * rd) DESC
            LIMIT ? OFFSET ?
        """, (min_matches, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_player_match_history(player: str, limit: int = 50,
                             path: Path = ELO_DB_PATH) -> list[dict]:
    with get_conn(path) as conn:
        rows = conn.execute("""
            SELECT mp.match_id, mp.kill_order_out, mp.elo_before, mp.elo_after, mp.elo_change,
                   m.streamer, m.start_time, m.kill_count
            FROM match_placements mp
            JOIN matches m ON mp.match_id = m.match_id
            WHERE mp.player = ?
            ORDER BY m.start_time DESC
            LIMIT ?
        """, (player, limit)).fetchall()
        return [dict(r) for r in rows]


def get_matches(streamer: Optional[str] = None, limit: int = 50, offset: int = 0,
                path: Path = ELO_DB_PATH) -> list[dict]:
    with get_conn(path) as conn:
        if streamer:
            rows = conn.execute("""
                SELECT * FROM matches WHERE streamer = ?
                ORDER BY start_time DESC LIMIT ? OFFSET ?
            """, (streamer, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM matches
                ORDER BY start_time DESC LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_match(match_id: str, path: Path = ELO_DB_PATH) -> Optional[dict]:
    with get_conn(path) as conn:
        match = conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not match:
            return None

        kills = conn.execute("""
            SELECT kill_order, attacker, victim, timestamp, attacker_conf, victim_conf
            FROM match_kills WHERE match_id = ? ORDER BY kill_order
        """, (match_id,)).fetchall()

        placements = conn.execute("""
            SELECT player, kill_order_out, survived, elo_before, elo_after, elo_change
            FROM match_placements WHERE match_id = ?
            ORDER BY survived DESC, kill_order_out DESC
        """, (match_id,)).fetchall()

        return {
            "match": dict(match),
            "kills": [dict(k) for k in kills],
            "placements": [dict(p) for p in placements],
        }


def get_total_rankings_count(min_matches: int = 1, path: Path = ELO_DB_PATH) -> int:
    with get_conn(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM player_ratings WHERE matches_played >= ?", (min_matches,)
        ).fetchone()
        return row[0]


def get_all_player_ratings(path: Path = ELO_DB_PATH) -> list[dict]:
    """Return all rows from player_ratings."""
    with get_conn(path) as conn:
        rows = conn.execute("SELECT * FROM player_ratings ORDER BY matches_played DESC").fetchall()
        return [dict(r) for r in rows]


def merge_player(canonical: str, duplicates: list[str],
                 merged_kills: int, merged_deaths: int, merged_matches: int,
                 merged_peak_elo: float,
                 path: Path = ELO_DB_PATH) -> None:
    """Merge duplicate player entries into the canonical name.

    - Updates match_kills and match_placements to point to canonical name.
    - Updates player_ratings for canonical with merged stats.
    - Deletes duplicate rows from player_ratings.
    """
    with get_conn(path) as conn:
        for dup in duplicates:
            conn.execute("UPDATE match_kills SET attacker = ? WHERE attacker = ?", (canonical, dup))
            conn.execute("UPDATE match_kills SET victim = ? WHERE victim = ?", (canonical, dup))
            # For match_placements, the (match_id, player) pair must be unique.
            # If the canonical already has a row for the same match, just delete the duplicate row.
            # Otherwise rename it.
            dup_matches = {row[0] for row in conn.execute(
                "SELECT match_id FROM match_placements WHERE player = ?", (dup,)
            ).fetchall()}
            canon_matches = {row[0] for row in conn.execute(
                "SELECT match_id FROM match_placements WHERE player = ?", (canonical,)
            ).fetchall()}
            conflict = dup_matches & canon_matches
            if conflict:
                conn.executemany(
                    "DELETE FROM match_placements WHERE player = ? AND match_id = ?",
                    [(dup, mid) for mid in conflict]
                )
            conn.execute("UPDATE match_placements SET player = ? WHERE player = ?", (canonical, dup))
            conn.execute("DELETE FROM player_ratings WHERE player = ?", (dup,))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE player_ratings
            SET total_kills = ?, total_deaths = ?, matches_played = ?, peak_elo = ?, last_updated = ?
            WHERE player = ?
        """, (merged_kills, merged_deaths, merged_matches, merged_peak_elo, now, canonical))
