"""Unit tests for the persistence-aware icon vote (Layer 1) + db_log chain merge (Layer 2).
See DESIGN_persistence_aware_icon_vote.md. Run: .venv/Scripts/python.exe scratch/test_icon_vote.py
"""
import os, re, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr
import db_log
import config

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    PASS += cond; FAIL += (not cond)

# Faithful copy of parse_killfeed_line's kill-marker test (parsers.py:455).
def has_kill_marker(text):
    return bool(re.search(r"<\s*kill[_\s]*icon\s*>|\bkillicon\b", text.lower()))

def V(seq, base=1000.0):
    """Build variants [(ts, text)] from a marker sequence like 'ggkgg' (g=gun, k=kill, .=none)."""
    out = []
    for i, c in enumerate(seq):
        mk = {'g': ' <gun_icon> ', 'k': ' <kill_icon> ', '.': ' '}[c]
        out.append((base + i, f"boutwork{mk}arcticfox20023"))
    return out

# Force known thresholds regardless of config defaults.
ocr.ICON_KILL_MIN_RUN = 3
ocr.ICON_KILL_MIN_FRAC = 0.50

print("== _icon_vote decision (MIN_RUN=3, MIN_FRAC=0.5) ==")
check("sparse flip (gggkggg) -> gun (phantom rejected)", ocr._icon_vote(V("gggkggg"))[0] == 'gun')
check("isolated double flip (gkggkg) -> gun",            ocr._icon_vote(V("gkggkg"))[0] == 'gun')
check("sustained kill run (kkkkk) -> kill",              ocr._icon_vote(V("kkkkk"))[0] == 'kill')
check("knock->finish block (ggkkkk) -> kill",            ocr._icon_vote(V("ggkkkk"))[0] == 'kill')
check("knock only (ggggg) -> gun",                       ocr._icon_vote(V("ggggg"))[0] == 'gun')
check("fast finish 2 kills (kk) -> gun (documents FN)",  ocr._icon_vote(V("kk"))[0] == 'gun')
check("run ok but frac<0.5 (kkkgggg) -> gun",            ocr._icon_vote(V("kkkgggg"))[0] == 'gun')
# stats sanity
_, st = ocr._icon_vote(V("gggkggg"))
check("stats: kill=1 gun=6 run=1", st['kill'] == 1 and st['gun'] == 6 and st['kill_run'] == 1)

print("== _apply_icon_decision -> has_kill_marker contract ==")
check("kill decision injects kill token",   has_kill_marker(ocr._apply_icon_decision("boutwork <gun_icon> arcticfox", 'kill')))
check("gun decision leaves no kill token",   not has_kill_marker(ocr._apply_icon_decision("boutwork <kill_icon> arcticfox", 'gun')))
check("mangled marker still forced to kill", has_kill_marker(ocr._apply_icon_decision("boutwork <gil_icon> arcticfox", 'kill')))
check("split still works after decision",    ',' not in ocr._apply_icon_decision("a <kill_icon> b", 'gun'))  # sanity: returns a string

print("== find_recent_match merges across markers only when enabled ==")
now = 2000.0
tracker = {"boutwork <gun_icon> arcticfox20023": [(now, "boutwork <gun_icon> arcticfox20023")]}
ocr.ICON_VOTE_ENABLED = False
r_off = ocr.find_recent_match("boutwork <kill_icon> arcticfox20023", tracker, now)
check("vote OFF: kill read does NOT merge into gun track", r_off == "boutwork <kill_icon> arcticfox20023")
ocr.ICON_VOTE_ENABLED = True
r_on = ocr.find_recent_match("boutwork <kill_icon> arcticfox20023", tracker, now)
check("vote ON:  kill read merges into existing gun track", r_on == "boutwork <gun_icon> arcticfox20023")

print("== flush_old_events end-to-end label ==")
ocr.ICON_VOTE_ENABLED = True
ocr.ICON_VOTE_LOG = False
# One merged track: mostly knock reads + one flip -> must emit a Knock (no kill marker)
key = "boutwork <gun_icon> arcticfox20023"
tr = {key: V("gggkggg", base=3000.0)}
out = ocr.flush_old_events(tr, now=3000.0 + 100, event_crops=None, streamer="TestS")
check("flush emitted exactly one event", len(out) == 1)
check("phantom-flip track emitted as Knock (no kill marker)", not has_kill_marker(out[0][1]))
# Sustained kill track -> Kill
tr2 = {key: V("gkkkkk", base=4000.0)}
out2 = ocr.flush_old_events(tr2, now=4000.0 + 100, event_crops=None, streamer="TestS")
check("sustained-kill track emitted as Kill", has_kill_marker(out2[0][1]))

print("== Layer 2: STICKY_CHAIN_MERGE_TYPES merges kill+knock into one chain ==")
def ins(db, et):
    return db_log.insert_event(streamer="ZZ", timestamp="2026-07-09 15:00:00", raw_text="r",
        canonical="boutwork <gun_icon> arcticfox", event_type=et,
        attacker="boutwork", victim="arcticfox", db_path=db)
# Fresh DB per phase so the 20s exact-dedup doesn't swallow cross-phase repeats.
# merge OFF -> Kill and Knock live in separate chain groups
db_off = Path(tempfile.mkdtemp()) / "off.db"; db_log.init_db(db_off)
config.STICKY_CHAIN_MERGE_TYPES = False
db_log._chain_state.clear()
ins(db_off, "Kill"); ins(db_off, "Knock")
groups_off = [k for k in db_log._chain_state if k[0] == "ZZ"]
check("merge OFF: kill/knock -> 2 separate chain groups", len(groups_off) == 2)
# merge ON -> one group keyed by streamer only; kill+knock share ONE cluster (not two)
db_on = Path(tempfile.mkdtemp()) / "on.db"; db_log.init_db(db_on)
config.STICKY_CHAIN_MERGE_TYPES = True
db_log._chain_state.clear()
ins(db_on, "Kill"); ins(db_on, "Knock")
groups_on = [k for k in db_log._chain_state if k[0] == "ZZ"]
one = db_log._chain_state.get(("ZZ",), [])
check("merge ON:  single group key (streamer,)", groups_on == [("ZZ",)])
check("merge ON:  kill+knock share ONE cluster (not split)", len(one) == 1)
config.STICKY_CHAIN_MERGE_TYPES = False  # restore

print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS} passed)")
sys.exit(1 if FAIL else 0)
