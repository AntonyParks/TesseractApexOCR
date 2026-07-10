"""Unit test for SAME_VICTIM_GUARD (stem-aware multikill guard) in db_log.
Run: .venv/Scripts/python.exe scratch/test_same_victim_guard.py
"""
import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_log
import config

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    PASS += cond; FAIL += (not cond)

def fresh():
    p = Path(tempfile.mkdtemp()) / "g.db"; db_log.init_db(p); db_log._chain_state.clear(); return p

def ins(db, v, a="bornlimitless", et="Kill"):
    return db_log.insert_event(streamer="ZZ", timestamp="2026-07-09 15:00:00", raw_text="r",
        canonical="c", event_type=et, attacker=a, victim=v, db_path=db)

# ---- helper contract ----
print("== _distinct_default_name_victim ==")
D = db_log._distinct_default_name_victim
check("same stem, different digits -> distinct (multikill)", D("gibraltar1619", ["gibraltar2127"]))
check("same stem, garbled same-ish digits -> NOT distinct",  not D("gibraltar2l27", ["gibraltar2127"]))
check("fuzzy-matches a kept victim -> NOT distinct",          not D("gibraltar2127", ["gibraltar2127"]))
check("no numeric suffix -> NOT distinct (can't tell from garble)", not D("sirkonsti", ["konsti"]))
check("truncation garble (no digits) -> NOT distinct",       not D("vesoson", ["ofpsvesoson"]))

# Jittered victims: all fuzzy-chain to the first read but are distinct tuples (dodging the 20s exact
# dedup). "same victim" = garbles of gibraltar2127; "multikill" = clearly different default numbers.
same_victim = ["gibraltar2127", "gibraltar2l27", "gibraltar212z", "gibraltar2127x", "gibraltarz2127"]
multikill   = ["gibraltar2127", "gibraltar2l27", "gibraltar1619", "gibraltar3044"]

print("== guard OFF (baseline cap=2) ==")
config.SAME_VICTIM_GUARD = False
db = fresh(); ids = [ins(db, v) for v in multikill]
check("multikill reads collapse to cap=2 rows (real kills lost)", len(set(ids)) == 2)

print("== guard ON ==")
config.SAME_VICTIM_GUARD = True
db2 = fresh(); ids2 = [ins(db2, v) for v in multikill]
check("multikill: distinct-number victims kept (>2 rows)", len(set(ids2)) == 4)
db3 = fresh(); ids3 = [ins(db3, v) for v in same_victim]
check("sticky same victim: still suppressed to cap=2", len(set(ids3)) == 2)
config.SAME_VICTIM_GUARD = False  # restore

print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS} passed)")
sys.exit(1 if FAIL else 0)
