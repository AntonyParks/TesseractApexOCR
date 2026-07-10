"""Retroactive sticky-chain cleanup of killfeed.db.

Mirrors db_log.py's live suppression semantics exactly: per (streamer, event_type,
attacker, victim), walk rows in created_at order; rows within STICKY_CHAIN_GAP_SECONDS
of the previous row continue a chain, otherwise the chain reseeds from the count of
KEPT rows in the previous STICKY_SEED_LOOKBACK_SECONDS. Rows with chain_len >
STICKY_CHAIN_MAX_ROWS would have been suppressed by the live fix -> delete them.

Usage: python retro_sticky_cleanup.py [--execute]   (default is preview only)
"""
import sqlite3, sys
from collections import defaultdict

GAP = 150
MAX_ROWS = 4
LOOKBACK = 600
DB = r"g:\PycharmProjects\TesseractApexOCR\killfeed.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, streamer, event_type, attacker, victim, created_at FROM events "
    "WHERE attacker != '' AND victim != '' ORDER BY created_at, id"
).fetchall()

by_tuple = defaultdict(list)
for r in rows:
    by_tuple[(r["streamer"], r["event_type"], r["attacker"], r["victim"])].append(r)

to_delete = []
per_tuple_deleted = {}
for key, trs in by_tuple.items():
    kept_ts = []
    chain_len = 0
    last_ts = None
    deleted_here = 0
    for r in trs:
        ts = r["created_at"]
        if last_ts is not None and ts - last_ts < GAP:
            chain_len += 1
        else:
            seed = sum(1 for k in kept_ts if k >= ts - LOOKBACK)
            chain_len = seed + 1
        last_ts = ts
        if chain_len > MAX_ROWS:
            to_delete.append(r["id"])
            deleted_here += 1
        else:
            kept_ts.append(ts)
    if deleted_here:
        per_tuple_deleted[key] = (deleted_here, len(trs))

total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"total events:            {total}")
print(f"rows to delete:          {len(to_delete)}")
print(f"tuples affected:         {len(per_tuple_deleted)}")
print()
print("top 15 affected tuples (deleted/total):")
for key, (d, t) in sorted(per_tuple_deleted.items(), key=lambda x: -x[1][0])[:15]:
    s, e, a, v = key
    print(f"  {d:4d}/{t:<4d} [{s}] {e}: {a} -> {v}")

if "--execute" in sys.argv:
    conn.executemany("DELETE FROM events WHERE id=?", [(i,) for i in to_delete])
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"\nDELETED. {total} -> {remaining} events.")
else:
    print("\n(preview only -- rerun with --execute to delete)")
