"""Measure the Layer-2 sticky-cap lever on run2 (seg5, 2026-07-09) and size residual name-jitter.

Insight (verified): db_log suppression is INSERT-time. killfeed.db rows are the survivors of a
live ELO cap of STICKY_CHAIN_MAX_ROWS=4. Because suppression only ever trims the tail of a chain
(suppressed reads were never inserted), replaying survivors at a LOWER cap faithfully simulates
what a lower live cap would have kept. So we can size the lever without a new live run.

This script:
  1. Reproduces the current inflation picture on seg5 ELO rows (Kill + both-name BleedOut).
  2. Simulates lowering the ELO chain cap (4 -> 3,2,1) and the merge_types flag, reporting
     ELO survivors + rows removed for each -> sizes which knob touches the 54% pure-sticky.
  3. Sizes residual name-jitter: rows that survive a tight (0.82) cap but collapse under a
     loose (0.65) grouping = same elimination, spellings too far apart to chain.

Read-only (no writes/deletes). Run: .venv/Scripts/python.exe scratch/measure_sticky_lever.py
"""
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import datetime

DB = r"g:\PycharmProjects\TesseractApexOCR\killfeed.db"
RUN2_START = datetime(2026, 7, 9, 15, 0, 0).timestamp()   # seg5 boundary (gap-detected)

GAP = 150            # STICKY_CHAIN_GAP_SECONDS
SIM = 0.82           # STICKY_SIM_THRESHOLD (matches db_log live)
import re
_NORM = re.compile(r"[^a-z0-9]")
def sig(a, v):
    return _NORM.sub("", (a or "").lower()) + "|" + _NORM.sub("", (v or "").lower())

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ELO-eligible rows exactly as detect_matches_from_db selects them, restricted to run2.
rows = conn.execute(
    "SELECT id, streamer, event_type, attacker, victim, created_at, timestamp, canonical "
    "FROM events WHERE (event_type='Kill' OR (event_type='BleedOut' AND attacker!='' AND victim!='')) "
    "AND source IN ('trocr','easyocr') AND created_at >= ? ORDER BY created_at, id",
    (RUN2_START,)).fetchall()
print(f"run2 ELO-eligible rows: {len(rows)}")
n_empty = sum(1 for r in rows if not (r["attacker"] and r["victim"]))
print(f"  of which missing attacker OR victim (never sticky-suppressed live): {n_empty} "
      f"({100*n_empty/len(rows):.1f}%)")


def simulate(rows, cap, merge_types, sim=SIM):
    """Replay insert-time chain suppression. Returns (n_kept, deleted_ids, chain_log).
    merge_types: chain key drops event_type (Layer-2 flag). cap applies to ELO branch;
    here ALL rows are ELO-eligible so cap applies to every chain."""
    groups = defaultdict(list)          # key -> list of live chains
    kept, deleted = [], []
    chains_all = []
    for r in rows:
        # Faithful to db_log.insert_event: the sticky-chain block only runs `if attacker and
        # victim`. Rows missing either side are ALWAYS inserted (never chained/suppressed).
        if not (r["attacker"] and r["victim"]):
            kept.append(r["id"])
            continue
        key = (r["streamer"],) if merge_types else (r["streamer"], r["event_type"])
        ts = r["created_at"]
        s = sig(r["attacker"], r["victim"])
        chains = groups[key]
        chains[:] = [c for c in chains if ts - c["last_ts"] < GAP]
        best, best_ratio = None, 0.0
        for c in chains:
            ratio = SequenceMatcher(None, s, c["sig"]).ratio()
            if ratio >= sim and ratio > best_ratio:
                best, best_ratio = c, ratio
        if best is None:
            c = {"sig": s, "last_ts": ts, "start": ts, "len": 1,
                 "kept": [r["id"]], "deleted": [], "rows": [r]}
            chains.append(c); chains_all.append(c)
            kept.append(r["id"])
        else:
            best["len"] += 1
            best["last_ts"] = ts
            best["rows"].append(r)
            if best["len"] > cap:
                deleted.append(r["id"]); best["deleted"].append(r["id"])
            else:
                kept.append(r["id"]); best["kept"].append(r["id"])
    return kept, deleted, chains_all


print("\n== Lever sizing: ELO survivors by (cap, merge_types) ==")
print(f"{'cap':>4} {'merge':>6} {'kept':>6} {'deleted':>8} {'%del':>6}")
base_kept = None
for merge in (False, True):
    for cap in (4, 3, 2, 1):
        kept, deleted, _ = simulate(rows, cap, merge)
        pct = 100 * len(deleted) / len(rows)
        tag = "  <- current live baseline" if (cap == 4 and not merge) else ""
        print(f"{cap:>4} {str(merge):>6} {len(kept):>6} {len(deleted):>8} {pct:>5.1f}%{tag}")

# Decompose what a candidate tighter cap removes vs. the current cap=4 baseline.
CAND_CAP = 2
print(f"\n== What lowering ELO cap 4 -> {CAND_CAP} (merge off) removes (Gate-2 over-suppression check) ==")
_, del4, _ = simulate(rows, 4, False)
kept2, del2, chains2 = simulate(rows, CAND_CAP, False)
extra = set(del2) - set(del4)
print(f"cap=4 deletes {len(del4)}, cap={CAND_CAP} deletes {len(del2)}  -> {len(extra)} ADDITIONAL rows removed")

# For each chain that loses additional rows, show intra-chain similarity + span so we can eyeball
# whether the deleted rows are sticky repeats (safe) or distinct kills (over-suppression).
print(f"\nChains hit by the tighter cap (kept first {CAND_CAP}, would delete the rest):")
hit = [c for c in chains2 if c["deleted"]]
hit.sort(key=lambda c: -len(c["deleted"]))
for c in hit[:25]:
    rws = c["rows"]
    span = rws[-1]["created_at"] - rws[0]["created_at"]
    # min pairwise sim between the kept row and each deleted row
    first = sig(rws[0]["attacker"], rws[0]["victim"])
    sims = [SequenceMatcher(None, first, sig(r["attacker"], r["victim"])).ratio() for r in rws[1:]]
    minsim = min(sims) if sims else 1.0
    a0, v0 = rws[0]["attacker"], rws[0]["victim"]
    print(f"  [{rws[0]['streamer']:14s}] {rws[0]['event_type']:8s} '{a0[:16]}->{v0[:16]}' "
          f"len={c['len']} del={len(c['deleted'])} span={span:5.0f}s minsim={minsim:.2f}")

# Residual name-jitter: rows that survive the tight (0.82) cap but are the SAME real elimination
# under a jittered spelling. Principled test (not a blanket loose ratio, which over-merges distinct
# kills that happen to share a streamer): among cap-survivors, within one streamer and within GAP
# seconds, a pair is a jitter-duplicate iff ONE side matches (exact-normalized) and the OTHER side
# is similar but below the 0.82 chain threshold (0.55 <= ratio < 0.82). That is precisely the case
# the sticky-cap CANNOT fix and name-canonicalization would.
print(f"\n== Residual name-jitter among cap={CAND_CAP} survivors (one side exact, other jittered <0.82) ==")
survivor_rows = [r for r in rows if r["id"] in set(kept2) and r["attacker"] and r["victim"]]
by_streamer = defaultdict(list)
for r in survivor_rows:
    by_streamer[r["streamer"]].append(r)
jitter_pairs = 0
examples = []
parent = {}                       # union-find over row ids -> distinct excess rows, not pairs
def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]; x = parent[x]
    return x
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb: parent[ra] = rb
for st, rs in by_streamer.items():
    rs.sort(key=lambda r: r["created_at"])
    for i in range(len(rs)):
        find(rs[i]["id"])
        for j in range(i + 1, len(rs)):
            if rs[j]["created_at"] - rs[i]["created_at"] > GAP:
                break
            ai = _NORM.sub("", rs[i]["attacker"].lower()); vi = _NORM.sub("", rs[i]["victim"].lower())
            aj = _NORM.sub("", rs[j]["attacker"].lower()); vj = _NORM.sub("", rs[j]["victim"].lower())
            a_r = SequenceMatcher(None, ai, aj).ratio()
            v_r = SequenceMatcher(None, vi, vj).ratio()
            # one side identical, the other jittered (similar but under chain threshold)
            att_match = (ai == aj and 0.55 <= v_r < SIM)
            vic_match = (vi == vj and 0.55 <= a_r < SIM)
            if att_match or vic_match:
                jitter_pairs += 1
                union(rs[i]["id"], rs[j]["id"])
                if len(examples) < 20:
                    examples.append((st, rs[i]["attacker"], rs[i]["victim"],
                                     rs[j]["attacker"], rs[j]["victim"]))
# Distinct excess rows = sum(component_size - 1) over components with >1 member. This is the
# real inflation (a 3-read jitter burst = 3 pairs but only 2 excess rows).
from collections import Counter
comp = Counter(find(r["id"]) for r in survivor_rows)
excess_rows = sum(sz - 1 for sz in comp.values() if sz > 1)
n_clusters = sum(1 for sz in comp.values() if sz > 1)
print(f"name-complete survivors: {len(survivor_rows)}")
print(f"jitter-duplicate pairs: {jitter_pairs}  ->  {n_clusters} distinct eliminations affected, "
      f"{excess_rows} EXCESS rows ({100*excess_rows/max(len(survivor_rows),1):.1f}% of survivors)")
for st, a1, v1, a2, v2 in examples:
    print(f"  [{st:14s}] '{a1}->{v1}'  ~=  '{a2}->{v2}'")
