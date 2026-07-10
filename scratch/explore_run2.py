"""Orientation: find the run2 window via created_at gaps and count ELO rows.
Read-only. Run: .venv/Scripts/python.exe scratch/explore_run2.py
"""
import sqlite3
from datetime import datetime

DB = r"g:\PycharmProjects\TesseractApexOCR\killfeed.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"total events: {total}")

# created_at range
row = conn.execute("SELECT MIN(created_at) mn, MAX(created_at) mx FROM events").fetchone()
print(f"created_at: {datetime.fromtimestamp(row['mn'])}  ->  {datetime.fromtimestamp(row['mx'])}")

# Find large gaps in created_at (session boundaries). List gaps > 30 min.
rows = conn.execute("SELECT created_at FROM events ORDER BY created_at").fetchall()
cas = [r["created_at"] for r in rows]
print("\nGaps > 30 min in created_at (session boundaries):")
prev = cas[0]
seg_start = cas[0]
segments = []
for c in cas[1:]:
    if c - prev > 1800:
        segments.append((seg_start, prev))
        print(f"  gap {(c-prev)/60:6.1f} min  after {datetime.fromtimestamp(prev)}")
        seg_start = c
    prev = c
segments.append((seg_start, prev))

print(f"\n{len(segments)} session segment(s):")
for i, (s, e) in enumerate(segments):
    n = conn.execute("SELECT COUNT(*) FROM events WHERE created_at>=? AND created_at<=?", (s, e)).fetchone()[0]
    print(f"  seg{i}: {datetime.fromtimestamp(s)} -> {datetime.fromtimestamp(e)}  ({(e-s)/60:.0f} min, {n} rows)")

# ELO rows overall and per segment
print("\nELO-eligible rows (Kill, or BleedOut w/ both names, source trocr/easyocr):")
def elo_count(where, params):
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE (event_type='Kill' OR "
        "(event_type='BleedOut' AND attacker!='' AND victim!='')) "
        "AND source IN ('trocr','easyocr') AND " + where, params).fetchone()[0]
print(f"  all-time ELO rows: {elo_count('1=1', ())}")
for i, (s, e) in enumerate(segments):
    print(f"  seg{i}: {elo_count('created_at>=? AND created_at<=?', (s, e))}")

# event_type distribution in last segment
last_s, last_e = segments[-1]
print(f"\nevent_type dist in LAST segment (created_at {datetime.fromtimestamp(last_s)}+):")
for r in conn.execute(
    "SELECT event_type, COUNT(*) n FROM events WHERE created_at>=? GROUP BY event_type ORDER BY n DESC",
    (last_s,)):
    print(f"  {r['event_type']:16s} {r['n']}")
