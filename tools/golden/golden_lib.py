"""Single source of truth for the vision GOLDEN ground truth + name matching.

Both score_ocr.py (parser/collapse harness) and replay_dblog.py (production db_log dedup harness)
import from here so there is exactly ONE definition of "the golden game" -- if the ground truth or
the crop-verified corrections drift, they drift in one place, not two.
"""
import json, os, re
from difflib import SequenceMatcher

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GAP = 120   # respawn re-cluster window: a victim dying again after > GAP is a NEW death, not a re-read


def norm(s):
    s = re.sub(r'\[[^\]]*\]', '', s or '')      # drop [CLAN] tags
    return re.sub(r'[^a-z0-9]', '', s.lower())

def stem(s):
    return re.sub(r'\d+$', '', norm(s))

def fz(a, b):
    return SequenceMatcher(None, a, b).ratio()

def vmatch(a, b):
    """Same-source (vision<->vision) victim match: tight fuzz / prefix / numeric-stem."""
    na, nb = norm(a), norm(b)
    if not na or not nb: return False
    if fz(na, nb) >= 0.82: return True
    if len(na) >= 4 and len(nb) >= 4 and (na.startswith(nb) or nb.startswith(na)): return True
    if stem(na) and stem(na) == stem(nb): return True
    return False

def xmatch(gv, ov):
    """Cross-source (vision golden <-> garbled OCR) victim match: looser fuzz + prefix + stem, since
    OCR names are jittered vs the clean vision names."""
    ng, no = norm(gv), norm(ov)
    if not ng or not no: return False
    if fz(ng, no) >= 0.6: return True
    if len(ng) >= 4 and len(no) >= 4 and (ng[:5] == no[:5] or ng.startswith(no) or no.startswith(ng)): return True
    if stem(ng) and stem(ng) == stem(no): return True
    return False


# Verified golden corrections (crop-checked reversible OVERLAY; vision_reads.jsonl is left UNTOUCHED).
# Claude vision (opus, 1/sec) made two NON-kill misreads, each confirmed against the kf_crop pixels:
#   t~536  "we lose to the 6'7 robot?"  = CHAT text overlaid on the death-recap screen (crop 0536.png),
#                                         not a killfeed elimination.
#   t~679  [LGMA] Superbadger10 -> [LIVE] Calamoriii = KNOCK only (gun icon, NO red skull in crop
#                                         0679.png); the OCR pipeline correctly read it as a knock.
# Both are dropped from the ground-truth denominator. NOTE the near-misses we did NOT drop: rCloudy@180
# and reo@517 ARE real kills -- a red skull is clearly visible in crops 0180.png / 0517.png -- so they
# remain in the golden as genuine misses. Keyed tightly by (victim substring, t-window).
def is_golden_misread(r):
    vic = (r.get("victim") or "").lower()
    t = r["t"]
    if "we lose to" in vic and 530 <= t <= 545:                       # chat misread as a kill
        return True
    if "calamor" in vic and 676 <= t <= 685 and r["kind"] == "kill":  # knock mislabeled as a kill
        return True
    return False


def build_golden(data_dir=DATA):
    """Re-cluster the vision kill/bleedout reads into distinct eliminations (respawn-aware, GAP=120s),
    applying the crop-verified corrections. Returns a list of dicts: {vic, atk:set, t0, t1}."""
    vr = [json.loads(l) for l in open(os.path.join(data_dir, "vision_reads.jsonl"), encoding="utf-8")]
    elim = [r for r in vr if r["kind"] in ("kill", "bleedout") and r.get("victim")
            and not is_golden_misread(r)]
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
    return golden
