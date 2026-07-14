"""FAST PASS (iterate here in seconds, no re-OCR): replay saved reads.jsonl in video-PTS order
through the real parser, then transparently collapse to distinct events for the golden-game
name-chain review + distinct-elimination count vs the >=57 floor.

Reuses parse_killfeed_line (so parser/normalize fixes transfer to production). Collapse is a
transparent video-time + fuzzy-name grouping we can eyeball against frames -- NOT the suspect
db_log sticky-chain dedup (that's a separate study, per the review).

Usage: python _vod_parse.py
"""
import json, os
from difflib import SequenceMatcher
import re

import config
from database import PlayerDatabase
from parsers import parse_killfeed_line

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(SP, "vod_capture")
ELIM_TYPES = {"Kill", "BleedOut", "ChampionEliminated"}  # ChampionEliminated = death banner (victim-only)

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def fuzzy(a, b):
    return SequenceMatcher(None, a, b).ratio()

def main():
    reads = [json.loads(l) for l in open(os.path.join(OUT, "reads.jsonl"), encoding="utf-8")]
    reads.sort(key=lambda r: r["t"])
    print(f"reads: {len(reads)}  video span: {reads[0]['t']:.1f}-{reads[-1]['t']:.1f}s")

    db = PlayerDatabase(); db.load_databases()   # faithful normalization context; never saved

    parsed = []
    n_evt = 0; n_drop_atk = 0
    for r in reads:
        try: p = parse_killfeed_line(r["text"], db, r["t"])
        except Exception: p = None
        et = (p or {}).get("event_type")
        if not et: continue
        n_evt += 1
        atk, vic = p.get("attacker"), p.get("victim")
        if et in ELIM_TYPES and not atk: n_drop_atk += 1
        parsed.append({"t": r["t"], "et": et, "atk": atk, "vic": vic, "raw": r["text"], "crop": r["crop"]})

    # ---- transparent collapse of ELIMINATIONS by victim identity ----
    # A victim can die MORE THAN ONCE per game (respawn beacon / re-deploy), so we do NOT merge
    # by name alone -- that over-merges a player's separate deaths into one (measured: chaosboy91,
    # I AM HERE, Smurfette each merged 2-3 deaths spanning 300-600s). Merge only when the victim
    # matches AND the read is within RESPAWN_GAP of the death's last read; a longer silence starts
    # a NEW death. GAP mirrors the golden's own 120s re-cluster window (score_ocr.py) and is far
    # larger than a single death's feed-lingering (<30s), so it never splits one death in two.
    # NOTE: this is the HARNESS collapse only. Production db_log sticky-chain needs the same
    # respawn-gap fix (beads 0zd/vmu) before these kills ship end-to-end.
    deaths = []   # {vic_norm, vic_disp, atk, t0, t1, n, ets:set, raws:[]}
    VIC_SIM = 0.82
    RESPAWN_GAP = 120
    for p in [x for x in parsed if x["et"] in ELIM_TYPES]:
        vn = norm(p["vic"])
        if not vn: continue
        hit = None
        for d in deaths:
            if fuzzy(vn, d["vic_norm"]) >= VIC_SIM and p["t"] - d["t1"] <= RESPAWN_GAP:
                hit = d; break
        if hit is None:
            deaths.append({"vic_norm": vn, "vic_disp": p["vic"], "atk": p["atk"], "t0": p["t"],
                           "t1": p["t"], "n": 1, "ets": {p["et"]}, "atks": {norm(p["atk"])} if p["atk"] else set()})
        else:
            hit["t1"] = p["t"]; hit["n"] += 1; hit["ets"].add(p["et"])
            if p["atk"]: hit["atks"].add(norm(p["atk"]))

    # distinct knocks (informational)
    knocks = [x for x in parsed if x["et"] == "Knock"]

    print(f"\nparsed events (per-read): {n_evt}  | elim reads: {sum(1 for x in parsed if x['et'] in ELIM_TYPES)}"
          f"  knock reads: {len(knocks)}")
    print(f"eliminations with attacker DROPPED (atk=None): {n_drop_atk}")
    print(f"\n==== DISTINCT ELIMINATIONS (collapsed by victim, sim>={VIC_SIM}): {len(deaths)} ====")
    print(f"(>=57 floor for a full game; streamer WON this one)\n")
    deaths.sort(key=lambda d: d["t0"])
    for i, d in enumerate(deaths, 1):
        multi_atk = "  <-- MULTI-ATK (conflation?)" if len(d["atks"]) > 1 else ""
        print(f" {i:2d}. victim={d['vic_disp']!s:20} atk={sorted(d['atks'])} "
              f"t={d['t0']:.0f}-{d['t1']:.0f} reads={d['n']} types={sorted(d['ets'])}{multi_atk}")

    # write distinct events for downstream
    with open(os.path.join(OUT, "distinct_eliminations.jsonl"), "w", encoding="utf-8") as f:
        for d in deaths:
            d2 = dict(d); d2["ets"] = sorted(d["ets"]); d2["atks"] = sorted(d["atks"])
            f.write(json.dumps(d2) + "\n")

if __name__ == "__main__":
    main()
