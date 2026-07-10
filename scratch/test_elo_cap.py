"""Unit test for STICKY_ELO_CHAIN_MAX_ROWS (tighter cap for Kill/BleedOut sticky chains).
Run: .venv/Scripts/python.exe scratch/test_elo_cap.py
"""
import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_log

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    PASS += cond; FAIL += (not cond)

def fresh_db():
    p = Path(tempfile.mkdtemp()) / "cap.db"
    db_log.init_db(p)
    db_log._chain_state.clear()
    return p

def ins(db, et, a="boutwork", v="arcticfox"):
    return db_log.insert_event(streamer="ZZ", timestamp="2026-07-09 15:00:00", raw_text="r",
        canonical="c", event_type=et, attacker=a, victim=v, db_path=db)

print(f"STICKY_ELO_CHAIN_MAX_ROWS = {db_log.STICKY_ELO_CHAIN_MAX_ROWS}")

# ELO type (Kill): first N=cap rows insert (distinct ids), rows past cap are suppressed
# (return a prior id). Sidestep the 20s EXACT-tuple dedup by jittering the victim within the
# 0.82 fuzzy chain so each read is a new row but stays in one sticky chain.
print("== Kill chain suppressed past STICKY_ELO_CHAIN_MAX_ROWS ==")
db = fresh_db()
victims = ["arcticfox20023", "arcticfox2o023", "arcticfox20o23", "articfox20023",
           "arcticf0x20023", "arcticfox2002e"]
ids = [ins(db, "Kill", v=vv) for vv in victims]
cap = db_log.STICKY_ELO_CHAIN_MAX_ROWS
distinct = len(set(ids))
# first `cap` are new rows; the rest collapse onto the last kept id
check(f"first {cap} Kill reads insert distinct rows", len(set(ids[:cap])) == cap)
check("reads past the cap are suppressed (no new rows)", distinct == cap)
check("suppressed reads return the last kept row id", all(i == ids[cap-1] for i in ids[cap:]))

# Non-ELO (Knock) must NOT use the tight ELO cap: a short in-fight burst survives (span logic,
# HARD_CAP=8), so 6 quick knock reads all persist as distinct rows.
print("== Knock chain NOT bound by the ELO cap (short burst survives) ==")
db2 = fresh_db()
kids = [ins(db2, "Knock", v=vv) for vv in victims]
check("6 quick Knock reads are all kept (non-ELO span logic)", len(set(kids)) == 6)

print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS} passed)")
sys.exit(1 if FAIL else 0)
