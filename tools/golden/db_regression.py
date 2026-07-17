"""DB regression harness -- Layer A: structural invariants (read-only).

The golden harness (score_ocr.py / replay_dblog.py) guards OCR text + dedup on ONE game's fixed
crops. The identity->ELO layer on the full DB has no automated guard, so canonicalization
mis-merges, dropped kills, corrupt stats, and anon-name leaks are found only by manual audit.

This is Layer A of the plan (bead TesseractApexOCR-zg9): a catalog of properties that must hold for
ANY DB state, each expressed as a query returning violating rows. A check FAILS when its violation
count exceeds its tolerance. Read-only: it never writes to killfeed.db / elo.db.

Layers B (labeled identity regression) and C (drift snapshot) are added to this module later.

Run:
    python tools/golden/db_regression.py                 # human report, exit 1 if any hard check fails
    python tools/golden/db_regression.py --json          # machine-readable
    python tools/golden/db_regression.py --fail-on-warn  # treat WARN as failure too
    python tools/golden/db_regression.py --db killfeed.db --elo-db elo.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --- make the repo importable regardless of CWD ---
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from config import KILLFEED_DB_PATH, CROP_OUTPUT_DIR  # noqa: E402
from elo_db import ELO_DB_PATH  # noqa: E402
from glicko import RD0, RD_FLOOR  # noqa: E402

_EPS = 1e-6
_ELO_LO, _ELO_HI = -1000.0, 4000.0   # sane display band; catches NaN / runaway values, not fine tuning
_CROP_SAMPLE = 200                    # how many non-empty crop rows to spot-check on disk
_MAX_EXAMPLES = 8                     # violating examples printed per check


# ---------------------------------------------------------------------------
# Check framework
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    requires: str            # 'kf' | 'elo' | 'both'
    severity: str            # 'fail' | 'warn'
    count: int               # number of violations
    examples: list           # up to _MAX_EXAMPLES sample rows (strings)
    skipped: bool = False
    note: str = ""


@dataclass
class Check:
    name: str
    requires: str
    severity: str
    fn: Callable            # (ctx) -> (count:int, examples:list[str])
    desc: str = ""


class Ctx:
    """Holds the (read-only) connections. elo_conn has killfeed.db ATTACHed AS kf when both exist."""
    def __init__(self, kf_path: Path, elo_path: Path):
        self.kf_path = kf_path
        self.elo_path = elo_path
        self.kf = sqlite3.connect(f"file:{kf_path}?mode=ro", uri=True) if kf_path.exists() else None
        self.elo = None
        if elo_path.exists():
            self.elo = sqlite3.connect(f"file:{elo_path}?mode=ro", uri=True)
            if self.kf is not None:
                # read-only ATTACH so a concurrently-writing pipeline is never blocked
                self.elo.execute("ATTACH ? AS kf", (f"file:{kf_path}?mode=ro",))
        for c in (self.kf, self.elo):
            if c is not None:
                c.row_factory = sqlite3.Row

    def has_column(self, conn: sqlite3.Connection, table: str, col: str) -> bool:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))

    def close(self):
        for c in (self.kf, self.elo):
            if c is not None:
                try: c.close()
                except Exception: pass


CHECKS: list[Check] = []


def check(name: str, requires: str, severity: str = "fail", desc: str = ""):
    def deco(fn):
        CHECKS.append(Check(name=name, requires=requires, severity=severity, fn=fn, desc=desc))
        return fn
    return deco


# ---------------------------------------------------------------------------
# Layer A checks
# ---------------------------------------------------------------------------

# --- killfeed.db: provenance-column domains ---

@check("icon_vote_domain", "kf", "fail",
       "events.icon_vote must be one of '', 'kill', 'gun'")
def _icon_vote_domain(ctx: Ctx):
    if not ctx.has_column(ctx.kf, "events", "icon_vote"):
        raise _Skip("events.icon_vote missing (DB not migrated)")
    rows = ctx.kf.execute(
        "SELECT id, event_type, icon_vote FROM events "
        "WHERE icon_vote NOT IN ('', 'kill', 'gun') LIMIT ?", (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.kf.execute(
        "SELECT COUNT(*) FROM events WHERE icon_vote NOT IN ('', 'kill', 'gun')"
    ).fetchone()[0]
    return n, [f"id={r['id']} type={r['event_type']} icon_vote={r['icon_vote']!r}" for r in rows]


@check("read_count_ge_1", "kf", "fail",
       "events.read_count must be >= 1 (a row absorbed at least its own read)")
def _read_count_ge_1(ctx: Ctx):
    if not ctx.has_column(ctx.kf, "events", "read_count"):
        raise _Skip("events.read_count missing (DB not migrated)")
    rows = ctx.kf.execute(
        "SELECT id, event_type, read_count FROM events WHERE read_count < 1 LIMIT ?", (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.kf.execute("SELECT COUNT(*) FROM events WHERE read_count < 1").fetchone()[0]
    return n, [f"id={r['id']} type={r['event_type']} read_count={r['read_count']}" for r in rows]


@check("elo_kill_has_a_name", "kf", "warn",
       "a Kill/BleedOut with BOTH names empty can never credit ELO (should be rejected at insert)")
def _elo_kill_has_a_name(ctx: Ctx):
    q = ("event_type IN ('Kill','BleedOut') "
         "AND TRIM(COALESCE(attacker,''))='' AND TRIM(COALESCE(victim,''))=''")
    rows = ctx.kf.execute(
        f"SELECT id, event_type, raw_text FROM events WHERE {q} LIMIT ?", (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.kf.execute(f"SELECT COUNT(*) FROM events WHERE {q}").fetchone()[0]
    return n, [f"id={r['id']} type={r['event_type']} raw={r['raw_text']!r}" for r in rows]


# --- elo.db <-> killfeed.db: referential provenance ---

@check("source_event_id_resolves", "both", "fail",
       "every non-null match_kills.source_event_id must resolve to a killfeed events.id")
def _source_event_id_resolves(ctx: Ctx):
    if not ctx.has_column(ctx.elo, "match_kills", "source_event_id"):
        raise _Skip("match_kills.source_event_id missing (DB not migrated)")
    rows = ctx.elo.execute(
        "SELECT mk.id, mk.match_id, mk.source_event_id FROM match_kills mk "
        "WHERE mk.source_event_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM kf.events e WHERE e.id = mk.source_event_id) LIMIT ?",
        (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.elo.execute(
        "SELECT COUNT(*) FROM match_kills mk WHERE mk.source_event_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM kf.events e WHERE e.id = mk.source_event_id)"
    ).fetchone()[0]
    return n, [f"mk.id={r['id']} match={r['match_id'][:20]} orphan_event_id={r['source_event_id']}"
               for r in rows]


# --- elo.db: identity ---

@check("no_self_kills", "elo", "fail",
       "match_kills must not credit a kill where attacker == victim")
def _no_self_kills(ctx: Ctx):
    q = "attacker IS NOT NULL AND TRIM(attacker) != '' AND attacker = victim"
    rows = ctx.elo.execute(
        f"SELECT match_id, kill_order, attacker FROM match_kills WHERE {q} LIMIT ?", (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.elo.execute(f"SELECT COUNT(*) FROM match_kills WHERE {q}").fetchone()[0]
    return n, [f"match={r['match_id'][:20]} #{r['kill_order']} {r['attacker']!r}=self" for r in rows]


@check("no_anon_in_ratings", "elo", "fail",
       "anonymized Legend#### names must never appear in player_ratings (non-identifying)")
def _no_anon_in_ratings(ctx: Ctx):
    from elo_engine import _is_anonymized_player   # production definition of "anon"
    players = [r[0] for r in ctx.elo.execute("SELECT player FROM player_ratings")]
    bad = [p for p in players if _is_anonymized_player(p)]
    return len(bad), bad[:_MAX_EXAMPLES]


# --- elo.db: rating sanity ---

@check("matches_played_ge_1", "elo", "fail",
       "a rated player must have played at least one match")
def _matches_played_ge_1(ctx: Ctx):
    rows = ctx.elo.execute(
        "SELECT player, matches_played FROM player_ratings WHERE matches_played < 1 LIMIT ?",
        (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.elo.execute("SELECT COUNT(*) FROM player_ratings WHERE matches_played < 1").fetchone()[0]
    return n, [f"{r['player']!r} matches={r['matches_played']}" for r in rows]


@check("glicko_ranges", "elo", "fail",
       "player_ratings Glicko fields in range: RD_FLOOR<=rd<=RD0, vol>0, peak>=elo, elo sane")
def _glicko_ranges(ctx: Ctx):
    q = (f"rd < {RD_FLOOR - _EPS} OR rd > {RD0 + _EPS} OR vol <= 0 "
         f"OR peak_elo < elo - {_EPS} OR elo < {_ELO_LO} OR elo > {_ELO_HI}")
    rows = ctx.elo.execute(
        f"SELECT player, elo, rd, vol, peak_elo FROM player_ratings WHERE {q} LIMIT ?",
        (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.elo.execute(f"SELECT COUNT(*) FROM player_ratings WHERE {q}").fetchone()[0]
    return n, [f"{r['player']!r} elo={r['elo']:.1f} rd={r['rd']:.1f} vol={r['vol']:.4f} "
               f"peak={r['peak_elo']:.1f}" for r in rows]


@check("placement_backed_by_kill", "elo", "warn",
       "a definitively-eliminated placement (survived=0) should appear as a victim in that match")
def _placement_backed_by_kill(ctx: Ctx):
    q = ("mp.survived = 0 AND NOT EXISTS "
         "(SELECT 1 FROM match_kills mk WHERE mk.match_id = mp.match_id AND mk.victim = mp.player)")
    rows = ctx.elo.execute(
        f"SELECT mp.match_id, mp.player, mp.kill_order_out FROM match_placements mp WHERE {q} LIMIT ?",
        (_MAX_EXAMPLES,)
    ).fetchall()
    n = ctx.elo.execute(f"SELECT COUNT(*) FROM match_placements mp WHERE {q}").fetchone()[0]
    return n, [f"match={r['match_id'][:20]} {r['player']!r} out@{r['kill_order_out']}" for r in rows]


# --- killfeed.db: crop provenance on disk (sampled) ---

@check("crop_files_exist", "kf", "warn",
       "a sample of non-empty crop_filename rows should resolve to a raw crop on disk")
def _crop_files_exist(ctx: Ctx):
    root = Path(CROP_OUTPUT_DIR)
    rows = ctx.kf.execute(
        "SELECT streamer, crop_filename FROM events WHERE crop_filename != '' "
        "ORDER BY id DESC LIMIT ?", (_CROP_SAMPLE,)
    ).fetchall()
    missing = []
    for r in rows:
        p = root / r["streamer"] / f"{r['crop_filename']}_raw.png"
        if not p.exists():
            missing.append(f"{r['streamer']}/{r['crop_filename']}_raw.png")
    return len(missing), missing[:_MAX_EXAMPLES]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class _Skip(Exception):
    """Raised by a check when its precondition (e.g. a migrated column) is absent."""


def run(kf_path: Path, elo_path: Path) -> list[Result]:
    ctx = Ctx(kf_path, elo_path)
    results: list[Result] = []
    try:
        for c in CHECKS:
            have_kf, have_elo = ctx.kf is not None, ctx.elo is not None
            need_ok = ({"kf": have_kf, "elo": have_elo, "both": have_kf and have_elo}[c.requires])
            if not need_ok:
                results.append(Result(c.name, c.requires, c.severity, 0, [], skipped=True,
                                      note=f"requires {c.requires} DB (not found)"))
                continue
            try:
                count, examples = c.fn(ctx)
                results.append(Result(c.name, c.requires, c.severity, count, examples))
            except _Skip as s:
                results.append(Result(c.name, c.requires, c.severity, 0, [], skipped=True, note=str(s)))
    finally:
        ctx.close()
    return results


def _print_report(results: list[Result]) -> int:
    print("=" * 78)
    print("DB REGRESSION HARNESS  --  Layer A: structural invariants")
    print("=" * 78)
    worst = 0
    for r in results:
        if r.skipped:
            print(f"  SKIP  {r.name:26s}  ({r.note})")
            continue
        ok = r.count == 0
        tag = "PASS" if ok else ("FAIL" if r.severity == "fail" else "WARN")
        if not ok:
            worst = max(worst, 2 if r.severity == "fail" else 1)
        print(f"  {tag:4s}  {r.name:26s}  violations={r.count}")
        for ex in r.examples:
            print(f"           - {ex}")
    print("-" * 78)
    hard = sum(1 for r in results if not r.skipped and r.severity == "fail" and r.count)
    soft = sum(1 for r in results if not r.skipped and r.severity == "warn" and r.count)
    print(f"  {hard} hard failure(s), {soft} warning(s), "
          f"{sum(1 for r in results if r.skipped)} skipped.")
    return worst


def main() -> int:
    ap = argparse.ArgumentParser(description="DB regression harness (Layer A: structural invariants)")
    ap.add_argument("--db", type=Path, default=KILLFEED_DB_PATH, help="killfeed.db path")
    ap.add_argument("--elo-db", type=Path, default=ELO_DB_PATH, help="elo.db path")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on-warn", action="store_true", help="treat WARN as failure (exit 1)")
    args = ap.parse_args()

    results = run(args.db, args.elo_db)

    if args.json:
        print(json.dumps([{
            "name": r.name, "requires": r.requires, "severity": r.severity,
            "count": r.count, "skipped": r.skipped, "note": r.note,
            "examples": r.examples,
        } for r in results], indent=2))
    else:
        _print_report(results)

    hard_fail = any(not r.skipped and r.severity == "fail" and r.count for r in results)
    warn_hit = any(not r.skipped and r.severity == "warn" and r.count for r in results)
    return 1 if (hard_fail or (args.fail_on_warn and warn_hit)) else 0


if __name__ == "__main__":
    sys.exit(main())
