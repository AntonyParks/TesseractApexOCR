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
from difflib import SequenceMatcher

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def norm(s):
    s = re.sub(r'\[[^\]]*\]', '', s or '')
    return re.sub(r'[^a-z0-9]', '', s.lower())
def stem(s): return re.sub(r'\d+$', '', norm(s))
def fz(a, b): return SequenceMatcher(None, a, b).ratio()
def vmatch(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb: return False
    if fz(na, nb) >= 0.82: return True
    if len(na) >= 4 and len(nb) >= 4 and (na.startswith(nb) or nb.startswith(na)): return True
    if stem(na) and stem(na) == stem(nb): return True
    return False

# ---- GOLDEN: re-cluster vision kill/bleedout reads with prefix-aware merge (collapse over-splits)
vr = [json.loads(l) for l in open(os.path.join(SP, "vision_reads.jsonl"), encoding="utf-8")]

# Verified golden corrections (crop-checked reversible OVERLAY; vision_reads.jsonl is left UNTOUCHED).
# Claude vision (opus, 1/sec) made two NON-kill misreads, each confirmed against the kf_crop pixels:
#   t~536  "we lose to the 6'7 robot?"  = CHAT text overlaid on the death-recap screen (crop 0536.png),
#                                         not a killfeed elimination.
#   t~679  [LGMA] Superbadger10 -> [LIVE] Calamoriii = KNOCK only (gun icon, NO red skull in crop
#                                         0679.png); the OCR pipeline correctly read it as a knock.
# Both are dropped from the ground-truth denominator. NOTE the near-misses we did NOT drop: rCloudy@180
# and reo@517 ARE real kills -- a red skull is clearly visible in crops 0180.png / 0517.png -- so they
# remain in the golden as genuine OCR misses. Keyed tightly by (victim substring, t-window) so only the
# two verified non-kill reads are removed.
def _is_golden_misread(r):
    vic = (r.get("victim") or "").lower()
    t = r["t"]
    if "we lose to" in vic and 530 <= t <= 545:            # chat misread as a kill
        return True
    if "calamor" in vic and 676 <= t <= 685 and r["kind"] == "kill":  # knock mislabeled as a kill
        return True
    return False

elim = [r for r in vr if r["kind"] in ("kill", "bleedout") and r.get("victim") and not _is_golden_misread(r)]
GAP = 120
golden = []
for r in sorted(elim, key=lambda x: x["t"]):
    hit = None
    for d in golden:
        if vmatch(r["victim"], d["vic"]) and r["t"] - d["t1"] <= GAP:
            hit = d; break
    if hit is None:
        golden.append({"vic": r["victim"], "atk": set(filter(None, [r.get("attacker")])),
                       "t0": r["t"], "t1": r["t"]})
    else:
        hit["t1"] = r["t"]
        if len(r["victim"]) > len(hit["vic"]): hit["vic"] = r["victim"]   # keep fullest name
        if r.get("attacker"): hit["atk"].add(r["attacker"])
golden.sort(key=lambda d: d["t0"])

# ---- OCR distinct eliminations + raw reads
ocr = [json.loads(l) for l in open(os.path.join(SP, "vod_capture", "distinct_eliminations.jsonl"), encoding="utf-8")]
raw = [json.loads(l) for l in open(os.path.join(SP, "vod_capture", "reads.jsonl"), encoding="utf-8")]

# ---- match golden <-> ocr. OCR names are GARBLED vs the clean vision names, so use a looser
# cross-source match (lower fuzz, prefix, wider time window) than the golden self-dedup.
def xmatch(gv, ov):
    ng, no = norm(gv), norm(ov)
    if not ng or not no: return False
    if fz(ng, no) >= 0.6: return True
    if len(ng) >= 4 and len(no) >= 4 and (ng[:5] == no[:5] or ng.startswith(no) or no.startswith(ng)): return True
    if stem(ng) and stem(ng) == stem(no): return True
    return False
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
