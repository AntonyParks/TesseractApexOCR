"""
diagnose.py — TesseractApexOCR pipeline diagnostic tool

Usage:
    python diagnose.py [--json] [--stage STAGE] [--api-check] [--db PATH] [--elo-db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# ---------------------------------------------------------------------------
# Config import (best-effort; fallback defaults keep the script usable
# even when project venv is unavailable)
# ---------------------------------------------------------------------------
try:
    from config import (
        APEX_LEGENDS_CANONICAL,
        COMMON_WORDS,
        CROP_DEDUP_WINDOW,
        GEMINI_AGREE_THRESHOLD,
        GEMINI_CONFIRMED_DIR,
        GEMINI_CORRECTION_DIR,
        GEMINI_VALIDATE,
        KILLFEED_DB_PATH,
        TROCR_CONF_THRESHOLD,
        TROCR_MODEL_PATH,
        TWITCH_CHANNELS,
        USE_EASYOCR,
        USE_TROCR,
        EASYOCR_LANGUAGES,
        EASYOCR_GPU,
    )
    _CONFIG_OK = True
    _CONFIG_ERR = ""
except Exception as _e:
    _CONFIG_OK = False
    _CONFIG_ERR = str(_e)
    APEX_LEGENDS_CANONICAL: set = set()
    COMMON_WORDS: list = []
    TWITCH_CHANNELS: dict = {}
    USE_EASYOCR = True
    USE_TROCR = False
    EASYOCR_LANGUAGES = ['en']
    EASYOCR_GPU = True
    TROCR_MODEL_PATH = Path("models/trocr_apex")
    TROCR_CONF_THRESHOLD = 0.30
    CROP_DEDUP_WINDOW = 30.0
    GEMINI_AGREE_THRESHOLD = 0.85
    GEMINI_VALIDATE = True
    GEMINI_CORRECTION_DIR = Path("labels/gemini_corrections")
    GEMINI_CONFIRMED_DIR = Path("labels/gemini_confirmed")
    KILLFEED_DB_PATH = Path("killfeed.db")

try:
    from elo_db import ELO_DB_PATH
except Exception:
    ELO_DB_PATH = Path("elo.db")

# ---------------------------------------------------------------------------
# Rich (optional pretty output)
# ---------------------------------------------------------------------------
try:
    from rich import box as _rbox
    from rich.console import Console as _Console
    from rich.panel import Panel as _Panel
    from rich.table import Table as _Table
    _RICH = True
    _console = _Console()
except ImportError:
    _RICH = False
    _console = None

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
OK = "ok"
WARN = "warn"
FAIL = "fail"

STATUS_COLOR = {OK: "green", WARN: "yellow", FAIL: "red"}
STATUS_ICON  = {OK: "[OK]",  WARN: "[WARN]",  FAIL: "[FAIL]"}


# ---------------------------------------------------------------------------
# Core data structure
# ---------------------------------------------------------------------------
@dataclass
class StageResult:
    name: str
    status: str = OK
    metrics: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    def add_warn(self, msg: str) -> None:
        self.issues.append(msg)
        if self.status == OK:
            self.status = WARN

    def add_fail(self, msg: str) -> None:
        self.issues.append(msg)
        self.status = FAIL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _open_db(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _percentile(data: list, p: int) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * p / 100
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 2) if total else 0.0


def _mean(data: list) -> float:
    return round(statistics.mean(data), 3) if data else 0.0


def _stdev(data: list) -> float:
    return round(statistics.stdev(data), 3) if len(data) > 1 else 0.0


def _db_missing(path: Path, r: StageResult) -> bool:
    if not path.exists():
        r.add_warn(f"Database not found: {path}")
        r.skipped = True
        r.skip_reason = "database not found"
        return True
    return False


# ---------------------------------------------------------------------------
# Stage 1: Config
# ---------------------------------------------------------------------------
def check_config() -> StageResult:
    r = StageResult(name="config")

    if not _CONFIG_OK:
        r.add_fail(f"config.py import failed: {_CONFIG_ERR}")
        return r

    # TROCR model
    if USE_TROCR and not Path(TROCR_MODEL_PATH).exists():
        r.add_fail(f"USE_TROCR=True but TROCR_MODEL_PATH missing: {TROCR_MODEL_PATH}")

    # Threshold sanity
    if not (0.1 <= TROCR_CONF_THRESHOLD <= 0.7):
        r.add_warn(f"TROCR_CONF_THRESHOLD={TROCR_CONF_THRESHOLD} outside sane range 0.1–0.7")

    if CROP_DEDUP_WINDOW < 5:
        r.add_warn(f"CROP_DEDUP_WINDOW={CROP_DEDUP_WINDOW}s is below recommended minimum 5s")

    if not (0.5 <= GEMINI_AGREE_THRESHOLD <= 0.99):
        r.add_warn(f"GEMINI_AGREE_THRESHOLD={GEMINI_AGREE_THRESHOLD} outside sane range 0.5–0.99")

    empty_ch = [k for k, v in TWITCH_CHANNELS.items() if not str(v).strip()]
    if empty_ch:
        r.add_warn(f"TWITCH_CHANNELS has empty display name for: {empty_ch}")

    if not GEMINI_VALIDATE:
        r.add_warn("GEMINI_VALIDATE=False in config — Gemini correction disabled")

    r.metrics = {
        "USE_TROCR": USE_TROCR,
        "TROCR_MODEL_PATH": str(TROCR_MODEL_PATH),
        "trocr_model_exists": Path(TROCR_MODEL_PATH).exists(),
        "TROCR_CONF_THRESHOLD": TROCR_CONF_THRESHOLD,
        "CROP_DEDUP_WINDOW_s": CROP_DEDUP_WINDOW,
        "GEMINI_AGREE_THRESHOLD": GEMINI_AGREE_THRESHOLD,
        "GEMINI_VALIDATE": GEMINI_VALIDATE,
        "twitch_channel_count": len(TWITCH_CHANNELS),
        "killfeed_db_exists": KILLFEED_DB_PATH.exists(),
        "elo_db_exists": Path(ELO_DB_PATH).exists(),
    }
    return r


# ---------------------------------------------------------------------------
# Stage 2: Database
# ---------------------------------------------------------------------------
def check_database(db_path: Path) -> StageResult:
    r = StageResult(name="database")
    if _db_missing(db_path, r):
        return r

    conn = _open_db(db_path)

    # Total events
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # Per-type
    type_rows = conn.execute(
        "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type ORDER BY n DESC"
    ).fetchall()
    event_types = {row["event_type"]: row["n"] for row in type_rows}
    kill_total = event_types.get("Kill", 0)

    # Per-streamer
    streamer_count = conn.execute(
        "SELECT COUNT(DISTINCT streamer) FROM events"
    ).fetchone()[0]

    # Null names in Kill events
    null_row = conn.execute(
        """SELECT
             SUM(CASE WHEN attacker IS NULL OR attacker='' THEN 1 ELSE 0 END),
             SUM(CASE WHEN victim   IS NULL OR victim  ='' THEN 1 ELSE 0 END),
             COUNT(*)
           FROM events WHERE event_type='Kill'"""
    ).fetchone()
    null_attacker = null_row[0] or 0
    null_victim   = null_row[1] or 0
    kill_count_db = null_row[2] or 0

    # Confidence distribution
    conf_rows = conn.execute(
        "SELECT attacker_conf, victim_conf FROM events WHERE event_type='Kill'"
    ).fetchall()
    a_confs = [row[0] for row in conf_rows if row[0] is not None]
    v_confs = [row[1] for row in conf_rows if row[1] is not None]

    a_mean = _mean(a_confs)
    a_p25  = round(_percentile(a_confs, 25), 3)
    a_p50  = round(_percentile(a_confs, 50), 3)
    a_p75  = round(_percentile(a_confs, 75), 3)
    v_mean = _mean(v_confs)
    v_p50  = round(_percentile(v_confs, 50), 3)

    # Below CONF_FLOOR (0.5)
    below_floor_row = conn.execute(
        """SELECT SUM(CASE WHEN attacker_conf < 0.5 THEN 1 ELSE 0 END), COUNT(*)
           FROM events WHERE event_type='Kill'"""
    ).fetchone()
    below_floor = below_floor_row[0] or 0

    # Freshness
    events_24h = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp >= datetime('now', '-1 day')"
    ).fetchone()[0]

    # Gemini
    gem_row = conn.execute(
        """SELECT SUM(CASE WHEN source='gemini' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN gemini_corrected=1 THEN 1 ELSE 0 END),
                  COUNT(*) FROM events"""
    ).fetchone()
    gemini_source  = gem_row[0] or 0
    gemini_corr    = gem_row[1] or 0

    # Duplicate kills (last 30 days, to keep fast)
    dup_count = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT e1.id FROM events e1 JOIN events e2
               ON  e1.streamer=e2.streamer
               AND e1.attacker=e2.attacker
               AND e1.victim  =e2.victim
               AND e2.id > e1.id
               AND e1.event_type='Kill' AND e2.event_type='Kill'
               AND e1.timestamp >= datetime('now', '-30 days')
               AND (julianday(e2.timestamp)-julianday(e1.timestamp))*86400 <= 5
           )"""
    ).fetchone()[0]

    conn.close()

    # Issues
    if kill_count_db > 0:
        if _pct(null_attacker, kill_count_db) > 5:
            r.add_warn(f"{null_attacker} Kill events ({_pct(null_attacker, kill_count_db):.1f}%) have null attacker")
        if _pct(null_victim, kill_count_db) > 5:
            r.add_warn(f"{null_victim} Kill events ({_pct(null_victim, kill_count_db):.1f}%) have null victim")
        if a_p25 == 1.0 and a_p75 == 1.0 and a_mean == 1.0:
            r.add_warn("All attacker_conf values are 1.0 — likely legacy CSV import rows (no real confidence data)")
        elif a_mean < TROCR_CONF_THRESHOLD:
            r.add_warn(f"Mean attacker_conf {a_mean:.3f} is below TROCR_CONF_THRESHOLD {TROCR_CONF_THRESHOLD}")
        below_pct = _pct(below_floor, kill_count_db)
        if below_pct > 20:
            r.add_warn(f"{below_pct:.1f}% of Kill events have attacker_conf < 0.5 (CONF_FLOOR)")

    if events_24h == 0:
        r.add_warn("No events in last 24h — pipeline may be stalled")

    if dup_count > 50:
        r.add_warn(f"{dup_count} duplicate kill pairs within 5s (last 30 days)")
    elif dup_count > 0:
        r.add_warn(f"{dup_count} duplicate kill pairs within 5s (last 30 days)")

    r.metrics = {
        "total_events": total_events,
        "event_types": event_types,
        "streamer_count": streamer_count,
        "kill_events": kill_total,
        "kill_null_attacker": null_attacker,
        "kill_null_attacker_pct": _pct(null_attacker, kill_count_db),
        "kill_null_victim": null_victim,
        "kill_null_victim_pct": _pct(null_victim, kill_count_db),
        "attacker_conf_mean": a_mean,
        "attacker_conf_p25": a_p25,
        "attacker_conf_p50": a_p50,
        "attacker_conf_p75": a_p75,
        "victim_conf_mean": v_mean,
        "victim_conf_p50": v_p50,
        "pct_kill_below_conf_floor": _pct(below_floor, kill_count_db),
        "events_last_24h": events_24h,
        "gemini_source_count": gemini_source,
        "gemini_corrected_count": gemini_corr,
        "duplicate_kill_pairs_30d": dup_count,
    }
    return r


# ---------------------------------------------------------------------------
# Stage 3: Parsing quality
# ---------------------------------------------------------------------------
def check_parsing(db_path: Path) -> StageResult:
    r = StageResult(name="parsing")
    if _db_missing(db_path, r):
        return r

    conn = _open_db(db_path)

    # Self-kills
    self_kills = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='Kill' AND attacker=victim AND attacker != ''"
    ).fetchone()[0]

    # Name length anomalies
    len_row = conn.execute(
        """SELECT SUM(CASE WHEN LENGTH(attacker)<3 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN LENGTH(attacker)>30 THEN 1 ELSE 0 END),
                  COUNT(*)
           FROM events WHERE event_type='Kill' AND attacker != '' AND attacker IS NOT NULL"""
    ).fetchone()
    too_short = len_row[0] or 0
    too_long  = len_row[1] or 0
    name_total = len_row[2] or 0

    # All attacker/victim for legend + common word checks
    kill_rows = conn.execute(
        "SELECT attacker, victim FROM events WHERE event_type='Kill'"
    ).fetchall()
    conn.close()

    legend_lower  = {l.lower() for l in APEX_LEGENDS_CANONICAL}
    common_lower  = {w.lower() for w in COMMON_WORDS}

    attacker_legend = Counter()
    victim_legend   = Counter()
    attacker_common = Counter()
    victim_common   = Counter()

    for row in kill_rows:
        a = (row["attacker"] or "").strip()
        v = (row["victim"]   or "").strip()
        if a:
            al = a.lower()
            if al in legend_lower:
                attacker_legend[a] += 1
            if al in common_lower:
                attacker_common[a] += 1
        if v:
            vl = v.lower()
            if vl in legend_lower:
                victim_legend[v] += 1
            if vl in common_lower:
                victim_common[v] += 1

    legend_total = sum(attacker_legend.values()) + sum(victim_legend.values())
    common_total = sum(attacker_common.values()) + sum(victim_common.values())

    top_legend = (attacker_legend + victim_legend).most_common(5)
    top_common = (attacker_common + victim_common).most_common(5)

    # Issues
    if self_kills > 10:
        r.add_warn(f"{self_kills} Kill events where attacker==victim (OCR name-split failure)")
    elif self_kills > 0:
        r.add_warn(f"{self_kills} Kill events where attacker==victim")

    if legend_total > 5:
        sample = ", ".join(f"{n}×{c}" for n, c in top_legend)
        r.add_warn(f"{legend_total} legend names in player slots (top: {sample})")

    if common_total > 5:
        sample = ", ".join(f"{n}×{c}" for n, c in top_common)
        r.add_warn(f"{common_total} common-word strings in player slots (top: {sample})")

    if too_short > 0:
        r.add_warn(f"{too_short} attacker names with len < 3")

    if too_long > 0:
        r.add_warn(f"{too_long} attacker names with len > 30")

    r.metrics = {
        "self_kills": self_kills,
        "self_kills_pct": _pct(self_kills, len(kill_rows)),
        "attacker_too_short": too_short,
        "attacker_too_long": too_long,
        "legend_leaks_total": legend_total,
        "legend_leaks_top5": [[n, c] for n, c in top_legend],
        "common_word_leaks_total": common_total,
        "common_word_top5": [[n, c] for n, c in top_common],
    }
    return r


# ---------------------------------------------------------------------------
# Stage 4: Gemini validation
# ---------------------------------------------------------------------------
def check_gemini(db_path: Path) -> StageResult:
    r = StageResult(name="gemini")

    corr_dir = Path(GEMINI_CORRECTION_DIR)
    conf_dir = Path(GEMINI_CONFIRMED_DIR)

    corr_files = list(corr_dir.rglob("*.png")) if corr_dir.exists() else []
    conf_files = list(conf_dir.rglob("*.png")) if conf_dir.exists() else []
    corr_count = len(corr_files)
    conf_count  = len(conf_files)
    total_files = corr_count + conf_count
    corr_ratio  = round(corr_count / total_files, 3) if total_files else 0.0

    newest_corr_ts = None
    if corr_files:
        newest = max(corr_files, key=lambda f: f.stat().st_mtime)
        newest_corr_ts = datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    # DB stats
    gemini_db_rows = 0
    gemini_corr_rows = 0
    if db_path.exists():
        conn = _open_db(db_path)
        gem_row = conn.execute(
            """SELECT SUM(CASE WHEN source='gemini' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN gemini_corrected=1 THEN 1 ELSE 0 END)
               FROM events"""
        ).fetchone()
        conn.close()
        gemini_db_rows  = gem_row[0] or 0
        gemini_corr_rows = gem_row[1] or 0

    # Live singleton peek (best-effort, no queue creation)
    live_stats = None
    try:
        import gemini_queue as _gq
        inst = getattr(_gq, "_instance", None)
        if inst is not None:
            live_stats = {
                "validated": getattr(inst, "_validated", None),
                "agreed":    getattr(inst, "_agreed",    None),
                "corrections": getattr(inst, "_corrections", None),
                "dropped":   getattr(inst, "_dropped",   None),
            }
    except Exception:
        pass

    # Issues
    if not GEMINI_VALIDATE:
        r.add_warn("GEMINI_VALIDATE=False in config — queue disabled")

    if total_files == 0:
        r.add_warn("No Gemini label files found — Gemini validation may not have run")
    elif corr_ratio > 0.5:
        r.add_warn(
            f"High Gemini correction rate: {corr_ratio:.1%} "
            f"({corr_count} corrections vs {conf_count} confirmed)"
        )

    r.metrics = {
        "correction_file_count": corr_count,
        "confirmed_file_count": conf_count,
        "correction_ratio": corr_ratio,
        "most_recent_correction": newest_corr_ts,
        "gemini_db_rows": gemini_db_rows,
        "gemini_corrected_db_rows": gemini_corr_rows,
        "live_queue_stats": live_stats,
    }
    return r


# ---------------------------------------------------------------------------
# Stage 5: Match detection
# ---------------------------------------------------------------------------
def check_matches(elo_path: Path) -> StageResult:
    r = StageResult(name="matches")
    if _db_missing(elo_path, r):
        return r

    conn = _open_db(elo_path)

    # Check table exists
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "matches" not in tables:
        r.add_warn("matches table not found in elo.db — run reprocess.py first")
        r.skipped = True
        r.skip_reason = "matches table missing"
        conn.close()
        return r

    total_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    if total_matches == 0:
        r.add_warn("No matches found in elo.db — run reprocess.py first")
        conn.close()
        r.metrics = {"total_matches": 0}
        return r

    streamer_rows = conn.execute(
        "SELECT streamer, COUNT(*) n FROM matches GROUP BY streamer ORDER BY n DESC"
    ).fetchall()
    streamer_match_counts = {row["streamer"]: row["n"] for row in streamer_rows}

    kill_counts = [row[0] for row in conn.execute("SELECT kill_count FROM matches").fetchall()]
    mega_count  = conn.execute("SELECT COUNT(*) FROM matches WHERE kill_count >= 100").fetchone()[0]

    durations = [
        row[0] for row in conn.execute(
            "SELECT (julianday(end_time)-julianday(start_time))*1440 FROM matches"
        ).fetchall()
        if row[0] is not None
    ]

    players_obs = [
        row[0] for row in conn.execute("SELECT players_observed FROM matches").fetchall()
        if row[0] is not None
    ]

    conn.close()

    mega_pct = _pct(mega_count, total_matches)

    if mega_count > 0:
        r.add_warn(f"{mega_count} matches with 100+ kills ({mega_pct:.1f}%) — possible mega-match stitching bug")
    if mega_pct > 5:
        r.add_fail(f">{mega_pct:.1f}% of matches are mega-matches — match gap detection likely broken")

    long_matches = [d for d in durations if d > 120]
    if long_matches:
        r.add_warn(f"{len(long_matches)} match(es) with duration >120 min (max {max(long_matches):.0f} min)")

    r.metrics = {
        "total_matches": total_matches,
        "streamer_count": len(streamer_match_counts),
        "kill_count_min": min(kill_counts) if kill_counts else 0,
        "kill_count_max": max(kill_counts) if kill_counts else 0,
        "kill_count_mean": _mean(kill_counts),
        "kill_count_median": round(_percentile(kill_counts, 50), 1),
        "kill_count_p90": round(_percentile(kill_counts, 90), 1),
        "mega_match_count": mega_count,
        "mega_match_pct": mega_pct,
        "duration_min_mins": round(min(durations), 1) if durations else None,
        "duration_max_mins": round(max(durations), 1) if durations else None,
        "duration_mean_mins": _mean(durations),
        "players_per_match_mean": _mean(players_obs),
    }
    return r


# ---------------------------------------------------------------------------
# Stage 6: ELO health
# ---------------------------------------------------------------------------
def check_elo(elo_path: Path) -> StageResult:
    r = StageResult(name="elo")
    if _db_missing(elo_path, r):
        return r

    conn = _open_db(elo_path)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "player_ratings" not in tables:
        r.add_warn("player_ratings table not found — run reprocess.py first")
        r.skipped = True
        r.skip_reason = "player_ratings table missing"
        conn.close()
        return r

    total_players = conn.execute("SELECT COUNT(*) FROM player_ratings").fetchone()[0]
    qualified     = conn.execute(
        "SELECT COUNT(*) FROM player_ratings WHERE matches_played >= 3"
    ).fetchone()[0]

    elo_values = [row[0] for row in conn.execute("SELECT elo FROM player_ratings").fetchall()]

    outliers = conn.execute(
        """SELECT player, elo, matches_played FROM player_ratings
           WHERE elo > 1500 OR elo < 500
           ORDER BY ABS(elo - 1000) DESC LIMIT 10"""
    ).fetchall()
    outlier_list = [[row["player"], round(row["elo"], 1), row["matches_played"]] for row in outliers]
    high_count = sum(1 for v in elo_values if v > 1500)
    low_count  = sum(1 for v in elo_values if v < 500)

    # Near-duplicate detection (players with >= 2 matches)
    players_2plus = [
        row[0] for row in conn.execute(
            "SELECT player FROM player_ratings WHERE matches_played >= 2"
        ).fetchall()
    ]
    conn.close()

    by_prefix: dict[str, list] = defaultdict(list)
    for p in players_2plus:
        if len(p) >= 3:
            by_prefix[p[:3].lower()].append(p)

    dup_pairs = []
    for group in by_prefix.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if len(group) > 50:
                    break  # skip pathologically large prefix groups
                ratio = SequenceMatcher(None, group[i].lower(), group[j].lower()).ratio()
                if ratio >= 0.8:
                    dup_pairs.append((group[i], group[j], round(ratio, 3)))

    elo_min  = round(min(elo_values), 1) if elo_values else 0
    elo_max  = round(max(elo_values), 1) if elo_values else 0
    elo_mean = _mean(elo_values)
    elo_std  = _stdev(elo_values)
    elo_p10  = round(_percentile(elo_values, 10), 1)
    elo_p90  = round(_percentile(elo_values, 90), 1)

    # Issues
    if total_players == 0:
        r.add_warn("No players in player_ratings — run reprocess.py first")
    if qualified == 0 and total_players > 0:
        r.add_warn("No players with 3+ matches — ELO rankings not meaningful yet")
    if elo_min < 0:
        r.add_warn(f"Player(s) have negative ELO (min={elo_min}) — possible data error")
    if elo_max > 2000:
        r.add_warn(f"Extreme high ELO {elo_max} — mega-match or OCR data quality issue")
    if len(dup_pairs) > 20:
        sample = ", ".join(f"{a}/{b}" for a, b, _ in dup_pairs[:3])
        r.add_warn(f"{len(dup_pairs)} near-duplicate player name pairs detected (e.g. {sample}) — run reprocess.py --dedupe")

    r.metrics = {
        "total_players": total_players,
        "players_3plus_matches": qualified,
        "elo_min": elo_min,
        "elo_max": elo_max,
        "elo_mean": elo_mean,
        "elo_std": elo_std,
        "elo_p10": elo_p10,
        "elo_p90": elo_p90,
        "outlier_high_count": high_count,
        "outlier_low_count": low_count,
        "outlier_extreme_top5": outlier_list[:5],
        "near_dup_pair_count": len(dup_pairs),
        "near_dup_sample": [[a, b, s] for a, b, s in dup_pairs[:5]],
    }
    return r


# ---------------------------------------------------------------------------
# Stage 7: API health
# ---------------------------------------------------------------------------
def check_api() -> StageResult:
    r = StageResult(name="api")
    base = "http://localhost:8080"

    def _get(url: str) -> tuple:
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                elapsed = round((time.perf_counter() - t0) * 1000, 1)
                body = json.loads(resp.read().decode())
                return resp.status, body, elapsed
        except Exception as exc:
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            return -1, {"error": str(exc)}, elapsed

    status, body, latency = _get(f"{base}/health")

    if status == -1:
        r.add_fail(f"API server unreachable at {base}: {body.get('error', '')}")
        r.metrics = {"server_reachable": False}
        return r

    if status != 200:
        r.add_warn(f"Health endpoint returned HTTP {status}")
    if latency > 500:
        r.add_warn(f"Health endpoint latency {latency}ms exceeds 500ms")
    if isinstance(body, dict) and body.get("status") != "ok":
        r.add_warn(f"API health body status: {body.get('status', '?')!r}")

    s2, b2, _ = _get(f"{base}/stats/streamers")
    streamer_count = len(b2.get("streamers", [])) if s2 == 200 and isinstance(b2, dict) else None

    r.metrics = {
        "server_reachable": True,
        "health_status_code": status,
        "health_response_ms": latency,
        "health_body_status": body.get("status") if isinstance(body, dict) else None,
        "api_event_count": body.get("total_events") if isinstance(body, dict) else None,
        "streamer_count_from_api": streamer_count,
    }
    return r


# ---------------------------------------------------------------------------
# Output: JSON
# ---------------------------------------------------------------------------
def render_json(results: list[StageResult]) -> None:
    statuses = [r.status for r in results if not r.skipped]
    if FAIL in statuses:
        overall = FAIL
    elif WARN in statuses:
        overall = WARN
    else:
        overall = OK

    out = {
        "overall_health": overall,
        "stages": [
            {
                "name": r.name,
                "status": r.status,
                "skipped": r.skipped,
                "skip_reason": r.skip_reason,
                "metrics": r.metrics,
                "issues": r.issues,
            }
            for r in results
        ],
        "critical_issues": [
            f"[{r.name}] {issue}"
            for r in results for issue in r.issues
            if r.status == FAIL
        ],
        "warnings": [
            f"[{r.name}] {issue}"
            for r in results for issue in r.issues
            if r.status == WARN
        ],
    }
    print(json.dumps(out, indent=2, default=str))


# ---------------------------------------------------------------------------
# Output: Human (rich or plain fallback)
# ---------------------------------------------------------------------------
def _status_tag(status: str) -> str:
    if _RICH:
        color = STATUS_COLOR[status]
        return f"[bold {color}]{status.upper()}[/bold {color}]"
    return STATUS_ICON[status]


def _render_plain(results: list[StageResult]) -> None:
    statuses = [r.status for r in results if not r.skipped]
    overall = FAIL if FAIL in statuses else (WARN if WARN in statuses else OK)
    print(f"\nOverall pipeline health: {STATUS_ICON[overall]}\n{'='*60}")

    for r in results:
        tag = STATUS_ICON[r.status]
        print(f"\n{tag}  {r.name.upper()}")
        if r.skipped:
            print(f"  (skipped: {r.skip_reason})")
            continue
        for k, v in r.metrics.items():
            print(f"  {k:<38} {v}")
        for issue in r.issues:
            print(f"  {STATUS_ICON[r.status]}  {issue}")

    all_issues = [(r.name, r.status, i) for r in results for i in r.issues]
    if all_issues:
        print(f"\n{'='*60}\nIssue Summary")
        for name, status, issue in all_issues:
            print(f"  {STATUS_ICON[status]}  [{name}] {issue}")


def _render_rich(results: list[StageResult]) -> None:
    from rich.text import Text

    statuses = [r.status for r in results if not r.skipped]
    overall = FAIL if FAIL in statuses else (WARN if WARN in statuses else OK)
    color = STATUS_COLOR[overall]

    _console.print()
    _console.rule(f"[bold]TesseractApexOCR Pipeline Audit[/bold]")
    _console.print(
        f"Overall health: [bold {color}]{overall.upper()}[/bold {color}]",
        justify="center",
    )
    _console.print()

    for r in results:
        tag = _status_tag(r.status)
        title = f"{r.name.upper()}  {tag}"

        if r.skipped:
            _console.print(_Panel(f"[dim]{r.skip_reason}[/dim]", title=title, box=_rbox.ROUNDED))
            continue

        tbl = _Table(show_header=False, box=None, padding=(0, 1))
        tbl.add_column("key",   style="dim",  no_wrap=True)
        tbl.add_column("value", style="white")

        for k, v in r.metrics.items():
            if isinstance(v, dict):
                v_str = ", ".join(f"{ek}:{ev}" for ek, ev in list(v.items())[:6])
                if len(v) > 6:
                    v_str += f" (+{len(v)-6} more)"
            elif isinstance(v, list) and v and isinstance(v[0], list):
                v_str = ", ".join(f"{row[0]}×{row[1]}" for row in v[:5]) or "—"
            else:
                v_str = str(v) if v is not None else "—"
            tbl.add_row(k, v_str)

        issue_text = Text()
        for issue in r.issues:
            c = STATUS_COLOR[r.status]
            issue_text.append(f"  {STATUS_ICON[r.status]}  {issue}\n", style=c)

        from rich.console import Group as _Group
        body = _Group(tbl, issue_text) if r.issues else tbl
        _console.print(_Panel(body, title=title, box=_rbox.ROUNDED))

    # Issue summary
    all_issues = [(r.name, r.status, i) for r in results for i in r.issues]
    if all_issues:
        _console.print()
        _console.rule("[bold]Issue Summary[/bold]")
        for name, status, issue in all_issues:
            c = STATUS_COLOR[status]
            _console.print(f"  [{c}]{STATUS_ICON[status]}[/{c}]  [dim]\\[{name}][/dim] {issue}")
        _console.print()


def render_human(results: list[StageResult]) -> None:
    if _RICH:
        _render_rich(results)
    else:
        _render_plain(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="TesseractApexOCR pipeline diagnostic tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python diagnose.py                      # full human-readable audit
  python diagnose.py --json               # machine-readable JSON for AI agents
  python diagnose.py --stage database     # single stage
  python diagnose.py --api-check          # include live API health check
  python diagnose.py --db alt.db --elo-db alt_elo.db
""",
    )
    p.add_argument("--json",      action="store_true",  help="Machine-readable JSON output")
    p.add_argument("--stage",     choices=["config", "database", "parsing", "gemini", "matches", "elo", "api"],
                   help="Run only one stage")
    p.add_argument("--api-check", action="store_true",  help="Include live HTTP check of FastAPI on port 8080")
    p.add_argument("--db",        default=None, metavar="PATH", help="Override killfeed.db path")
    p.add_argument("--elo-db",    default=None, metavar="PATH", dest="elo_db",
                   help="Override elo.db path")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    db_path  = Path(args.db)     if args.db     else KILLFEED_DB_PATH
    elo_path = Path(args.elo_db) if args.elo_db else Path(ELO_DB_PATH)

    stage_map = {
        "config":   lambda: check_config(),
        "database": lambda: check_database(db_path),
        "parsing":  lambda: check_parsing(db_path),
        "gemini":   lambda: check_gemini(db_path),
        "matches":  lambda: check_matches(elo_path),
        "elo":      lambda: check_elo(elo_path),
        "api":      lambda: check_api(),
    }

    if args.stage:
        stages_to_run = [args.stage]
    else:
        stages_to_run = ["config", "database", "parsing", "gemini", "matches", "elo"]
        if args.api_check:
            stages_to_run.append("api")

    results = []
    for name in stages_to_run:
        try:
            result = stage_map[name]()
        except Exception as exc:
            result = StageResult(
                name=name, status=FAIL,
                metrics={}, issues=[f"Stage crashed: {type(exc).__name__}: {exc}"]
            )
        results.append(result)

    if args.json:
        render_json(results)
    else:
        render_human(results)


if __name__ == "__main__":
    main()
