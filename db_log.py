"""SQLite-backed killfeed event log.

Replaces killfeed_log.csv with a proper database.  Key features:
- WAL mode: concurrent reads never block writes
- Thread-safe single-writer connection (all inserts go through _write_conn + lock)
- Per-thread read connections (check_same_thread=False with WAL is safe)
- Auto-migrates killfeed_log.csv on first init if the DB is new
- Extra columns: source ('trocr' | 'gemini') and gemini_corrected (0 | 1)

Public API
----------
init_db(path)          -- create schema + migrate CSV if needed
insert_event(...)      -- thread-safe insert, returns row id
query_events(...)      -- filtered query, returns list[dict]
get_total_count(path)  -- fast COUNT(*) for health checks
"""

import csv
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# Suppress re-inserting the same (streamer, event_type, attacker, victim) if it was already
# logged within this many seconds. ocr.py's in-memory event_tracker groups repeated OCR reads
# of the same still-visible killfeed banner within EVENT_WINDOW (config.py, ~seconds) and flushes
# once no new matching read arrives -- but killfeed banners routinely stay on screen longer than
# that window (revive sequences, queued multi-kills), so the same visible banner gets flushed,
# written, and then re-detected as "new" on the next read, over and over, for as long as it's
# visible. Confirmed directly: one revive banner produced 109 separate DB rows over 44 seconds.
# This is a second, independent safety net at the DB layer -- it doesn't depend on tuning the
# in-memory window correctly, and also covers worker restarts resetting that in-memory state.
DEDUP_WINDOW_SECONDS = 20

# Longer-horizon suppression for "sticky" on-screen lines (see KNOWN_ISSUES.md, "Persistent
# on-screen kill line"): some game-UI elements (e.g. the death/spectate-screen "your killer"
# line) are visually identical to a killfeed entry but stay on screen for many minutes, getting
# re-flushed and re-inserted every time the 20s dedup window lapses (confirmed: 60 rows over 38
# minutes for one such line). A blanket long dedup window would be WRONG -- in Apex the same
# (attacker, victim) pair legitimately repeats: a knockdown and its finisher are separate feed
# entries, and after a revive or respawn-beacon cycle the pair can recur again minutes later.
# The distinguishing signal is cadence: legit repeats come as short bursts (knock+finish) or
# minutes apart (revive/respawn cycles), while sticky lines recur CONTINUOUSLY. So: chain
# together inserts of the same line that land within STICKY_CHAIN_GAP_SECONDS of the previous
# one, and suppress once a chain exceeds STICKY_CHAIN_MAX_ROWS. A knock+finish plus a full
# respawn-cycle re-knock+re-finish inside 10 minutes still fits (4 rows); an unbroken
# every-minute recurrence does not. Chain state is in-memory (cheap) but seeded from the DB on
# a cold key so worker restarts don't reset an active chain.
#
# CRITICAL: matched FUZZILY, not by exact tuple. EasyOCR garbles the names slightly on each
# re-read of ONE persistent line ('kpaubgsing'/'kpaub9sing', 'ofps vesoson'/'vesoson'), so exact
# (attacker, victim) chaining never triggered and a single line was logged 11x over 13 min
# (confirmed 2026-07-05). We cluster inserts whose normalized 'attacker|victim' signature is
# >= STICKY_SIM_THRESHOLD similar. This is safe against dropping legit distinct kills because
# suppression only kicks in AFTER STICKY_CHAIN_MAX_ROWS rows of continuous recurrence -- two
# different kills with similar names don't recur 5+ times back-to-back. The 20s exact dedup above
# is deliberately left EXACT (fuzzy there could drop a real second kill within the window).
STICKY_CHAIN_GAP_SECONDS = 150   # max gap between inserts for them to count as one chain
STICKY_CHAIN_MAX_ROWS    = 4     # rows allowed per chain before suppression kicks in
STICKY_SEED_LOOKBACK_SECONDS = 1800 # DB lookback used to seed chain length on a cold key
                                    # (widened from 600s so a persistent line whose re-reads are
                                    # minutes apart still seeds a suppressing chain -- bead 0ef)
STICKY_SIM_THRESHOLD = 0.82      # fuzzy ratio to treat two jittered reads as the same line (full pair)
# VICTIM-ANCHORED chaining (bead 0ef, 2026-07-14): the full-pair signature above is defeated by
# attacker name-jitter -- a persistent line "grape [Bleed Out] cerb" re-read as crao/babyuon/2i.grape
# ->cerb never chains, so cap suppression never fires (measured: ~41% of kill rows were such re-reads).
# Since a victim can only die once (until a multi-minute respawn), repeated rows with the SAME victim
# are sticky re-reads regardless of attacker jitter, while a real multikill has DIFFERENT victims and
# is never merged. So also treat two reads as the same line when their VICTIMS match >= this ratio
# (guarded by _distinct_default_name_victim so gibraltar2127 vs gibraltar1619 stay distinct).
STICKY_VIC_ANCHOR_SIM = 0.85

# Legit same-pair REPEATS inside the chain window differ by event type (measured 2026-07-05):
#  - Kill / BleedOut FEED ELO, and a player can't be re-killed until they respawn (a beacon takes
#    minutes, longer than the chain window). So a same-pair repeat inside 150s is never a legit
#    second kill -- it's a sticky line or a garble. Keep these STRICT (suppress past MAX_ROWS) so
#    phantom ELO events die fast. (Of Kill/BleedOut clusters exceeding 4, the short-span ones are
#    sticky; BleedOut had ZERO short-span clusters.)
#  - Knock (and any other non-ELO type) legitimately repeats inside the window: knock -> revive ->
#    re-knock in one fight. Suppressing those by raw count risks dropping real knocks (~0.45% of
#    knock clusters were legit 5-6-row bursts inside 150s). For these, only suppress recurrence
#    that is SUSTAINED past STICKY_CHAIN_MIN_SPAN_SECONDS (a multi-minute sticky line) -- a short
#    in-fight burst is kept -- with an absolute HARD_CAP backstop for a pathologically fast sticky
#    line. Over-collecting a non-ELO knock costs only timeline noise, never ELO accuracy.
STICKY_CHAIN_MIN_SPAN_SECONDS = 180   # non-ELO: chain must persist this long before span-suppress
STICKY_CHAIN_HARD_CAP         = 8     # non-ELO: absolute row cap regardless of span
STICKY_ELO_EVENT_TYPES = frozenset({"Kill", "BleedOut"})  # strict suppression (these feed ELO)

# ELO types get a TIGHTER cap than the shared STICKY_CHAIN_MAX_ROWS (=4). A same-pair Kill/BleedOut
# repeat inside the 150s chain window is never a legit second kill (the victim can't respawn that
# fast), so an ELO chain past this length is a sticky line or an OCR garble re-read -- keep only the
# first N and suppress the rest. Measured on the 2026-07-09 run (scratch/measure_sticky_lever.py):
# 4 -> 2 removes 21% of ELO rows and the hit chains are all same-tuple repeats within ~150s (no
# distinct kills). Set to 2 (not 1) as a conservative buffer: still absorbs a genuinely-fast
# knock->finish->refinish edge and a single stray re-read before suppressing. Distinct default-name
# players (axle1456 vs axle8599, ratio 0.5) stay below STICKY_SIM_THRESHOLD=0.82 so they never chain.
STICKY_ELO_CHAIN_MAX_ROWS = 2

# (streamer, event_type) -> list of clusters, each {"sig","ts","len","id"}; one cluster per
# distinct persistent line, matched fuzzily so OCR-jittered re-reads share a chain.
_chain_state: dict = {}

_NORM_RE = re.compile(r"[^a-z0-9]")
_DIGITS_RE = re.compile(r"\d+")
_NONDIGIT_RE = re.compile(r"\D+")

# SAME_VICTIM_GUARD tuning: a suppression candidate is a DISTINCT victim (a real multikill, keep it)
# only if it shares a kept victim's alpha-stem this closely but its digit-suffix differs this much.
STICKY_GUARD_VIC_SIM  = 0.85   # >= this fuzzy ratio to a kept victim => same victim (sticky, suppress)
STICKY_GUARD_STEM_SIM = 0.85   # alpha-stem must match a kept victim at least this much to compare
STICKY_GUARD_DIGIT_SIM = 0.60  # ...and digit-suffix must differ MORE than this to count as distinct


def _distinct_default_name_victim(victim: str, kept_vics: list) -> bool:
    """SAME_VICTIM_GUARD (stem-aware): return True iff `victim` is a genuinely DIFFERENT default-name
    player than every victim already kept in this sticky chain -- i.e. it does NOT fuzzy-match a kept
    victim, but DOES share a kept victim's alpha-stem while carrying a clearly different numeric
    suffix (gibraltar2127 vs gibraltar1619). That pattern is a one-attacker multikill of two default-
    named players, which the fuzzy chain wrongly merges; keep it. A garble of the same victim (no
    numeric suffix, or same-ish digits, or a different stem) returns False -> stays sticky-suppressed."""
    v = _NORM_RE.sub("", (victim or "").lower())
    vdig = _NONDIGIT_RE.sub("", v)
    if not vdig:
        return False                                   # no numeric suffix: can't tell from a garble
    vstem = _DIGITS_RE.sub("", v)
    for kv in kept_vics:
        if SequenceMatcher(None, v, kv).ratio() >= STICKY_GUARD_VIC_SIM:
            return False                               # matches a kept victim => sticky re-read
    for kv in kept_vics:
        kdig = _NONDIGIT_RE.sub("", kv)
        kstem = _DIGITS_RE.sub("", kv)
        if kdig and SequenceMatcher(None, vstem, kstem).ratio() >= STICKY_GUARD_STEM_SIM \
                and SequenceMatcher(None, vdig, kdig).ratio() < STICKY_GUARD_DIGIT_SIM:
            return True                                # same legend-stem, clearly different number
    return False


def _line_sig(attacker: str, victim: str) -> str:
    """Normalized 'attacker|victim' signature for fuzzy sticky-line matching: lowercased,
    alphanumerics only (drops spaces/brackets/punctuation OCR adds inconsistently)."""
    return _NORM_RE.sub("", (attacker or "").lower()) + "|" + _NORM_RE.sub("", (victim or "").lower())


def _seed_cluster(conn, streamer, event_type, sig, now, merge_types=False, vic_norm="") -> tuple[int, Optional[int]]:
    """Seed a cold cluster's length + last row id by fuzzy-matching recent DB rows for this line,
    so a worker restart doesn't reset an active sticky chain -- and so a persistent line whose
    re-reads are minutes apart (beyond the in-memory chain gap) still seeds a suppressing chain.
    Matches full-pair fuzzy OR victim-anchored (attacker jitter) like the live chain. Bounded to
    the lookback window. merge_types=True seeds across ALL event types (Layer 2: one physical
    line's kill/knock/bleedout reads share a chain) -- see DESIGN_persistence_aware_icon_vote.md."""
    if merge_types:
        rows = conn.execute(
            "SELECT id, attacker, victim FROM events WHERE streamer=? "
            "AND created_at >= ? ORDER BY id DESC LIMIT 200",
            (streamer, int(now) - STICKY_SEED_LOOKBACK_SECONDS)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, attacker, victim FROM events WHERE streamer=? AND event_type=? "
            "AND created_at >= ? ORDER BY id DESC LIMIT 200",
            (streamer, event_type, int(now) - STICKY_SEED_LOOKBACK_SECONDS)
        ).fetchall()
    count = 0
    last_id = None
    kept_vics: list = []
    for r in rows:
        rv = _NORM_RE.sub("", (r["victim"] or "").lower())
        full = SequenceMatcher(None, sig, _line_sig(r["attacker"], r["victim"])).ratio() >= STICKY_SIM_THRESHOLD
        vic = bool(vic_norm) and bool(rv) and SequenceMatcher(None, vic_norm, rv).ratio() >= STICKY_VIC_ANCHOR_SIM \
            and not (vic_norm and kept_vics and _distinct_default_name_victim(vic_norm, kept_vics))
        if full or vic:
            count += 1
            if rv and rv not in kept_vics:
                kept_vics.append(rv)
            if last_id is None:       # rows are DESC, so the first match is the latest row
                last_id = r["id"]
    return count + 1, last_id

# Windows console UTF-8
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer         TEXT    NOT NULL DEFAULT '',
    timestamp        TEXT    NOT NULL DEFAULT '',
    raw_text         TEXT    DEFAULT '',
    canonical        TEXT    DEFAULT '',
    event_type       TEXT    DEFAULT '',
    attacker         TEXT    DEFAULT '',
    victim           TEXT    DEFAULT '',
    attacker_conf    REAL    DEFAULT 1.0,
    victim_conf      REAL    DEFAULT 1.0,
    source           TEXT    DEFAULT 'trocr',
    gemini_corrected INTEGER DEFAULT 0,
    crop_filename    TEXT    DEFAULT '',
    created_at       INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_ev_streamer   ON events(streamer);
CREATE INDEX IF NOT EXISTS idx_ev_type       ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_ev_timestamp  ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_attacker   ON events(attacker);
CREATE INDEX IF NOT EXISTS idx_ev_victim     ON events(victim);
CREATE INDEX IF NOT EXISTS idx_ev_source     ON events(source);
"""

_INSERT = """
INSERT INTO events
    (streamer, timestamp, raw_text, canonical, event_type,
     attacker, victim, attacker_conf, victim_conf,
     source, gemini_corrected, crop_filename)
VALUES
    (?,?,?,?,?,?,?,?,?,?,?,?)
"""

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_write_lock   = threading.Lock()
_write_conn:  Optional[sqlite3.Connection] = None
_db_path_used: Optional[Path] = None

_thread_local = threading.local()


def _get_write_conn(path: Path) -> sqlite3.Connection:
    global _write_conn, _db_path_used
    if _write_conn is None or _db_path_used != path:
        if _write_conn:
            _write_conn.close()
        _write_conn = sqlite3.connect(str(path), check_same_thread=False)
        _write_conn.row_factory = sqlite3.Row
        _db_path_used = path
        _write_conn.executescript(_SCHEMA)
        _write_conn.commit()
    return _write_conn


def _get_read_conn(path: Path) -> sqlite3.Connection:
    """Per-thread read connection (WAL allows concurrent readers)."""
    conn = getattr(_thread_local, "conn", None)
    conn_path = getattr(_thread_local, "conn_path", None)
    if conn is None or conn_path != path:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _thread_local.conn = conn
        _thread_local.conn_path = path
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(path: Path, csv_path: Optional[Path] = None) -> None:
    """Create schema and optionally migrate existing CSV data.

    Call once at startup. If the DB is new (empty) and csv_path exists,
    all CSV rows are imported automatically.
    """
    with _write_lock:
        conn = _get_write_conn(path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    if count == 0 and csv_path and csv_path.exists():
        print(f"[db_log] New DB — importing {csv_path} …", flush=True)
        imported = _import_csv(path, csv_path)
        print(f"[db_log] Imported {imported:,} rows from CSV.", flush=True)
    else:
        print(f"[db_log] Using {path} ({count:,} existing events).", flush=True)


def _import_csv(db_path: Path, csv_path: Path) -> int:
    """Bulk-import CSV into the DB. Returns row count."""
    rows = []
    with csv_path.open(encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                a_conf = float(row.get("attacker_conf") or 1.0)
            except ValueError:
                a_conf = 1.0
            try:
                v_conf = float(row.get("victim_conf") or 1.0)
            except ValueError:
                v_conf = 1.0
            rows.append((
                row.get("streamer", ""),
                row.get("timestamp", ""),
                row.get("raw_text", ""),
                row.get("canonical", ""),
                row.get("event_type", ""),
                row.get("attacker", ""),
                row.get("victim", ""),
                a_conf,
                v_conf,
                "trocr",   # source
                0,         # gemini_corrected
                "",        # crop_filename
            ))

    with _write_lock:
        conn = _get_write_conn(db_path)
        conn.executemany(_INSERT, rows)
        conn.commit()

    return len(rows)


def insert_event(
    streamer: str,
    timestamp: str,
    raw_text: str,
    canonical: str,
    event_type: str,
    attacker: str = "",
    victim: str = "",
    attacker_conf: float = 1.0,
    victim_conf: float = 1.0,
    source: str = "trocr",
    gemini_corrected: int = 0,
    crop_filename: str = "",
    db_path: Optional[Path] = None,
) -> int:
    """Thread-safe insert. Returns the new row id (or the existing row's id if this is a
    recent duplicate or part of a sticky-line chain -- see DEDUP_WINDOW_SECONDS and
    STICKY_CHAIN_MAX_ROWS)."""
    from config import KILLFEED_DB_PATH
    db = db_path or KILLFEED_DB_PATH
    # Drop unusable ELO rows: a Kill/BleedOut with NEITHER an attacker NOR a victim can never credit a
    # rating and is pure crop-truncation noise (e.g. 'Bleed Out] pathfinder1426' with the attacker cut
    # off the left edge, victim unparseable) -- ~0.4% of stored events (bug audit 2026-07-16). Reject
    # before the DB touch so they never pollute the killfeed log or downstream counts.
    if event_type in ("Kill", "BleedOut") and not (attacker or "").strip() and not (victim or "").strip():
        return -1
    with _write_lock:
        conn = _get_write_conn(db)
        active_cluster = None   # set when this row belongs to a fuzzy sticky-line chain

        if attacker and victim:
            now = time.time()
            cutoff = int(now) - DEDUP_WINDOW_SECONDS
            existing = conn.execute(
                "SELECT id FROM events WHERE streamer=? AND event_type=? AND attacker=? "
                "AND victim=? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                (streamer, event_type, attacker, victim, cutoff)
            ).fetchone()
            if existing:
                return existing["id"]

            # Fuzzy sticky-line chain suppression (see comment on STICKY_CHAIN_GAP_SECONDS above).
            # STICKY_CHAIN_MERGE_TYPES (Layer 2): key the chain by name-pair only, so kill/knock/
            # bleedout reads of ONE physical line accumulate together (an icon flip or a knock->
            # bleedout can't split one sticky line across two under-cap chains). The per-insert
            # ELO/non-ELO cap branch below still uses THIS row's event_type.
            from config import STICKY_CHAIN_MERGE_TYPES, SAME_VICTIM_GUARD
            sig = _line_sig(attacker, victim)
            vic_norm = _NORM_RE.sub("", (victim or "").lower())
            group_key = (streamer,) if STICKY_CHAIN_MERGE_TYPES else (streamer, event_type)
            clusters = _chain_state.setdefault(group_key, [])
            # Drop clusters whose last insert is older than the chain gap (chain broken).
            clusters[:] = [c for c in clusters if now - c["ts"] < STICKY_CHAIN_GAP_SECONDS]

            def _same_sticky_line(c) -> bool:
                # Full-pair fuzzy match (original behavior)...
                if SequenceMatcher(None, sig, c["sig"]).ratio() >= STICKY_SIM_THRESHOLD:
                    return True
                # ...OR victim-anchored: same victim as one kept in this chain (attacker may jitter),
                # unless it's a genuinely distinct default-name victim (stem-aware multikill guard).
                if vic_norm and any(
                    SequenceMatcher(None, vic_norm, kv).ratio() >= STICKY_VIC_ANCHOR_SIM
                    for kv in c["vics"]
                ):
                    if not (SAME_VICTIM_GUARD and _distinct_default_name_victim(vic_norm, c["vics"])):
                        return True
                return False

            match = next((c for c in clusters if _same_sticky_line(c)), None)
            if match is None:
                seed_len, seed_id = _seed_cluster(conn, streamer, event_type, sig, now,
                                                  STICKY_CHAIN_MERGE_TYPES, vic_norm)
                # "vics": distinct victims KEPT in this chain, for the stem-aware multikill guard.
                match = {"sig": sig, "ts": now, "start": now, "len": seed_len, "id": seed_id,
                         "vics": [vic_norm]}
                clusters.append(match)
            else:
                match["len"] += 1
                match["ts"] = now

            if len(_chain_state) > 10000:
                stale = now - STICKY_CHAIN_GAP_SECONDS
                for k in [k for k, v in _chain_state.items()
                          if not v or all(c["ts"] < stale for c in v)]:
                    del _chain_state[k]

            # ELO event types (Kill/BleedOut): strict — suppress past MAX_ROWS (no legit same-pair
            # repeat exists inside the window). Non-ELO (Knock etc.): only suppress recurrence
            # sustained past MIN_SPAN, so a short in-fight knock->res->reknock burst survives, with
            # a HARD_CAP backstop for a fast sticky line.
            if event_type in STICKY_ELO_EVENT_TYPES:
                suppress = match["len"] > STICKY_ELO_CHAIN_MAX_ROWS
                # Stem-aware multikill guard: don't suppress a genuinely different default-name
                # victim (gibraltar2127 vs gibraltar1619) that the fuzzy chain merged in.
                if suppress and SAME_VICTIM_GUARD and \
                        _distinct_default_name_victim(vic_norm, match["vics"]):
                    suppress = False
            else:
                span = now - match["start"]
                suppress = (match["len"] > STICKY_CHAIN_HARD_CAP
                            or (match["len"] > STICKY_CHAIN_MAX_ROWS
                                and span > STICKY_CHAIN_MIN_SPAN_SECONDS))
            if suppress and match["id"] is not None:
                return match["id"]
            # This row will be inserted (kept): record its victim for the guard's future comparisons.
            if vic_norm and vic_norm not in match["vics"]:
                match["vics"].append(vic_norm)
            active_cluster = match

        cursor = conn.execute(_INSERT, (
            streamer, timestamp, raw_text, canonical, event_type,
            attacker, victim, attacker_conf, victim_conf,
            source, gemini_corrected, crop_filename
        ))
        conn.commit()
        if active_cluster is not None:
            active_cluster["id"] = cursor.lastrowid
        return cursor.lastrowid


def query_events(
    path:        Optional[Path] = None,
    streamer:    Optional[str]  = None,
    event_type:  Optional[str]  = None,
    attacker:    Optional[str]  = None,
    victim:      Optional[str]  = None,
    from_ts:     Optional[str]  = None,
    to_ts:       Optional[str]  = None,
    source:      Optional[str]  = None,
    gemini_only: bool           = False,
    limit:       Optional[int]  = None,
    offset:      int            = 0,
    order_desc:  bool           = False,
) -> list[dict]:
    """Filtered query, returns list of dicts (column names as keys)."""
    from config import KILLFEED_DB_PATH
    db = path or KILLFEED_DB_PATH

    clauses: list[str] = []
    params:  list      = []

    if streamer:
        clauses.append("LOWER(streamer) = LOWER(?)")
        params.append(streamer)
    if event_type:
        clauses.append("LOWER(event_type) = LOWER(?)")
        params.append(event_type)
    if attacker:
        clauses.append("LOWER(attacker) LIKE LOWER(?)")
        params.append(f"%{attacker}%")
    if victim:
        clauses.append("LOWER(victim) LIKE LOWER(?)")
        params.append(f"%{victim}%")
    if from_ts:
        clauses.append("timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("timestamp <= ?")
        params.append(to_ts)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if gemini_only:
        clauses.append("gemini_corrected = 1")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "DESC" if order_desc else "ASC"
    lim   = f"LIMIT {int(limit)} OFFSET {int(offset)}" if limit is not None else ""

    sql = f"SELECT * FROM events {where} ORDER BY id {order} {lim}"

    conn = _get_read_conn(db)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_events(
    path:       Optional[Path] = None,
    streamer:   Optional[str]  = None,
    event_type: Optional[str]  = None,
    source:     Optional[str]  = None,
) -> int:
    from config import KILLFEED_DB_PATH
    db = path or KILLFEED_DB_PATH

    clauses: list[str] = []
    params:  list      = []
    if streamer:
        clauses.append("LOWER(streamer) = LOWER(?)")
        params.append(streamer)
    if event_type:
        clauses.append("LOWER(event_type) = LOWER(?)")
        params.append(event_type)
    if source:
        clauses.append("source = ?")
        params.append(source)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT COUNT(*) FROM events {where}"
    conn = _get_read_conn(db)
    return conn.execute(sql, params).fetchone()[0]
