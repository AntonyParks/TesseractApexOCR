"""Aggregate vision reads -> golden ground-truth kill list; diff against the OCR pipeline's kills.

A death = a victim in a kill (skull) OR bleedout line. Dedup by victim (a victim dies once, unless
a respawn separates two deaths by a long gap). Then match golden victims against the OCR distinct
eliminations to see what OCR missed.
"""
import json, os, re
from difflib import SequenceMatcher

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def norm(s):
    s = re.sub(r'\[[^\]]*\]', '', s or '')       # strip clan tags
    return re.sub(r'[^a-z0-9]', '', s.lower())
def stem(s): return re.sub(r'\d+$', '', norm(s))
def fz(a, b): return SequenceMatcher(None, a, b).ratio()

vr = [json.loads(l) for l in open(os.path.join(SP, "vision_reads.jsonl"), encoding="utf-8")]
elim = [r for r in vr if r["kind"] in ("kill", "bleedout") and r.get("victim")]
knock = [r for r in vr if r["kind"] == "knock" and r.get("victim")]

# cluster elim reads into distinct deaths by victim identity + respawn gap
RESPAWN_GAP = 120
deaths = []   # {vn, disp, atk set, t0, t1, n, kinds}
for r in sorted(elim, key=lambda x: x["t"]):
    vn = norm(r["victim"])
    if not vn: continue
    hit = None
    for d in deaths:
        if (fz(vn, d["vn"]) >= 0.85 or (stem(vn) and stem(vn) == d["stem"])) and r["t"] - d["t1"] <= RESPAWN_GAP:
            hit = d; break
    if hit is None:
        deaths.append({"vn": vn, "stem": stem(vn), "disp": r["victim"], "atks": set(), "t0": r["t"],
                       "t1": r["t"], "n": 1, "kinds": {r["kind"]}})
        if r.get("attacker"): deaths[-1]["atks"].add(r["attacker"])
    else:
        hit["t1"] = r["t"]; hit["n"] += 1; hit["kinds"].add(r["kind"])
        if r.get("attacker"): hit["atks"].add(r["attacker"])

deaths.sort(key=lambda d: d["t0"])

# knock-only victims (never seen in a kill/bleedout line) -> either revived, or vision misclassified
death_norms = [d["vn"] for d in deaths]
knock_only = {}
for r in knock:
    vn = norm(r["victim"])
    if not vn: continue
    if any(fz(vn, dn) >= 0.85 for dn in death_norms): continue
    knock_only.setdefault(vn, r["victim"])

# OCR distinct eliminations
ocr = [json.loads(l) for l in open(os.path.join(SP, "vod_capture", "distinct_eliminations.jsonl"), encoding="utf-8")]
ocr_vn = [norm(o["vic_disp"]) for o in ocr]

print("="*76)
print("GOLDEN GAME (Claude vision, 1 read/sec) vs OCR PIPELINE  —  Sang's win")
print("="*76)
print(f"vision reads: {len(vr)} | elim-line reads: {len(elim)} | knock reads: {len(knock)}")
print(f"\nGOLDEN distinct eliminations (vision): {len(deaths)}")
print(f"OCR distinct eliminations             : {len(ocr)}")
print(f"apex floor (60 - 3 winners)           : >= 57")
print()
missed = []
for d in deaths:
    matched = any(fz(d["vn"], ov) >= 0.85 or (d["stem"] and d["stem"] == stem(ov)) for ov in ocr_vn)
    d["ocr"] = matched
    if not matched: missed.append(d)
print(f"golden kills the OCR pipeline MISSED entirely: {len(missed)}")
print("-"*76)
for i, d in enumerate(deaths, 1):
    mark = "  " if d["ocr"] else "  << OCR MISSED"
    a = "/".join(sorted(d["atks"])[:2]) if d["atks"] else "???"
    print(f" {i:2d}. t={d['t0']:4d}-{d['t1']:<4d} {a[:24]:>24} -> {d['disp'][:22]:<22}{mark}")
print("-"*76)
print(f"knock-only victims (revived or vision-classified as knock, not counted as deaths): {len(knock_only)}")
print("   " + ", ".join(list(knock_only.values())[:25]))
