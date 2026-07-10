"""Second retro pass: fuzzy sticky-chain cleanup for legacy rows where OCR garbling made
each re-read of the same sticky line a DIFFERENT (attacker, victim) tuple, evading the
exact-tuple pass. Chains rows of the same (streamer, event_type) whose 'attacker|victim'
string is >=0.75 similar to the chain's most recent row, with gaps < 150s. Keeps the
first 4 rows per chain, deletes the rest -- same semantics as the exact pass, just with
the same fuzzy matching threshold ocr.py's find_recent_match uses (0.75).

Usage: python retro_fuzzy_sticky_cleanup.py [--execute]
"""
import sqlite3, sys
from collections import defaultdict
from difflib import SequenceMatcher

GAP = 150
MAX_ROWS = 4
SIM = 0.75
DB = r"g:\PycharmProjects\TesseractApexOCR\killfeed.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, streamer, event_type, attacker, victim, created_at FROM events "
    "WHERE attacker != '' AND victim != '' ORDER BY created_at, id"
).fetchall()

groups = defaultdict(list)
for r in rows:
    groups[(r["streamer"], r["event_type"])].append(r)

to_delete = []
chain_report = []
for gkey, grs in groups.items():
    chains = []  # each: dict(last_ts, last_name, count, first_name, deleted)
    for r in grs:
        ts = r["created_at"]
        name = f'{r["attacker"]}|{r["victim"]}'.lower()
        chains = [c for c in chains if ts - c["last_ts"] < GAP]
        best, best_ratio = None, 0.0
        for c in chains:
            ratio = SequenceMatcher(None, name, c["last_name"]).ratio()
            if ratio >= SIM and ratio > best_ratio:
                best, best_ratio = c, ratio
        if best is None:
            chains.append({"last_ts": ts, "last_name": name, "count": 1,
                           "first_name": name, "deleted": 0, "gkey": gkey})
            continue
        best["count"] += 1
        best["last_ts"] = ts
        best["last_name"] = name
        if best["count"] > MAX_ROWS:
            to_delete.append(r["id"])
            best["deleted"] += 1
            if best["deleted"] == 1:
                chain_report.append(best)

total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"total events:   {total}")
print(f"rows to delete: {len(to_delete)}")
print(f"chains hit:     {len(chain_report)}")
print()
print("top 15 chains by deletions:")
for c in sorted(chain_report, key=lambda c: -c["deleted"])[:15]:
    s, e = c["gkey"]
    print(f'  {c["deleted"]:4d} del (kept {min(c["count"], MAX_ROWS)}) [{s}] {e}: {c["first_name"][:60]}')

if "--execute" in sys.argv:
    conn.executemany("DELETE FROM events WHERE id=?", [(i,) for i in to_delete])
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"\nDELETED. {total} -> {remaining} events.")
else:
    print("\n(preview only -- rerun with --execute to delete)")
