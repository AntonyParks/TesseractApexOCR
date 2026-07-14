"""Score the OCR pipeline against the Claude-vision GOLDEN ground truth.

Metrics: recall (golden kills OCR found), precision (OCR kills that are real), and per-field
name accuracy on matched kills. Crucially, each MISS is classified:
  - NEVER CAPTURED : the victim never appeared in ANY raw OCR read near that time
                     -> detection / narrow-zone / total-OCR-failure (needs wider zone / better OCR)
  - LOST DOWNSTREAM: it WAS in a raw OCR read but never became a distinct elimination
                     -> parse / normalize / collapse (a cheap parser-side fix)
This partition is the tuning map for closing the gap.
"""
import json, os, re
from golden_lib import DATA as SP, norm, fz, vmatch, xmatch, build_golden

# ---- GOLDEN: distinct eliminations from the vision ground truth (respawn-aware clustering + the
# crop-verified corrections all live in golden_lib.build_golden, shared with replay_dblog.py).
golden = build_golden(SP)

# ---- OCR distinct eliminations + raw reads
ocr = [json.loads(l) for l in open(os.path.join(SP, "vod_capture", "distinct_eliminations.jsonl"), encoding="utf-8")]
raw = [json.loads(l) for l in open(os.path.join(SP, "vod_capture", "reads.jsonl"), encoding="utf-8")]

# ---- match golden <-> ocr (looser cross-source xmatch from golden_lib: OCR names are garbled).
used = set()
for g in golden:
    g["ocr"] = None
    best = None
    for i, o in enumerate(ocr):
        if i in used: continue
        overlap = not (o["t1"] < g["t0"] - 45 or o["t0"] > g["t1"] + 45)
        if overlap and xmatch(g["vic"], o["vic_disp"]):
            best = i; break
    if best is not None:
        used.add(best); g["ocr"] = ocr[best]

matched = [g for g in golden if g["ocr"]]
missed = [g for g in golden if not g["ocr"]]
spurious = [ocr[i] for i in range(len(ocr)) if i not in used]

# classify misses via raw reads
def ever_in_raw(g):
    for r in raw:
        if not (g["t0"] - 6 <= r["t"] <= g["t1"] + 6): continue
        nt = norm(r["text"])
        gv = norm(g["vic"])
        if gv and (gv in nt or fz(gv, nt) >= 0.6 or any(fz(gv, w) >= 0.8 for w in re.findall(r'[a-z0-9]{4,}', nt))):
            return r["text"]
    return None
for g in missed:
    g["raw"] = ever_in_raw(g)

# per-field name accuracy on matched
atk_ok = sum(1 for g in matched if g["atk"] and any(vmatch(a, oa) for a in g["atk"] for oa in g["ocr"]["atks"]))
vic_ok = len(matched)  # matched by victim by construction

G, O, M = len(golden), len(ocr), len(matched)
print("="*74)
print("OCR PIPELINE  scored against  VISION GOLDEN ground truth   (Sang's win)")
print("="*74)
print(f"golden kills (vision, de-duped): {G}")
print(f"OCR distinct eliminations      : {O}")
print(f"matched (OCR found the kill)   : {M}")
print(f"\n  RECALL    = {M}/{G} = {100*M//G}%   (share of real kills OCR captured)")
print(f"  PRECISION = {M}/{O} = {100*M//O}%   (share of OCR kills that are real)")
print(f"  ATTACKER name correct on matched: {atk_ok}/{M} = {100*atk_ok//M}%")
print(f"\n--- OCR MISSES: {len(missed)}  (broken down by cause) ---")
never = [g for g in missed if not g["raw"]]
lost = [g for g in missed if g["raw"]]
print(f"  NEVER CAPTURED (detection/zone/OCR)  : {len(never)}")
for g in never:
    print(f"     t={g['t0']:4d} {('/'.join(sorted(g['atk']))[:20] or '???'):>20} -> {g['vic'][:26]}")
print(f"  LOST DOWNSTREAM (parse/collapse fix) : {len(lost)}")
for g in lost:
    print(f"     t={g['t0']:4d} {g['vic'][:24]:<24} raw OCR saw: {g['raw'][:44]!r}")
print(f"\n--- OCR SPURIOUS (no golden match): {len(spurious)} ---")
for o in spurious:
    print(f"     t={int(o['t0']):4d} {('/'.join(o['atks'])[:20] or '???'):>20} -> {o['vic_disp'][:26]}")
