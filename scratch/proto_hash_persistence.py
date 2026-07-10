"""Prototype: can a perceptual hash identify re-reads of ONE persistent (sticky) killfeed line as
'the same', despite the semi-transparent-overlay-over-moving-gameplay problem? And does it SEPARATE
same-line re-reads from DIFFERENT killfeed lines (so it could power a pre-OCR persistence filter,
which strings can't do for similar default-names)?

Test data: the sticky chains found earlier each have many DB rows = re-reads of one physical line
across its lifespan, each with a saved raw crop. We dHash every raw crop and compare:
  - SAME-LINE Hamming distances: crops within one sticky chain (should be small if hash is robust).
  - DIFF-LINE Hamming distances: crops from different chains, same streamer (should be large).
A clean gap => hashing works. Overlap => transparency/background bleed defeats it.

Read-only. Run: .venv/Scripts/python.exe scratch/proto_hash_persistence.py
"""
import re, sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"g:\PycharmProjects\TesseractApexOCR")
BAK = ROOT / "killfeed.db.bak_pre_elo_cap2_20260710_134337"
SIM, CAP = 0.82, 2
_N = re.compile(r"[^a-z0-9]")
def sig(a, v): return _N.sub("", (a or "").lower()) + "|" + _N.sub("", (v or "").lower())

import cv2

def dhash(path, hx=8, hy=8):
    """Classic dHash: grayscale, resize to (hx+1, hy), compare adjacent columns -> hx*hy bits."""
    im = Image.open(path).convert("L").resize((hx + 1, hy), Image.LANCZOS)
    a = np.asarray(im, dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).flatten()   # boolean array of hx*hy bits

def ahash(path, hx=8, hy=8):
    im = Image.open(path).convert("L").resize((hx, hy), Image.LANCZOS)
    a = np.asarray(im, dtype=np.int16)
    return (a > a.mean()).flatten()

def _otsu_mask(path):
    """Isolate the (stable) text from the (moving) semi-transparent background: grayscale -> Otsu
    binary. This is the transparency fix -- text pixels are extreme, background bleed is mid-tone."""
    g = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2GRAY)
    _, m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if m.mean() > 127: m = 255 - m       # normalize polarity: text = white
    return m

def dhash_masked(path, hx=8, hy=8):
    m = cv2.resize(_otsu_mask(path), (hx + 1, hy), interpolation=cv2.INTER_AREA).astype(np.int16)
    return (m[:, 1:] > m[:, :-1]).flatten()

def ahash_masked(path, hx=8, hy=8):
    m = cv2.resize(_otsu_mask(path), (hx, hy), interpolation=cv2.INTER_AREA).astype(np.int16)
    return (m > m.mean()).flatten()

def ham(a, b): return int(np.count_nonzero(a != b))

def crop_path(st, base):
    if not base: return None
    p = ROOT / "crops" / st / f"{base}_raw.png"
    return str(p) if p.exists() else None

# Reconstruct chains from backup.
b = sqlite3.connect(str(BAK)); b.row_factory = sqlite3.Row
rows = b.execute(
    "SELECT streamer,event_type,attacker,victim,created_at,crop_filename FROM events "
    "WHERE (event_type='Kill' OR (event_type='BleedOut' AND attacker!='' AND victim!='')) "
    "ORDER BY created_at,id").fetchall()
groups = defaultdict(list); chains = []
for r in rows:
    if not (r["attacker"] and r["victim"]): continue
    key = (r["streamer"], r["event_type"]); ts = r["created_at"]; s = sig(r["attacker"], r["victim"])
    live = groups[key]; live[:] = [c for c in live if ts - c["last_ts"] < 150]
    best, br = None, 0.0
    for c in live:
        ra = SequenceMatcher(None, s, c["sig"]).ratio()
        if ra >= SIM and ra > br: best, br = c, ra
    if best is None:
        c = {"key": key, "sig": s, "last_ts": ts, "members": [r], "line": f'{r["attacker"]}->{r["victim"]}'}
        live.append(c); chains.append(c)
    else:
        best["last_ts"] = ts; best["members"].append(r)

# Attach existing raw-crop paths; keep sticky chains (len>CAP) with >=4 crops on disk.
for c in chains:
    c["crops"] = [p for m in c["members"] if (p := crop_path(c["key"][0], m["crop_filename"]))]
sticky = sorted([c for c in chains if len(c["members"]) > CAP and len(c["crops"]) >= 4],
                key=lambda c: -len(c["crops"]))
print(f"sticky chains with >=4 raw crops on disk: {len(sticky)}")
if not sticky:
    print("no crop-backed sticky chains available (crops for those sessions were not retained)."); raise SystemExit

# Use the top chains; group by streamer for a fair DIFF-LINE contrast.
use = sticky[:12]
for c in use:
    print(f"  [{c['key'][0]:14s}] {c['key'][1]:8s} len={len(c['members']):2d} crops={len(c['crops']):2d} '{c['line'][:34]}'")

def hashes(paths, fn):
    out = []
    for p in paths:
        try: out.append(fn(p))
        except Exception: pass
    return out

for name, fn in (("dHash", dhash), ("aHash", ahash),
                 ("dHash-masked", dhash_masked), ("aHash-masked", ahash_masked)):
    same = []
    for c in use:
        hs = hashes(c["crops"][:12], fn)
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                same.append(ham(hs[i], hs[j]))
    # diff-line: pairs of chains sharing a streamer, one crop each
    by_st = defaultdict(list)
    for c in use: by_st[c["key"][0]].append(c)
    diff = []
    for st, cs in by_st.items():
        if len(cs) < 2: continue
        reps = [hashes(c["crops"][:3], fn) for c in cs]
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                for hi in reps[i]:
                    for hj in reps[j]:
                        diff.append(ham(hi, hj))
    same = np.array(same); diff = np.array(diff)
    print(f"\n== {name} (64-bit) ==")
    if len(same):
        print(f"  SAME-line  n={len(same):4d}  Hamming  min={same.min():2d} median={np.median(same):4.1f} "
              f"p90={np.percentile(same,90):4.1f} max={same.max():2d}")
    if len(diff):
        print(f"  DIFF-line  n={len(diff):4d}  Hamming  min={diff.min():2d} median={np.median(diff):4.1f} "
              f"p10={np.percentile(diff,10):4.1f} max={diff.max():2d}")
    if len(same) and len(diff):
        # A threshold works if most SAME < most DIFF. Report separation at a few cutoffs.
        for t in (6, 8, 10, 12, 14):
            tpr = (same <= t).mean()   # same-line correctly called "same"
            fpr = (diff <= t).mean()   # diff-line wrongly called "same"
            print(f"    thresh<={t:2d}: same-as-same {tpr*100:4.0f}%   diff-as-same(false) {fpr*100:4.0f}%")
