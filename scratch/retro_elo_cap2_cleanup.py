"""Retro-clean killfeed.db to the NEW ELO sticky cap (STICKY_ELO_CHAIN_MAX_ROWS=2).

The live suppression in db_log.insert_event is INSERT-time, so lowering the ELO cap only affects
future collection -- the existing DB still holds rows a cap of 2 would have suppressed. This pass
retroactively removes them, faithfully mirroring the live chain semantics:
  - population: ELO-eligible rows only (event_type='Kill', or 'BleedOut' with both names) -- exactly
    what detect_matches_from_db feeds ELO. Non-ELO (Knock) rows are untouched (their policy is
    unchanged).
  - guard: rows missing attacker OR victim are never chained/suppressed (`if attacker and victim`).
  - chain key: (streamer, event_type)  [merge_types OFF, the live default]
  - fuzzy sig match at STICKY_SIM_THRESHOLD=0.82, chain gap STICKY_CHAIN_GAP_SECONDS=150s
  - keep the first STICKY_ELO_CHAIN_MAX_ROWS rows of each chain, delete the rest.

Faithful because live suppression only ever trims the chain TAIL (suppressed reads were never
inserted), so the DB rows are the head of each chain and re-capping at a lower value just trims more
tail. Ordered by created_at (wall-clock insert time), matching how the live chain saw them.

Usage:
  python scratch/retro_elo_cap2_cleanup.py            # preview only
  python scratch/retro_elo_cap2_cleanup.py --execute   # backup + DELETE
"""
import re, sqlite3, sys, shutil
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_log

DB = Path(r"g:\PycharmProjects\TesseractApexOCR\killfeed.db")
GAP = db_log.STICKY_CHAIN_GAP_SECONDS      # 150
SIM = db_log.STICKY_SIM_THRESHOLD          # 0.82
CAP = db_log.STICKY_ELO_CHAIN_MAX_ROWS     # 2
_NORM = re.compile(r"[^a-z0-9]")
def sig(a, v):
    return _NORM.sub("", (a or "").lower()) + "|" + _NORM.sub("", (v or "").lower())

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, streamer, event_type, attacker, victim, created_at "
    "FROM events WHERE (event_type='Kill' OR (event_type='BleedOut' AND attacker!='' AND victim!='')) "
    "ORDER BY created_at, id").fetchall()
total_all = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"total events in DB:      {total_all}")
print(f"ELO-eligible rows:       {len(rows)}")
print(f"policy: key=(streamer,event_type)  sim>={SIM}  gap<{GAP}s  keep first {CAP} per chain\n")

groups = defaultdict(list)
to_delete = []
chains_all = []
for r in rows:
    if not (r["attacker"] and r["victim"]):
        continue                            # guard: never chained live
    key = (r["streamer"], r["event_type"])
    ts = r["created_at"]
    s = sig(r["attacker"], r["victim"])
    chains = groups[key]
    chains[:] = [c for c in chains if ts - c["last_ts"] < GAP]
    best, best_ratio = None, 0.0
    for c in chains:
        ratio = SequenceMatcher(None, s, c["sig"]).ratio()
        if ratio >= SIM and ratio > best_ratio:
            best, best_ratio = c, ratio
    if best is None:
        c = {"sig": s, "last_ts": ts, "start": ts, "len": 1,
             "streamer": r["streamer"], "etype": r["event_type"],
             "first": (r["attacker"], r["victim"]), "del": 0}
        chains.append(c); chains_all.append(c)
    else:
        best["len"] += 1
        best["last_ts"] = ts
        if best["len"] > CAP:
            to_delete.append(r["id"]); best["del"] += 1

print(f"rows to delete: {len(to_delete)}  ({100*len(to_delete)/max(len(rows),1):.1f}% of ELO rows, "
      f"{100*len(to_delete)/total_all:.1f}% of all events)")
hit = sorted([c for c in chains_all if c["del"]], key=lambda c: -c["del"])
print(f"chains trimmed: {len(hit)}\n")
print("top 20 chains by deletions (kept first 2):")
for c in hit[:20]:
    a, v = c["first"]
    print(f"  {c['del']:4d} del (len {c['len']}) [{c['streamer']:14s}] {c['etype']:8s} '{a[:18]}->{v[:18]}'")

if "--execute" in sys.argv:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = DB.with_name(f"killfeed.db.bak_pre_elo_cap2_{stamp}")
    shutil.copy2(DB, bak)
    print(f"\nbackup: {bak.name}")
    conn.executemany("DELETE FROM events WHERE id=?", [(i,) for i in to_delete])
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"DELETED {len(to_delete)} rows. {total_all} -> {remaining} events.")
else:
    print("\n(preview only -- rerun with --execute to back up + delete)")
