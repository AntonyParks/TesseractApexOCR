"""Periodically rebuild elo.db from killfeed.db while the live capture pipeline runs.

Hybrid cadence (user choice 2026-07-16): a FAST no-dedupe rebuild every --interval seconds keeps the
board fresh (~2 min), and a full CLEAN dedupe rebuild every --clean-every cycles (hourly by default)
re-clusters identities (~6 min). To avoid the board flip-flopping between merged and split between the
hourly clean runs, the clean run CACHES its dedupe cluster-map (the expensive O(n^2) part) to JSON,
and every fast run cheaply RE-APPLIES that cached map -- so every rebuild is merged, and only the
discovery of NEW merges waits for the next clean cycle.

Each rebuild is written to a temp DB and then ATOMICALLY swapped into place, so the API/viewer never
see an empty elo.db mid-rebuild (closes the 6uj serving-gap for the auto path).

Run alongside the pipeline:  python elo_autorebuild.py
Stop with Ctrl+C. Safe to run when the pipeline is not running (it just rebuilds from whatever
killfeed.db holds).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent
PY = str((REPO / ".venv" / "Scripts" / "python.exe")) if (REPO / ".venv" / "Scripts" / "python.exe").exists() else sys.executable


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def base_rebuild(temp_db: Path, killfeed_db: Path) -> None:
    """Full clean-slate ELO rebuild (NO dedupe) into temp_db, as a fresh subprocess."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    r = subprocess.run(
        [PY, "-u", "reprocess.py", "--db", str(temp_db), "--db-log", str(killfeed_db)],
        cwd=str(REPO), env=env, capture_output=True, text=True, errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"base rebuild failed (exit {r.returncode}):\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")


def clean_and_cache(temp_db: Path, map_path: Path) -> int:
    """Run the full dedupe on temp_db (applies merges) and cache the cluster-map to map_path.
    Returns the number of merge clusters."""
    from reprocess import deduplicate_players
    merges = deduplicate_players(temp_db, dry_run=False)      # clusters (O(n^2)) + applies merges
    cache = {canonical: dups for (canonical, dups, _stats) in merges}
    tmp = map_path.with_suffix(map_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, map_path)
    return len(cache)


def reapply_cached(temp_db: Path, map_path: Path) -> int:
    """Cheaply re-apply the cached dedupe cluster-map to temp_db (no O(n^2) clustering).
    Recomputes merged stats from temp_db's CURRENT rows so only present names are merged."""
    from elo_db import merge_player, get_conn
    if not map_path.exists():
        return 0
    cache = json.loads(map_path.read_text(encoding="utf-8"))
    conn = get_conn(temp_db)
    allr = {r["player"]: dict(r) for r in conn.execute(
        "SELECT player, total_kills, total_deaths, matches_played, peak_elo FROM player_ratings")}
    conn.close()
    applied = 0
    for canonical, dups in cache.items():
        if canonical not in allr:
            continue                                   # canonical absent this rebuild -> skip cluster
        present = [d for d in dups if d in allr]
        if not present:
            continue
        members = [canonical] + present
        merge_player(
            canonical=canonical,
            duplicates=present,
            merged_kills=sum(allr[m]["total_kills"] for m in members),
            merged_deaths=sum(allr[m]["total_deaths"] for m in members),
            merged_matches=sum(allr[m]["matches_played"] for m in members),
            merged_peak_elo=max(allr[m]["peak_elo"] for m in members),
            path=temp_db,
        )
        applied += 1
    return applied


def atomic_swap(temp_db: Path, dst_db: Path, timeout_s: float = 60.0) -> None:
    """Checkpoint temp_db and atomically replace dst_db with it, tolerating a transient reader lock.

    On Windows a SQLite reader (e.g. the gui polling /rankings ~1x/sec) holds dst open in short
    bursts, and os.replace fails if EITHER file is open. We retry at a SUB-SECOND interval so an
    attempt lands in the gap between the reader's bursts, and gc.collect() first to finalize any of
    our own connection objects still pending release."""
    import gc
    c = sqlite3.connect(str(temp_db))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.commit()
    c.close()
    gc.collect()                                      # finalize any lingering temp connections
    deadline = time.time() + timeout_s
    while True:
        try:
            os.replace(temp_db, dst_db)               # atomic within the same directory
            break
        except PermissionError:
            if time.time() >= deadline:
                raise RuntimeError(f"could not swap into {dst_db.name} (locked for {timeout_s:.0f}s)")
            time.sleep(0.2)                           # land between the reader's ~1s open bursts
    # Remove now-stale sidecars so no reader pairs the new main file with an old WAL.
    for side in (f"{dst_db}-wal", f"{dst_db}-shm", f"{temp_db}-wal", f"{temp_db}-shm"):
        try:
            os.remove(side)
        except OSError:
            pass


def guard_check(killfeed: Path, elo_dst: Path) -> None:
    """Warn-only DB regression check (Layer A structural invariants on the freshly-swapped DBs +
    Layer B identity fixtures on the deployed code). Never raises; logs a one-line summary plus any
    hard regressions and the known-gap count. Because this loop runs continuously alongside capture,
    a corrupt rebuild OR a code change that regressed identity resolution surfaces in the operator's
    log within a cycle, not days later on the board. (Beads owns core.hooksPath, so continuous
    guarding here is the beads-safe substitute for a pre-commit gate.)"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools" / "golden"))
        from db_regression import run_layer_a, run_layer_b
        results = run_layer_a(killfeed, elo_dst) + run_layer_b()
        hard = [r.name for r in results if not r.skipped and r.severity == "fail" and r.count]
        gaps = sum(1 for r in results if not r.skipped and r.severity == "gap" and r.count)
        if hard:
            log(f"cycle guard: {len(hard)} REGRESSION(S): {', '.join(hard)}  ({gaps} known gap(s))")
        else:
            log(f"cycle guard: all invariants + identity fixtures pass ({gaps} known gap(s))")
    except Exception as e:
        log(f"cycle guard: check skipped ({e})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Periodically rebuild elo.db while capturing.")
    ap.add_argument("--interval", type=int, default=600, help="target seconds between rebuild starts (default 600)")
    ap.add_argument("--clean-every", type=int, default=6, help="every Nth cycle is a full dedupe rebuild (default 6 ~= hourly at 10min interval)")
    ap.add_argument("--db", default="elo.db", help="output ELO db (default elo.db)")
    ap.add_argument("--db-log", default="killfeed.db", help="source killfeed db (default killfeed.db)")
    ap.add_argument("--map", default="elo_dedupe_map.json", help="cached dedupe cluster-map path")
    ap.add_argument("--once", action="store_true", help="run a single clean cycle and exit (for testing)")
    args = ap.parse_args()

    dst = (REPO / args.db).resolve()
    killfeed = (REPO / args.db_log).resolve()
    map_path = (REPO / args.map).resolve()
    temp = dst.with_name(dst.name + ".rebuilding")

    log(f"auto-rebuild starting: interval={args.interval}s clean-every={args.clean_every} db={dst.name}")
    cycle = 0
    while True:
        t0 = time.time()
        cycle += 1
        is_clean = (cycle == 1) or (cycle % args.clean_every == 0) or not map_path.exists()
        kind = "CLEAN (dedupe)" if is_clean else "fast (cached-merge)"
        log(f"cycle {cycle} [{kind}] rebuilding...")
        try:
            for side in (temp, Path(f"{temp}-wal"), Path(f"{temp}-shm")):
                try:
                    os.remove(side)
                except OSError:
                    pass
            base_rebuild(temp, killfeed)
            if is_clean:
                n = clean_and_cache(temp, map_path)
                log(f"cycle {cycle}: dedupe applied {n} clusters (cached to {map_path.name})")
            else:
                n = reapply_cached(temp, map_path)
                log(f"cycle {cycle}: re-applied {n} cached clusters")
            atomic_swap(temp, dst)
            dt = time.time() - t0
            log(f"cycle {cycle}: swapped into {dst.name} ({dt:.0f}s)")
            guard_check(killfeed, dst)   # warn-only invariant check on the freshly-swapped DBs
        except Exception as e:
            log(f"cycle {cycle} FAILED: {e}")

        if args.once:
            return 0
        dt = time.time() - t0
        sleep_s = max(5.0, args.interval - dt)
        log(f"cycle {cycle}: next in {sleep_s:.0f}s")
        try:
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("stopped."); return 0


if __name__ == "__main__":
    sys.exit(main())
