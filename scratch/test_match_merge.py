"""Synthetic tests for cross-streamer match merging."""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, r"g:\PycharmProjects\TesseractApexOCR")
from match_detector import Match, KillEvent, merge_cross_streamer_matches

T0 = datetime(2026, 7, 4, 12, 0, 0)

def mk(offset_s, atk, vic, aconf=0.8, vconf=0.8):
    return KillEvent(timestamp=T0 + timedelta(seconds=offset_s),
                     attacker=atk, victim=vic, attacker_conf=aconf, victim_conf=vconf)

def match(mid, streamer, kills):
    kills = sorted(kills, key=lambda k: k.timestamp)
    for o, k in enumerate(kills, 1):
        k.kill_order = o
    return Match(match_id=mid, streamer=streamer,
                 start_time=kills[0].timestamp, end_time=kills[-1].timestamp, kills=kills)

fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    if not ok: fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")

# --- Case 1: two streamers, same lobby, 3 shared kills (garbled + 15s skew), 2 unique each
A = match("A_1", "Zuni", [
    mk(0,   "yayakowalski", "hattiwari"),
    mk(60,  "alpha",        "bravo"),
    mk(120, "charlie",      "delta"),
    mk(180, "zebra99",      "quailfoot"),
    mk(240, "mountaindew",  "grapesoda"),
])
B = match("B_1", "Matafe_", [
    mk(15,  "yayakawalskia", "hattiwaria", aconf=0.95),   # garbled dup, higher conf
    mk(75,  "alpha",         "bravo"),
    mk(135, "charlie",       "delta"),
    mk(200, "kite42",        "willowisp"),
    mk(260, "thunderclap",   "pigeonhole"),
])
merged = merge_cross_streamer_matches([A, B])
check("case1 match count", len(merged), 1)
m = merged[0]
check("case1 kill count (5+5-3 shared)", m.kill_count, 7)
check("case1 merged_from", m.merged_from, ["B_1"])
check("case1 kill_order contiguous", [k.kill_order for k in m.kills], list(range(1, 8)))
check("case1 chronological", m.kills == sorted(m.kills, key=lambda k: k.timestamp), True)
shared = next(k for k in m.kills if k.attacker == "yayakowalski")
check("case1 corroborated conf upgraded", shared.attacker_conf, 0.95)
ub = next(k for k in m.kills if k.attacker == "kite42")
check("case1 secondary event skew-corrected (200-15)", (ub.timestamp - T0).total_seconds(), 185.0)

# --- Case 2: same streamer never merges
C1 = match("C_1", "Zuni", [mk(0, "a", "b"), mk(30, "c", "d"), mk(60, "e", "f")])
C2 = match("C_2", "Zuni", [mk(10, "a", "b"), mk(40, "c", "d"), mk(70, "e", "f")])
check("case2 same-streamer untouched", len(merge_cross_streamer_matches([C1, C2])), 2)

# --- Case 3: only 2 shared kills -> no merge
D = match("D_1", "Zuni",    [mk(0, "a", "b"), mk(30, "c", "d"), mk(60, "x1", "y1")])
E = match("E_1", "Matafe_", [mk(5, "a", "b"), mk(35, "c", "d"), mk(65, "x2", "y2")])
check("case3 below min-shared untouched", len(merge_cross_streamer_matches([D, E])), 2)

# --- Case 4: non-overlapping time windows -> no merge even with same names
F = match("F_1", "Zuni",    [mk(0, "a", "b"), mk(30, "c", "d"), mk(60, "e", "f")])
G = match("G_1", "Matafe_", [mk(7200, "a", "b"), mk(7230, "c", "d"), mk(7260, "e", "f")])
check("case4 disjoint windows untouched", len(merge_cross_streamer_matches([F, G])), 2)

# --- Case 5: transitive A~B, B~C -> single match
H = match("H_1", "S1", [mk(0, "p1", "q1"), mk(30, "p2", "q2"), mk(60, "p3", "q3"), mk(90, "h_only", "hv")])
I = match("I_1", "S2", [mk(5, "p1", "q1"), mk(35, "p2", "q2"), mk(65, "p3", "q3"),
                        mk(100, "r1", "s1"), mk(130, "r2", "s2"), mk(160, "r3", "s3")])
J = match("J_1", "S3", [mk(108, "r1", "s1"), mk(138, "r2", "s2"), mk(168, "r3", "s3"), mk(190, "j_only", "jv")])
out = merge_cross_streamer_matches([H, I, J])
check("case5 transitive merge count", len(out), 1)
check("case5 all events present", out[0].kill_count, 4 + 6 + 4 - 3 - 3)

# --- Case 6: thin kill views, merge driven by shared KNOCK fingerprints
K1 = match("K_1", "Zuni", [mk(0, "finisher1", "deadguy1"), mk(200, "finisher2", "deadguy2")])
K1.fingerprints = [mk(20, "knockerA", "downedB"), mk(50, "knockerC", "downedD"), mk(80, "knockerE", "downedF")]
K2 = match("K_2", "Matafe_", [mk(230, "slurpjuice", "hologram")])
K2.fingerprints = [mk(32, "knockerA", "downedB"), mk(63, "knockerC", "downedD"), mk(91, "knockerE", "downedF")]
out6 = merge_cross_streamer_matches([K1, K2])
check("case6 fingerprint-driven merge", len(out6), 1)
check("case6 kills union (2+1, none shared)", out6[0].kill_count, 3)
check("case6 merged_from", out6[0].merged_from, ["K_2"])

print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")
sys.exit(1 if fails else 0)
