"""Bootstrap ELO ratings from existing killfeed_log.csv data.

Usage:
    python reprocess.py                # process all data → elo.db
    python reprocess.py --dry-run      # detect matches only, no DB writes
    python reprocess.py --reset        # wipe elo.db and reprocess from scratch
    python reprocess.py --gap 400      # use 400s gap threshold (default 300)
    python reprocess.py --min-kills 5  # require at least 5 kills per match
    python reprocess.py --dedupe       # merge near-duplicate player names after ELO
    python reprocess.py --dedupe --dry-run  # preview merges without writing
"""

import argparse
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from config import LOG_PATH
from elo_db import ELO_DB_PATH, drop_db, get_all_player_ratings, init_db, merge_player
from elo_engine import batch_reprocess
from match_detector import GAP_SECONDS, MIN_KILLS, detect_matches, get_player_survival

_DEDUPE_RATIO = 0.75   # SequenceMatcher threshold for considering two names the same
_DEDUPE_PREFIX = 3     # names must share this many leading chars to be considered
_DEDUPE_MIN_LEN = 6    # only deduplicate names this long or longer (short names too ambiguous)


def deduplicate_players(db_path: Path, dry_run: bool = False) -> list[tuple]:
    """Cluster near-duplicate player names and merge them.

    Returns list of (canonical, duplicates, merged_stats) for each merge performed.
    """
    players = get_all_player_ratings(db_path)
    if not players:
        return []

    # Build clusters: greedy — assign each name to the first cluster it's similar to
    clusters: list[list[dict]] = []
    for p in players:
        name = p["player"].lower()
        placed = False
        # Skip deduplication for short names — too ambiguous at this threshold
        if len(name) >= _DEDUPE_MIN_LEN:
            for cluster in clusters:
                rep = cluster[0]["player"].lower()
                if len(rep) < _DEDUPE_MIN_LEN:
                    continue
                # Must share first N chars AND meet similarity threshold
                if (name[:_DEDUPE_PREFIX] == rep[:_DEDUPE_PREFIX] and
                        SequenceMatcher(None, name, rep).ratio() >= _DEDUPE_RATIO):
                    cluster.append(p)
                    placed = True
                    break
        if not placed:
            clusters.append([p])

    # Also handle "base + noise suffix" pattern: "baby" → "baby svoo", "baby 5jzd"
    # Find single-word names that are a prefix of multi-word names
    single_names = {p["player"].lower(): p for p in players if " " not in p["player"]}
    multi_names = [p for p in players if " " in p["player"]]
    for mp in multi_names:
        base = mp["player"].split()[0].lower()
        if base in single_names and len(base) >= 4:
            base_player = single_names[base]
            # Check if they're already in the same cluster
            already_merged = False
            for cluster in clusters:
                names_in_cluster = [c["player"].lower() for c in cluster]
                if base in names_in_cluster and mp["player"].lower() in names_in_cluster:
                    already_merged = True
                    break
            if not already_merged:
                # Add to base player's cluster
                for cluster in clusters:
                    if cluster[0]["player"].lower() == base:
                        cluster.append(mp)
                        break

    merges = []
    for cluster in clusters:
        if len(cluster) <= 1:
            continue

        # Canonical = most matches played; ties broken by longest name
        canonical_row = max(cluster, key=lambda r: (r["matches_played"], len(r["player"])))
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
    parser = argparse.ArgumentParser(description="Reprocess killfeed CSV into ELO ratings")
    parser.add_argument("--dry-run", action="store_true", help="Detect matches only, no DB writes")
    parser.add_argument("--reset", action="store_true", help="Wipe elo.db before reprocessing")
    parser.add_argument("--gap", type=int, default=GAP_SECONDS, help="Gap seconds between matches")
    parser.add_argument("--min-kills", type=int, default=MIN_KILLS, help="Min kills per match")
    parser.add_argument("--csv", type=Path, default=LOG_PATH, help="Path to killfeed CSV")
    parser.add_argument("--db", type=Path, default=ELO_DB_PATH, help="Path to elo.db")
    parser.add_argument("--dedupe", action="store_true",
                        help="Merge near-duplicate player names after ELO processing")
    args = parser.parse_args()

    # --dedupe-only: skip reprocessing, just run deduplication on existing DB
    if args.dedupe and not args.csv.exists():
        print("CSV not found — running deduplication on existing DB only...")
        _run_dedupe(args.db, args.dry_run)
        return

    if not args.csv.exists():
        print(f"ERROR: CSV not found at {args.csv}")
        return

    print(f"Detecting matches from {args.csv} (gap={args.gap}s, min_kills={args.min_kills})...")
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

    if args.reset:
        print("  Resetting elo.db...")
        drop_db(args.db)
    else:
        init_db(args.db)

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
