"""analyze_failures.py (v2) -- recognizer-headroom split on KILLFEED crops only, full CER distribution.

Fixes v1: (1) no binary sim gate (that hid the near-misses); bucket the WHOLE CER distribution so the
recognizer-fixable band (0<CER<=0.15, ~1-2 chars off) is visible. (2) killfeed crops only -- exclude
chat/overlay/timestamp text the pipeline never cares about (filter to lines with kill icons or a
[Bleed Out]/Shield-Broken marker). eval_holdout(unseen) reported separately from labels_clean (which
apex.pth trained on -> its perfect-rate is inflated by memorization).
"""
import csv, os
import cv2
import ocr as ocr_mod
from ocr import ocr_with_easyocr, preprocess_for_easyocr
from pipeline_evaluator import character_levenshtein

def norm(s):
    s = (s or "").replace("<GUN_ICON>", " ").replace("<KILL_ICON>", " ")
    return " ".join(s.split()).strip()

def is_killfeed(label, quality):
    if quality == "killfeed":
        return True
    low = label.lower()
    return ("<gun_icon>" in label.lower() or "<kill_icon>" in label.lower()
            or "[bleed out]" in low or "shield broken" in low)

def load(path, tag):
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r.get("quality") == "noise" or not r["label"].strip():
            continue
        if not os.path.exists(r["filepath"]):
            continue
        if not is_killfeed(r["label"], r.get("quality", "")):
            continue
        rows.append((r["filepath"], r["label"], tag))
    return rows

data = load("labels/eval_holdout.csv", "holdout") + load("labels/labels_clean.csv", "clean")
print(f"KILLFEED crops to score: {len(data)}")

ocr_mod._easyocr_reader = None
ocr_mod._get_easyocr_reader()

res = []
for i, (path, gt, tag) in enumerate(data):
    img = cv2.imread(path)
    if img is None:
        continue
    text = ocr_with_easyocr(preprocess_for_easyocr(img)[0]).strip()
    g, o = norm(gt), norm(text)
    cer = character_levenshtein(g, o) / max(1, len(g))
    res.append((path, g, o, cer, tag))
    if (i + 1) % 200 == 0:
        print(f"  scored {i+1}/{len(data)}")

def dist(subset, label):
    n = max(1, len(subset))
    perfect = [r for r in subset if r[3] == 0]
    near = [r for r in subset if 0 < r[3] <= 0.15]     # recognizer-fixable candidate
    mod = [r for r in subset if 0.15 < r[3] <= 0.4]
    sev = [r for r in subset if r[3] > 0.4]
    print(f"\n=== {label} (n={len(subset)}) ===")
    print(f"  perfect  (CER=0):            {len(perfect):4d} ({100*len(perfect)//n}%)")
    print(f"  NEAR-MISS(0<CER<=.15):       {len(near):4d} ({100*len(near)//n}%)   <- recognizer-fixable band")
    print(f"  moderate (.15-.4):           {len(mod):4d} ({100*len(mod)//n}%)")
    print(f"  severe   (>.4):              {len(sev):4d} ({100*len(sev)//n}%)")
    return near, sev

print("\n########## HOLDOUT (unseen, clean) ##########")
h_near, h_sev = dist([r for r in res if r[4] == "holdout"], "HOLDOUT killfeed")
print("\n########## LABELS_CLEAN (apex trained on these -> perfect% inflated) ##########")
c_near, c_sev = dist([r for r in res if r[4] == "clean"], "CLEAN killfeed")

print("\n--- NEAR-MISS samples (holdout; would a better recognizer fix these?) ---")
for r in h_near[:20]:
    print(f"  cer={r[3]:.2f}  gt={r[1]!r}  ocr={r[2]!r}")
print("\n--- SEVERE samples (holdout; garbled/label-error?) ---")
for r in h_sev[:12]:
    print(f"  cer={r[3]:.2f}  gt={r[1]!r}  ocr={r[2]!r}")
