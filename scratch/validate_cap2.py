"""Validate the ELO cap-2 retro clean: was it an improvement (removed real sticky inflation) or a
regression (dropped distinct kills)? Reconstructs the trimmed chains from the pre-clean backup and
(1) structurally audits every deletion for over-suppression, (2) emits crop paths for a diverse
sample of chains so the deletions can be eyeballed against the kept rows.

Read-only. Run: .venv/Scripts/python.exe scratch/validate_cap2.py
"""
import re, sqlite3, os, json
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(r"g:\PycharmProjects\TesseractApexOCR")
BAK = ROOT / "killfeed.db.bak_pre_elo_cap2_20260710_134337"
CUR = ROOT / "killfeed.db"
GAP, SIM, CAP = 150, 0.82, 2
_NORM = re.compile(r"[^a-z0-9]")
def sig(a, v): return _NORM.sub("", (a or "").lower()) + "|" + _NORM.sub("", (v or "").lower())

cur = sqlite3.connect(str(CUR))
cur_ids = {r[0] for r in cur.execute("SELECT id FROM events")}

b = sqlite3.connect(str(BAK)); b.row_factory = sqlite3.Row
rows = b.execute(
    "SELECT id, streamer, event_type, attacker, victim, created_at, crop_filename "
    "FROM events WHERE (event_type='Kill' OR (event_type='BleedOut' AND attacker!='' AND victim!='')) "
    "ORDER BY created_at, id").fetchall()

# Reconstruct chains exactly as the cleanup did; record kept vs deleted members.
groups = defaultdict(list)
chains = []
for r in rows:
    if not (r["attacker"] and r["victim"]):
        continue
    key = (r["streamer"], r["event_type"]); ts = r["created_at"]; s = sig(r["attacker"], r["victim"])
    live = groups[key]
    live[:] = [c for c in live if ts - c["last_ts"] < GAP]
    best, br = None, 0.0
    for c in live:
        ra = SequenceMatcher(None, s, c["sig"]).ratio()
        if ra >= SIM and ra > br: best, br = c, ra
    if best is None:
        c = {"key": key, "sig": s, "last_ts": ts, "start": ts, "members": [r]}
        live.append(c); chains.append(c)
    else:
        best["last_ts"] = ts; best["members"].append(r)

trimmed = [c for c in chains if len(c["members"]) > CAP]
n_del = sum(len(c["members"]) - CAP for c in trimmed)
print(f"trimmed chains: {len(trimmed)}   rows deleted: {n_del}")

# ---- Over-suppression audit -------------------------------------------------
# A deletion is SAFE if the deleted row is the same elimination as the kept rows: same normalized
# victim AND attacker, OR high fuzzy sim to a kept row. FLAG deletions whose victim differs from
# BOTH kept rows' victims (a candidate distinct kill wrongly merged into the chain).
def nrm(x): return _NORM.sub("", (x or "").lower())
safe_exact = safe_fuzzy = flagged = 0
flag_examples = []
gap_flag = 0
for c in trimmed:
    kept = c["members"][:CAP]
    dele = c["members"][CAP:]
    kept_vic = {nrm(k["victim"]) for k in kept}
    kept_att = {nrm(k["attacker"]) for k in kept}
    kept_sig = [sig(k["attacker"], k["victim"]) for k in kept]
    prev_ts = kept[-1]["created_at"]
    for d in dele:
        ds = sig(d["attacker"], d["victim"])
        exact = nrm(d["victim"]) in kept_vic and nrm(d["attacker"]) in kept_att
        fuzzy = max(SequenceMatcher(None, ds, ks).ratio() for ks in kept_sig)
        # time gap from the most recent kept/prior row (chain is contiguous <150s by construction)
        if d["created_at"] - prev_ts > GAP:
            gap_flag += 1
        prev_ts = d["created_at"]
        if exact:
            safe_exact += 1
        elif fuzzy >= 0.90:
            safe_fuzzy += 1
        else:
            flagged += 1
            if len(flag_examples) < 25:
                flag_examples.append((c["key"][0], d["event_type"], kept[0]["attacker"],
                                      kept[0]["victim"], d["attacker"], d["victim"], round(fuzzy, 2)))

print(f"\n== Over-suppression audit of {n_del} deletions ==")
print(f"  SAFE  exact same (attacker,victim) as a kept row : {safe_exact:4d} ({100*safe_exact/n_del:.1f}%)")
print(f"  SAFE  fuzzy>=0.90 to a kept row                  : {safe_fuzzy:4d} ({100*safe_fuzzy/n_del:.1f}%)")
print(f"  FLAG  differs from kept (inspect)                : {flagged:4d} ({100*flagged/n_del:.1f}%)")
print(f"  (deletions arriving >150s after prior row: {gap_flag} -- should be 0 by construction)")
print("\nFlagged deletions (kept-line  vs  deleted-line, fuzzy):")
for st, et, ka, kv, da, dv, f in flag_examples:
    print(f"  [{st:14s}] {et:8s} kept '{ka[:16]}->{kv[:16]}'  del '{da[:16]}->{dv[:16]}'  fz={f}")

# ---- Crop sample for visual ground-truth ------------------------------------
# Pick a diverse set: the 3 biggest chains + 3 with the LOWEST intra-chain min-sim (borderline) +
# any flagged chains. Emit crop file paths (kept + up to 3 deleted) for montage.
def min_intrasim(c):
    sigs = [sig(m["attacker"], m["victim"]) for m in c["members"]]
    return min(SequenceMatcher(None, sigs[0], s).ratio() for s in sigs[1:])
def crop_path(streamer, base):
    if not base: return None
    for suff in ("_raw.png", ".png"):
        p = ROOT / "crops" / streamer / f"{base}{suff}"
        if p.exists(): return str(p)
    return None

biggest = sorted(trimmed, key=lambda c: -len(c["members"]))[:3]
borderline = sorted([c for c in trimmed], key=min_intrasim)[:4]
sample = []
seen = set()
for c in biggest + borderline:
    kidk = id(c)
    if kidk in seen: continue
    seen.add(kidk)
    st = c["key"][0]
    kept = [(m["id"], crop_path(st, m["crop_filename"])) for m in c["members"][:CAP]]
    dele = [(m["id"], crop_path(st, m["crop_filename"])) for m in c["members"][CAP:CAP+3]]
    sample.append({"streamer": st, "etype": c["key"][1],
                   "line": f'{c["members"][0]["attacker"]}->{c["members"][0]["victim"]}',
                   "len": len(c["members"]), "minsim": round(min_intrasim(c), 2),
                   "kept": kept, "deleted": dele})

out = ROOT / "scratch" / "validate_cap2_sample.json"
out.write_text(json.dumps(sample, indent=2))
print(f"\nwrote {out.name} with {len(sample)} sample chains for montage")
for s in sample:
    nk = sum(1 for _, p in s["kept"] if p); nd = sum(1 for _, p in s["deleted"] if p)
    print(f"  [{s['streamer']:14s}] {s['etype']:8s} len={s['len']:2d} minsim={s['minsim']} "
          f"'{s['line'][:34]}'  crops: kept {nk}/{len(s['kept'])} del {nd}/{len(s['deleted'])}")
