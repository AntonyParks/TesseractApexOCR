"""Bootstrap ELO ratings from existing killfeed_log.csv data.

A full reprocess reads ALL events from killfeed.db and replays every match, so it is only
correct when it starts from an empty elo.db. Writing onto a non-empty DB double-counts
(compounds matches_played, re-applies ELO passes). Therefore the default now ALWAYS rebuilds
from a clean slate; --append opts back into the legacy stacking behavior for anyone who wants it.

Usage:
    python reprocess.py                # rebuild elo.db from scratch (always clean — default)
    python reprocess.py --dry-run      # detect matches only, no DB writes
    python reprocess.py --append       # legacy: stack onto existing elo.db (COMPOUNDS ratings)
    python reprocess.py --gap 400      # use 400s gap threshold (default 300)
    python reprocess.py --min-kills 5  # require at least 5 kills per match
    python reprocess.py --dedupe       # merge near-duplicate player names after ELO
    python reprocess.py --dedupe --dry-run  # preview merges without writing
"""

import argparse
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from config import LOG_PATH, KILLFEED_DB_PATH
from elo_db import ELO_DB_PATH, drop_db, get_all_player_ratings, init_db, merge_player
from elo_engine import batch_reprocess
from match_detector import GAP_SECONDS, MIN_KILLS, detect_matches, detect_matches_from_db, get_player_survival

_DEDUPE_PREFIX = 3     # names must share this many leading chars for standard similarity threshold
_DEDUPE_MIN_LEN = 4    # lowered from 6 to 4 to support short player names


def deduplicate_players(db_path: Path, dry_run: bool = False) -> list[tuple]:
    """Cluster near-duplicate player names using Connected Components (transitive) clustering."""
    players = get_all_player_ratings(db_path)
    if not players:
        return []

    # Build adjacency list (graph) where nodes are player names
    # Two nodes have an edge if they satisfy the hybrid similarity check
    adj = {p["player"]: set() for p in players}
    n_players = len(players)

    for i in range(n_players):
        p1 = players[i]
        name1 = p1["player"].lower()
        if len(name1) < _DEDUPE_MIN_LEN:
            continue
            
        for j in range(i + 1, n_players):
            p2 = players[j]
            name2 = p2["player"].lower()
            if len(name2) < _DEDUPE_MIN_LEN:
                continue

            ratio = SequenceMatcher(None, name1, name2).ratio()
            
            # Hybrid comparison:
            # 1. Share first 3 characters: require similarity >= 0.70
            # 2. Different prefix: require similarity >= 0.82 (very strict for minor spelling typos)
            if name1[:_DEDUPE_PREFIX] == name2[:_DEDUPE_PREFIX]:
                is_similar = (ratio >= 0.70)
            else:
                is_similar = (ratio >= 0.82)

            if is_similar:
                adj[p1["player"]].add(p2["player"])
                adj[p2["player"]].add(p1["player"])

    # Also handle "base + noise suffix" pattern: "baby" -> "baby svoo"
    single_names = {p["player"].lower(): p["player"] for p in players if " " not in p["player"]}
    multi_names = [p["player"] for p in players if " " in p["player"]]
    for mp in multi_names:
        base = mp.split()[0].lower()
        if base in single_names and len(base) >= 4:
            orig_base_name = single_names[base]
            adj[orig_base_name].add(mp)
            adj[mp].add(orig_base_name)

    # Traverse graph using BFS to find connected components (clusters)
    visited = set()
    clusters: list[list[dict]] = []
    player_by_name = {p["player"]: p for p in players}

    for p in players:
        name = p["player"]
        if name in visited:
            continue
            
        component = []
        queue = [name]
        visited.add(name)
        
        while queue:
            curr = queue.pop(0)
            component.append(player_by_name[curr])
            for neighbor in adj[curr]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
                    
        clusters.append(component)

    merges = []
    for cluster in clusters:
        if len(cluster) <= 1:
            continue

        # Canonical selection: prioritize mixed casing (CamelCase / mixed upper-lower)
        # over raw lowercase OCR typos, then break ties by matches_played and length.
        def get_canonical_score(r: dict) -> tuple:
            p_name = r["player"]
            # Has both upper and lower case letters
            has_upper = any(c.isupper() for c in p_name)
            has_lower = any(c.islower() for c in p_name)
            mixed_case_bonus = 1 if (has_upper and has_lower) else 0
            return (mixed_case_bonus, r["matches_played"], len(p_name))

        canonical_row = max(cluster, key=get_canonical_score)
        canonical = canonical_row["player"]
        duplicates = [r["player"] for r in cluster if r["player"] != canonical]

        merged_kills   = sum(r["total_kills"]   for r in cluster)
        merged_deaths  = sum(r["total_deaths"]  for r in cluster)
        merged_matches = sum(r["matches_played"] for r in cluster)
        merged_peak    = max(r["peak_elo"]       for r in cluster)

        merges.append((canonical, duplicates, {
            "total_kills": merged_kills,
            "total_deaths": merged_deaths,
            "matches_played": merged_matches,
            "peak_elo": merged_peak,
        }))

        if not dry_run:
            merge_player(
                canonical=canonical,
                duplicates=duplicates,
                merged_kills=merged_kills,
                merged_deaths=merged_deaths,
                merged_matches=merged_matches,
                merged_peak_elo=merged_peak,
                path=db_path,
            )

    return merges


def main():
    parser = argparse.ArgumentParser(description="Reprocess killfeed data into ELO ratings")
    parser.add_argument("--dry-run",  action="store_true", help="Detect matches only, no DB writes")
    parser.add_argument("--reset",    action="store_true",
                        help="(now the default; kept for compatibility) wipe elo.db before reprocessing")
    parser.add_argument("--append",   action="store_true",
                        help="Legacy: stack onto an existing elo.db instead of rebuilding — this "
                             "COMPOUNDS matches_played and re-applies ELO passes. Almost never correct.")
    parser.add_argument("--gap",      type=int, default=GAP_SECONDS, help="Gap seconds between matches")
    parser.add_argument("--min-kills",type=int, default=MIN_KILLS,   help="Min kills per match")
    parser.add_argument("--csv",      type=Path, default=LOG_PATH,   help="Path to killfeed CSV (legacy)")
    parser.add_argument("--db-log",   type=Path, default=KILLFEED_DB_PATH, help="Path to killfeed.db")
    parser.add_argument("--db",       type=Path, default=ELO_DB_PATH,     help="Path to elo.db")
    parser.add_argument("--dedupe",   action="store_true",
                        help="Merge near-duplicate player names after ELO processing")
    parser.add_argument("--force-csv",action="store_true", help="Force CSV source even if DB exists")
    args = parser.parse_args()

    # NOTE (bead TesseractApexOCR-6uj): a clean rebuild drop_db()s then repopulates elo.db IN PLACE,
    # so api.py briefly serves an empty/partial leaderboard during the rebuild. An atomic temp-DB +
    # os.replace() swap was attempted but elo.db runs in WAL mode and a lingering connection keeps
    # elo.db.rebuild-wal locked at swap time. The correct fix is to build in a child process that
    # fully exits (guaranteeing all SQLite handles close) and os.replace() from the parent -- deferred.
    # Reverted to the original in-place rebuild for now.

    # Choose data source: SQLite preferred, CSV fallback
    use_db = args.db_log.exists() and not args.force_csv

    if args.dedupe and not use_db and not args.csv.exists():
        print("No data source found — running deduplication on existing ELO DB only...")
        _run_dedupe(args.db, args.dry_run)
        return

    if use_db:
        print(f"Detecting matches from {args.db_log} (SQLite, gap={args.gap}s, min_kills={args.min_kills})...")
        matches = detect_matches_from_db(args.db_log, gap_seconds=args.gap, min_kills=args.min_kills)
    else:
        if not args.csv.exists():
            print(f"ERROR: Neither {args.db_log} nor {args.csv} found.")
            return
        print(f"Detecting matches from {args.csv} (CSV, gap={args.gap}s, min_kills={args.min_kills})...")
        matches = detect_matches(args.csv, gap_seconds=args.gap, min_kills=args.min_kills)
    print(f"  -> {len(matches)} matches detected\n")

    if not matches:
        print("No matches found. Check your CSV and gap/min-kills settings.")
        return

    # Print match summary
    streamer_counts: Counter = Counter(m.streamer for m in matches)
    print("Matches per streamer:")
    for streamer, count in sorted(streamer_counts.items(), key=lambda x: -x[1]):
        print(f"  {streamer:20s} {count:4d} matches")

    total_kills = sum(m.kill_count for m in matches)
    total_players = sum(m.players_observed for m in matches)
    total_with_placement = sum(
        len(get_player_survival(m)[0]) for m in matches
    )

    print(f"\nTotal kill events in matches:  {total_kills:,}")
    print(f"Total player appearances:      {total_players:,}")
    print(f"Players with definitive elim:  {total_with_placement:,}")

    # Show sample matches
    print("\nSample matches (first 5):")
    for m in matches[:5]:
        elim, _ = get_player_survival(m)
        print(
            f"  {m.match_id:45s} | "
            f"{m.start_time.strftime('%Y-%m-%d %H:%M')} -> {m.end_time.strftime('%H:%M')} | "
            f"{m.kill_count:3d} kills | {len(elim):3d} players with placement"
        )

    if args.dry_run:
        print("\n[DRY RUN] No changes written to DB.")
        if args.dedupe:
            _run_dedupe(args.db, dry_run=True)
        return

    print(f"\nWriting to {args.db}...")

    # Default: rebuild from a clean slate (the only correct behavior, since every run replays
    # ALL matches). --append is the explicit, warned opt-in to the legacy compounding behavior.
    if args.append:
        init_db(args.db)
        if get_all_player_ratings(args.db):
            print("  [WARNING] --append onto a non-empty elo.db: matches_played and ELO will "
                  "COMPOUND on top of existing rows. This is almost never what you want.")
    else:
        print("  Rebuilding elo.db from a clean slate...")
        drop_db(args.db)

    print("  Running ELO engine...")
    ratings = batch_reprocess(matches, db_path=args.db)

    # Summary stats
    elos = sorted(r["elo"] for r in ratings.values())
    rated = [r for r in ratings.values() if r["matches_played"] >= 3]
    rated_elos = sorted(r["elo"] for r in rated)

    print(f"\n=== ELO Summary ===")
    print(f"Total players rated:    {len(ratings):,}")
    print(f"Players (>=3 matches):  {len(rated):,}")

    if elos:
        print(f"ELO range (all):        {min(elos):.0f} – {max(elos):.0f}")
    if rated_elos:
        n = len(rated_elos)
        p25 = rated_elos[n // 4]
        p50 = rated_elos[n // 2]
        p75 = rated_elos[3 * n // 4]
        print(f"ELO percentiles (>=3m): p25={p25:.0f}  p50={p50:.0f}  p75={p75:.0f}")

    print(f"\nTop 20 players by ELO (>=3 matches):")
    top = sorted(rated, key=lambda r: -r["elo"])[:20]
    for i, r in enumerate(top, 1):
        print(
            f"  {i:2d}. {r['player']:25s} ELO={r['elo']:7.1f}  "
            f"matches={r['matches_played']:4d}  "
            f"K={r['total_kills']:4d}  D={r['total_deaths']:4d}"
        )

    if args.dedupe:
        _run_dedupe(args.db, dry_run=False)

    print(f"\nDone. ELO database written to {args.db}")


def _run_dedupe(db_path: Path, dry_run: bool) -> None:
    label = "[DRY RUN] " if dry_run else ""
    print(f"\n{label}=== Deduplication ===")
    merges = deduplicate_players(db_path, dry_run=dry_run)
    if not merges:
        print("No duplicate clusters found.")
        return
    print(f"{label}Merged {len(merges)} cluster(s):")
    for canonical, dupes, stats in merges:
        print(f"  {canonical:25s} <- {', '.join(dupes)}")
        print(f"    combined: matches={stats['matches_played']}  "
              f"K={stats['total_kills']}  D={stats['total_deaths']}")


if __name__ == "__main__":
    main()
