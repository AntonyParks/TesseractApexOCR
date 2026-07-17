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
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
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
# Layer B: labeled identity regression
# ---------------------------------------------------------------------------
# Known-answer name fixtures run through the REAL canonicalization/dedupe. Two paths:
#   'live'   -> feed OCR variants into a fresh PlayerDatabase (per-event first-seen/confusion merge)
#   'dedupe' -> build player_ratings rows, run reprocess.deduplicate_players (leaderboard merge)
# Each fixture is a GUARD (currently correct; a change that breaks it is a regression -> hard FAIL)
# or a GAP (a currently-wrong behavior we've confirmed; reported as GAP, never blocks -- and if it
# starts producing the CORRECT answer the harness says GAP-CLOSED so we can promote it to a guard).
# Grow this list from every real mis-merge / mis-pick found. All fixtures below were verified against
# live behavior on 2026-07-17.

_B_FIXTURES = [
    # --- live canonicalization guards (must stay correct) ---
    {"id": "live.merge.axle_8b", "path": "live", "kind": "identities",
     "variants": ["axle8b44", "axlebb44"], "expect": 1, "status": "guard",
     "note": "confusion 8<->b: two OCR reads of one player must fold to one identity"},
    {"id": "live.merge.iakme_lakme", "path": "live", "kind": "identities",
     "variants": ["iakme", "lakme"], "expect": 1, "status": "guard",
     "note": "confusion i<->l (different prefix, 0.80 raw ratio): must still merge"},
    {"id": "live.distinct.gibraltar_anon", "path": "live", "kind": "identities",
     "variants": ["gibraltar2127", "gibraltar1619"], "expect": 2, "status": "guard",
     "note": "distinct anon (different ####): _anon_digit_conflict must keep them separate"},
    {"id": "live.distinct.axle_default", "path": "live", "kind": "identities",
     "variants": ["axle1456", "axle8599"], "expect": 2, "status": "guard",
     "note": "distinct default-name players must not merge"},

    # --- dedupe canonical-choice guard ---
    {"id": "dedupe.canon.mixedcase_wins", "path": "dedupe", "kind": "canonical",
     "rows": [["Jimmy", 3], ["jimmy", 2], ["jimmyy", 4]], "expect": "Jimmy", "status": "guard",
     "note": "mixed-case CamelCase must beat lowercase OCR mangles as the display name"},

    # --- KNOWN GAPS (confirmed bugs; informational until fixed -- see beads) ---
    {"id": "dedupe.canon.lowercase_bias", "path": "dedupe", "kind": "canonical",
     "rows": [["jimmyy", 5], ["jimmy", 2]], "expect": "jimmy", "status": "gap",
     "note": "canonical bias: both lowercase -> most-played garble 'jimmyy' wins over correct 'jimmy' "
             "(get_canonical_score has no correctness/confidence signal)"},
    {"id": "dedupe.distinct.anon_overmerge", "path": "dedupe", "kind": "distinct",
     "rows": [["gibraltar2127", 4], ["gibraltar1619", 3]], "expect": None, "status": "gap",
     "note": "dedupe raw-ratio edge lacks _anon_digit_conflict -> merges distinct anon "
             "(latent: masked because anon are excluded before dedupe)"},
]


def _live_identities(variants: list[str], reps: int = 4) -> set[str]:
    """Feed variants as timed observations into a fresh PlayerDatabase, return the distinct
    canonical identities they resolve to. reps>1 so frequency-based promotion can act."""
    from database import PlayerDatabase
    db = PlayerDatabase()
    t = 1000.0
    for _ in range(reps):
        for v in variants:
            db.add_name_observation(v, t)
            t += 1.0
    return {db.find_best_canonical_match(v, t)[0] for v in variants}


def _dedupe_merges(rows: list) -> list:
    """rows = [[name, matches_played], ...]. Build a throwaway elo.db and return the real
    deduplicate_players() merge decisions (list of (canonical, duplicates, stats))."""
    import elo_db
    import reprocess
    tmp = Path(tempfile.mkdtemp()) / "elo_b.db"
    elo_db.init_db(tmp)
    for name, mp in rows:
        elo_db.update_player_rating(name, 1000.0, int(mp), 0, 0, path=tmp)
    return reprocess.deduplicate_players(tmp, dry_run=True)


def _eval_fixture(fx: dict) -> tuple[bool, str]:
    """Return (matches_correct_answer, human_detail)."""
    if fx["path"] == "live":
        ids = _live_identities(fx["variants"])
        actual = len(ids)
        detail = f"{fx['variants']} -> {actual} id(s) {sorted(ids)} (expect {fx['expect']})"
        return actual == fx["expect"], detail
    # dedupe
    merges = _dedupe_merges(fx["rows"])
    names = [r[0] for r in fx["rows"]]
    if fx["kind"] == "canonical":
        canon = merges[0][0] if merges else "(no-merge)"
        detail = f"{names} -> canonical={canon!r} (expect {fx['expect']!r})"
        return canon == fx["expect"], detail
    # kind == 'distinct': correct == they did NOT merge
    merged = bool(merges)
    detail = (f"{names} -> {'MERGED as ' + repr(merges[0][0]) if merged else 'distinct'} "
              f"(expect distinct)")
    return (not merged), detail


def run_layer_b() -> list[Result]:
    """Run the identity fixtures. Needs no live DB. GUARD miss -> hard fail ('fail'); GAP that is
    still wrong -> 'gap' (soft); GAP now correct -> 'warn' (GAP-CLOSED, promote it)."""
    results: list[Result] = []
    for fx in _B_FIXTURES:
        try:
            correct, detail = _eval_fixture(fx)
        except Exception as e:  # a fixture that errors is itself a finding
            results.append(Result(fx["id"], "logic", "fail", 1, [f"error: {e}"], note=fx["note"]))
            continue
        if fx["status"] == "guard":
            sev, cnt = ("fail", 0) if correct else ("fail", 1)
        else:  # gap
            if correct:
                sev, cnt = "warn", 1   # GAP-CLOSED: the bug is fixed -> promote to guard
            else:
                sev, cnt = "gap", 1    # still the known-wrong behavior
        results.append(Result(fx["id"], "logic", sev, cnt, [detail], note=fx["note"]))
    return results


# ---------------------------------------------------------------------------
# Layer C: drift snapshot
# ---------------------------------------------------------------------------
# Replay the vision-golden game deterministically through the FULL identity->ELO pipeline (parse ->
# db_log dedup -> match detection -> Glicko -> leaderboard dedupe) and snapshot the aggregate
# outcome (player count, credited kills, the board, the merge decisions). Compare to a committed
# baseline: any delta means a code change shifted this fixed game's result -- caught even when no
# single Layer-A invariant or Layer-B fixture names it. Drift is a WARN (often intended); the
# operator re-baselines deliberately with --update-baseline after reviewing the diff.
#
# Determinism: a fresh PlayerDatabase seeded ONLY with static legends (never the live-mutating
# player_names.json, which ocr.py rewrites on shutdown), db_log driven by golden video-time with
# its sticky-chain state reset. So the snapshot depends only on the golden input + the code.

_BASELINE = Path(__file__).resolve().parent / "data" / "db_snapshot_baseline.json"
_C_EPOCH0 = 1_700_000_000
_C_BASE_DT = datetime(2024, 1, 1)
_C_ELIM = {"Kill", "BleedOut", "ChampionEliminated"}


def _c_ts(t: float) -> str:
    return (_C_BASE_DT + timedelta(seconds=float(t))).strftime("%Y-%m-%d %H:%M:%S")


def _build_golden_snapshot() -> dict:
    """Deterministically replay the golden game and return aggregate-outcome metrics."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # golden_lib lives next to us
    import golden_lib
    import db_log
    import elo_db
    import match_detector
    import reprocess
    from database import PlayerDatabase
    from parsers import parse_killfeed_line

    reads_path = os.path.join(golden_lib.DATA, "vod_capture", "reads.jsonl")
    reads = [json.loads(l) for l in open(reads_path, encoding="utf-8")]
    reads.sort(key=lambda r: r["t"])

    tmp = Path(tempfile.mkdtemp())
    kf = tmp / "kf.db"
    elo = tmp / "elo.db"

    clock = {"now": float(_C_EPOCH0)}
    real_time = db_log.time
    db_log.time = types.SimpleNamespace(time=lambda: clock["now"])   # db_log uses only time.time()
    db_log._chain_state.clear()
    try:
        conn = db_log._get_write_conn(kf)
        pdb = PlayerDatabase()
        pdb.seed_legend_names()                                      # static context only
        last_id = 0
        for r in reads:
            p = parse_killfeed_line(r["text"], pdb, r["t"])
            if p.get("event_type") not in _C_ELIM:
                continue
            atk, vic = p.get("attacker") or "", p.get("victim") or ""
            if not vic:
                continue
            clock["now"] = float(_C_EPOCH0 + r["t"])
            rid = db_log.insert_event("replay", _c_ts(r["t"]), r["text"],
                                      p.get("canonical", r["text"]), p["event_type"],
                                      atk, vic, 1.0, 1.0, db_path=kf)
            if rid and rid > last_id:                               # a NEW row (not suppressed)
                conn.execute("UPDATE events SET created_at=? WHERE id=?",
                             (int(_C_EPOCH0 + r["t"]), rid))
                conn.commit()
                last_id = rid
    finally:
        db_log.time = real_time
        db_log._chain_state.clear()

    elo_db.init_db(elo)
    matches = match_detector.detect_matches_from_db(kf)
    reprocess.batch_reprocess(matches, db_path=elo)
    merges = reprocess.deduplicate_players(elo, dry_run=False)      # apply the leaderboard dedupe

    e = sqlite3.connect(str(elo))
    e.row_factory = sqlite3.Row
    n_players = e.execute("SELECT COUNT(*) FROM player_ratings").fetchone()[0]
    n_matches = e.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    n_kills = e.execute("SELECT COUNT(*) FROM match_kills").fetchone()[0]
    board = [[row["player"], round(row["elo"])] for row in e.execute(
        "SELECT player, elo, rd FROM player_ratings ORDER BY (elo - 2*rd) DESC, player")]
    e.close()
    merge_list = sorted([[c, sorted(d)] for c, d, _ in merges])
    return {"n_players": n_players, "n_matches": n_matches, "n_credited_kills": n_kills,
            "board": board, "merges": merge_list}


def _diff_board(base: list, snap: list) -> list[str]:
    b = {p: elo for p, elo in base}
    s = {p: elo for p, elo in snap}
    out = []
    for p in sorted(set(b) - set(s)):
        out.append(f"removed {p!r} (was elo {b[p]})")
    for p in sorted(set(s) - set(b)):
        out.append(f"added {p!r} (elo {s[p]})")
    for p in sorted(set(b) & set(s)):
        if b[p] != s[p]:
            out.append(f"elo {p!r}: {b[p]} -> {s[p]}")
    return out


def _diff_merges(base: list, snap: list) -> list[str]:
    bset = {(c, tuple(d)) for c, d in base}
    sset = {(c, tuple(d)) for c, d in snap}
    out = []
    for c, d in sorted(bset - sset):
        out.append(f"no-longer-merged {c!r} <- {list(d)}")
    for c, d in sorted(sset - bset):
        out.append(f"newly-merged {c!r} <- {list(d)}")
    return out


def run_layer_c(update_baseline: bool = False) -> list[Result]:
    try:
        with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
            snap = _build_golden_snapshot()
    except Exception as e:
        return [Result("snapshot.build", "logic", "fail", 1, [f"build error: {e}"],
                       note="golden replay failed")]

    if update_baseline:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        return [Result("snapshot.baseline", "logic", "warn", 0,
                       [f"wrote {_BASELINE.name}: {snap['n_players']} players, "
                        f"{snap['n_credited_kills']} kills, {len(snap['merges'])} merges"],
                       note="baseline regenerated")]

    if not _BASELINE.exists():
        return [Result("snapshot.baseline", "logic", "warn", 0, [], skipped=True,
                       note="no baseline yet -- run: db_regression.py --layer C --update-baseline")]

    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    results: list[Result] = []
    count_diffs = [f"{k}: {base.get(k)} -> {snap[k]}"
                   for k in ("n_players", "n_matches", "n_credited_kills") if base.get(k) != snap[k]]
    results.append(Result("snapshot.counts", "logic", "warn", len(count_diffs), count_diffs,
                          note="golden-game aggregate counts drifted -- intended? re-baseline"))
    board_diffs = _diff_board(base.get("board", []), snap["board"])
    results.append(Result("snapshot.board", "logic", "warn", len(board_diffs),
                          board_diffs[:_MAX_EXAMPLES],
                          note="golden-game board (identity/elo) drifted -- intended? re-baseline"))
    merge_diffs = _diff_merges(base.get("merges", []), snap["merges"])
    results.append(Result("snapshot.merges", "logic", "warn", len(merge_diffs),
                          merge_diffs[:_MAX_EXAMPLES],
                          note="golden-game merge decisions drifted -- intended? re-baseline"))
    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class _Skip(Exception):
    """Raised by a check when its precondition (e.g. a migrated column) is absent."""


def run_layer_a(kf_path: Path, elo_path: Path) -> list[Result]:
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


def _tag(r: Result) -> str:
    if r.skipped:
        return "SKIP"
    if r.count == 0:
        return "PASS"
    return {"fail": "FAIL", "gap": "GAP ", "warn": "WARN"}.get(r.severity, "WARN")


def _print_section(title: str, results: list[Result]) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)
    for r in results:
        tag = _tag(r)
        if r.skipped:
            print(f"  SKIP  {r.name:28s}  ({r.note})")
            continue
        print(f"  {tag:4s}  {r.name:28s}  violations={r.count}")
        for ex in r.examples:
            print(f"           - {ex}")
        if tag in ("GAP ", "WARN", "FAIL") and r.note:
            print(f"           ~ {r.note}")


def _summary(results: list[Result]) -> None:
    hard = sum(1 for r in results if not r.skipped and r.severity == "fail" and r.count)
    warn = sum(1 for r in results if not r.skipped and r.severity == "warn" and r.count)
    gap = sum(1 for r in results if not r.skipped and r.severity == "gap" and r.count)
    skip = sum(1 for r in results if r.skipped)
    print("-" * 78)
    print(f"  {hard} hard failure(s), {warn} warning(s), {gap} known gap(s), {skip} skipped.")


def main() -> int:
    ap = argparse.ArgumentParser(description="DB regression harness (Layers A + B + C)")
    ap.add_argument("--layer", choices=["A", "B", "C", "all"], default="all",
                    help="A=structural invariants (needs DBs); B=identity fixtures; "
                         "C=golden drift snapshot; default all")
    ap.add_argument("--db", type=Path, default=KILLFEED_DB_PATH, help="killfeed.db path")
    ap.add_argument("--elo-db", type=Path, default=ELO_DB_PATH, help="elo.db path")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on-warn", action="store_true", help="treat WARN as failure (exit 1)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="Layer C: regenerate the golden drift baseline (deliberate re-baseline)")
    args = ap.parse_args()

    results: list[Result] = []
    if args.layer in ("A", "all"):
        a = run_layer_a(args.db, args.elo_db)
        results += a
        if not args.json:
            _print_section("DB REGRESSION  --  Layer A: structural invariants", a)
    if args.layer in ("B", "all"):
        b = run_layer_b()
        results += b
        if not args.json:
            _print_section("DB REGRESSION  --  Layer B: labeled identity regression", b)
    if args.layer in ("C", "all"):
        c = run_layer_c(update_baseline=args.update_baseline)
        results += c
        if not args.json:
            _print_section("DB REGRESSION  --  Layer C: golden drift snapshot", c)

    if args.json:
        print(json.dumps([{
            "name": r.name, "requires": r.requires, "severity": r.severity,
            "count": r.count, "skipped": r.skipped, "note": r.note, "examples": r.examples,
        } for r in results], indent=2))
    else:
        _summary(results)

    hard_fail = any(not r.skipped and r.severity == "fail" and r.count for r in results)
    warn_hit = any(not r.skipped and r.severity == "warn" and r.count for r in results)
    return 1 if (hard_fail or (args.fail_on_warn and warn_hit)) else 0


if __name__ == "__main__":
    sys.exit(main())
