"""Replay the golden game's OCR reads through the REAL production db_log.insert_event and score the
survivors against the vision golden -- a labeled corpus for the sticky-chain dedup that it never had.

Why this exists: db_log's sticky-chain suppression feeds ELO directly, and its victim-anchored merge
cannot tell a respawn death (same victim, jittered attacker, minutes later) from a persistent sticky
line without a cadence model. Before touching any of the hand-tuned constants (bead 0zd), we need to
MEASURE, on ground truth, what the current dedup costs: how many real respawn deaths it suppresses vs
how many phantom/sticky kills it correctly kills. This harness is that measurement. It is READ-ONLY
w.r.t. the product: it writes to a throwaway temp DB and NEVER touches elo.db / killfeed.db.

Faithful replay detail: db_log keys the whole chain on wall-clock time.time() and on the row
created_at (DEFAULT strftime('now')). A naive replay loop runs in milliseconds, collapsing the whole
game into one instant and over-suppressing. So we drive db_log's clock from GOLDEN VIDEO-TIME (inject
a fake time.time()) and rewrite each new row's created_at to the same video-time, so the 150s chain
gap and the 1800s seed-lookback see the real in-game deltas.

Run from repo root with PYTHONPATH=<repo> so it can import db_log/parsers/database.
"""
import json, os, tempfile, types
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import db_log
from database import PlayerDatabase
from parsers import parse_killfeed_line
from golden_lib import DATA, norm, xmatch, build_golden, GAP
from db_log import (STICKY_CHAIN_GAP_SECONDS, STICKY_SEED_LOOKBACK_SECONDS,
                    STICKY_SIM_THRESHOLD, STICKY_VIC_ANCHOR_SIM)

ELIM_TYPES = {"Kill", "BleedOut", "ChampionEliminated"}
EPOCH0 = 1_700_000_000          # arbitrary fixed base; video-time t maps to EPOCH0 + t
STREAMER = "replay"
VIC_SIM = 0.82                  # same victim-collapse threshold parse_game.py uses
BASE_DT = datetime(2024, 1, 1)  # video-time t (s) -> BASE_DT + t, so match_detector can _parse_ts it
TS_FMT = "%Y-%m-%d %H:%M:%S"

def t_to_ts(t):    return (BASE_DT + timedelta(seconds=float(t))).strftime(TS_FMT)
def ts_to_t(ts):   return (datetime.strptime(ts, TS_FMT) - BASE_DT).total_seconds()


def fz(a, b):
    return SequenceMatcher(None, a, b).ratio()


def collapse(rows):
    """Respawn-aware victim collapse (identical to parse_game.py): merge only within GAP seconds."""
    deaths = []
    for r in sorted(rows, key=lambda x: x["t"]):
        vn = norm(r["vic"])
        if not vn:
            continue
        hit = None
        for d in deaths:
            if fz(vn, d["vic_norm"]) >= VIC_SIM and r["t"] - d["t1"] <= GAP:
                hit = d; break
        if hit is None:
            deaths.append({"vic_norm": vn, "vic_disp": r["vic"], "t0": r["t"], "t1": r["t"],
                           "n": 1, "atks": {norm(r["atk"])} if r["atk"] else set()})
        else:
            hit["t1"] = r["t"]; hit["n"] += 1
            if r["atk"]: hit["atks"].add(norm(r["atk"]))
    deaths.sort(key=lambda d: d["t0"])
    return deaths


def score(golden, deaths, label):
    """Match golden deaths <-> distinct deaths (xmatch victim, +-45s overlap). Returns (matched, missed,
    spurious)."""
    used = set()
    matched, missed = [], []
    for g in golden:
        best = None
        for i, o in enumerate(deaths):
            if i in used:
                continue
            overlap = not (o["t1"] < g["t0"] - 45 or o["t0"] > g["t1"] + 45)
            if overlap and xmatch(g["vic"], o["vic_disp"]):
                best = i; break
        if best is not None:
            used.add(best); g["_hit"] = deaths[best]; matched.append(g)
        else:
            g["_hit"] = None; missed.append(g)
    spurious = [deaths[i] for i in range(len(deaths)) if i not in used]
    return matched, missed, spurious


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Replay golden reads through db_log dedup and score.")
    ap.add_argument("--cap", type=int, default=None,
                    help="override STICKY_ELO_CHAIN_MAX_ROWS (e.g. --cap 1 as a positive control: it "
                         "SHOULD start suppressing with-attacker respawns, proving the harness detects "
                         "db_log losses when they exist).")
    args = ap.parse_args()
    if args.cap is not None:
        db_log.STICKY_ELO_CHAIN_MAX_ROWS = args.cap   # read at call-time in insert_event

    reads = [json.loads(l) for l in open(os.path.join(DATA, "vod_capture", "reads.jsonl"), encoding="utf-8")]
    reads.sort(key=lambda r: r["t"])
    golden = build_golden()

    # --- fresh throwaway DB + video-time clock injection ------------------------------------------
    tmp = os.path.join(tempfile.gettempdir(), "replay_dblog.sqlite")
    for ext in ("", "-wal", "-shm"):
        try: os.remove(tmp + ext)
        except OSError: pass

    clock = {"now": float(EPOCH0)}
    db_log.time = types.SimpleNamespace(time=lambda: clock["now"])   # db_log uses only time.time()
    db_log._chain_state.clear()                                      # no state bleed between runs
    conn = db_log._get_write_conn(tmp)                               # same conn insert_event reuses

    pdb = PlayerDatabase(); pdb.load_databases()                     # faithful normalization context

    parsed_elims = []       # every elim read fed in (pre-dedup), for the "harness collapse" baseline
    fed = kept = 0
    last_max_id = 0
    for r in reads:
        p = parse_killfeed_line(r["text"], pdb, r["t"])
        et = p.get("event_type")
        if et not in ELIM_TYPES:
            continue
        atk, vic = p.get("attacker") or "", p.get("victim") or ""
        if not vic:
            continue
        parsed_elims.append({"t": r["t"], "vic": vic, "atk": atk, "et": et})
        fed += 1
        clock["now"] = float(EPOCH0 + r["t"])
        rid = db_log.insert_event(STREAMER, t_to_ts(r["t"]), r["text"], p.get("canonical", r["text"]),
                                  et, atk, vic, 1.0, 1.0, db_path=tmp)
        if rid and rid > last_max_id:                               # a NEW row was inserted (not suppressed)
            conn.execute("UPDATE events SET created_at=? WHERE id=?", (int(EPOCH0 + r["t"]), rid))
            conn.commit()
            last_max_id = rid
            kept += 1

    # --- pull the SURVIVING elim rows db_log kept, collapse them respawn-aware -----------------------
    surv = [{"t": ts_to_t(row["timestamp"]), "vic": row["victim"], "atk": row["attacker"]}
            for row in conn.execute(
                "SELECT timestamp, attacker, victim FROM events WHERE event_type IN (?,?,?)",
                tuple(ELIM_TYPES)).fetchall()]
    db_deaths = collapse(surv)
    harness_deaths = collapse(parsed_elims)   # what parse_game.py would produce (no db_log layer)

    # Store the per-death match flags ON each golden dict (NOT in a t0-keyed dict -- t=517 has TWO
    # distinct golden deaths, reo and Smurfette, which would collide and mask one).
    m_hn, miss_hn, spur_hn = score(golden, harness_deaths, "harness")
    for g in golden: g["_hn_ok"] = g["_hit"] is not None
    m_db, miss_db, spur_db = score(golden, db_deaths, "db_log")
    for g in golden: g["_db_ok"] = g["_hit"] is not None

    G = len(golden)
    print("=" * 78)
    print("PRODUCTION db_log DEDUP  scored against  VISION GOLDEN   (Sang's win, respawn-aware)")
    print("=" * 78)
    print(f"config: STICKY_ELO_CHAIN_MAX_ROWS={db_log.STICKY_ELO_CHAIN_MAX_ROWS}  CHAIN_GAP={STICKY_CHAIN_GAP_SECONDS}s"
          f"  SEED_LOOKBACK={STICKY_SEED_LOOKBACK_SECONDS}s  SIM={STICKY_SIM_THRESHOLD}  VIC_ANCHOR={STICKY_VIC_ANCHOR_SIM}")
    print(f"elim reads fed to db_log: {fed}   rows KEPT: {kept}   rows SUPPRESSED: {fed - kept}")
    print()
    print(f"                          distinct deaths   recall            precision")
    for label, deaths, m, spur in (("no db_log (parse harness)", harness_deaths, m_hn, spur_hn),
                                   ("through db_log dedup     ", db_deaths, m_db, spur_db)):
        O = len(deaths); M = len(m)
        rc = f"{M}/{G} = {100*M//G}%"
        pr = f"{M}/{O} = {100*M//O}%" if O else "n/a"
        print(f"  {label}      {O:>3}            {rc:<14}   {pr}")

    # --- the money view: deaths db_log LOSES that the parse harness KEEPS -------------------------
    # respawn deaths = 2nd+ death of a victim that dies more than once in the golden
    seen = {}
    for g in sorted(golden, key=lambda x: x["t0"]):
        seen.setdefault(norm(g["vic"]), 0)
        seen[norm(g["vic"])] += 1
        g["_death_ordinal"] = seen[norm(g["vic"])]

    lost = [g for g in golden if g["_hn_ok"] and not g["_db_ok"]]
    print(f"\n--- deaths the PARSE HARNESS captured but db_log DEDUP SUPPRESSED: {len(lost)} ---")
    for g in sorted(lost, key=lambda x: x["t0"]):
        tag = f"  (respawn death #{g['_death_ordinal']})" if g["_death_ordinal"] > 1 else ""
        print(f"     t={g['t0']:4d}  {g['vic'][:28]:<28} killed by {('/'.join(sorted(g['atk']))[:22] or '???')}{tag}")

    # Respawn spotlight -- the case the dedup risk is about. Distinguish THREE outcomes honestly:
    #   KEPT           : OCR captured it AND db_log kept it (dedup did no harm)
    #   db_log-LOST    : OCR captured it (harness has it) but db_log SUPPRESSED it -> a real dedup cost
    #   never-captured : OCR never got it (harness misses it too) -> not a db_log problem at all
    respawns = [g for g in golden if g["_death_ordinal"] > 1]
    r_kept = [g for g in respawns if g["_db_ok"]]
    r_dblost = [g for g in respawns if g["_hn_ok"] and not g["_db_ok"]]
    print(f"\n--- respawn re-deaths (victim dies >1x): {len(respawns)} total | "
          f"db_log KEPT {len(r_kept)} | db_log SUPPRESSED {len(r_dblost)} | never-captured-by-OCR "
          f"{len(respawns) - len(r_kept) - len(r_dblost)} ---")
    for g in sorted(respawns, key=lambda x: x["t0"]):
        if g["_db_ok"]:       status = "KEPT          "
        elif g["_hn_ok"]:     status = "db_log-LOST    "
        else:                 status = "never-captured "
        print(f"     [{status}] t={g['t0']:4d}  {g['vic'][:28]:<28} (death #{g['_death_ordinal']})")

    print(f"\n--- db_log SPURIOUS distinct deaths (survived dedup, no golden match): {len(spur_db)} ---")
    for o in sorted(spur_db, key=lambda x: x["t0"]):
        print(f"     t={int(o['t0']):4d}  {o['vic_disp'][:28]:<28} reads={o['n']}")

    # ============================================================================================
    # BOUNDARY #1: does a surviving row actually convert to ELO CREDIT?  (survival != rating)
    # The ELO path (match_detector -> elo_engine) does NOT respawn-collapse: it counts ROWS. But
    # total_kills/total_deaths are raw ROW counters, whereas the ELO RATING is placement-based
    # (pairwise survival order via elimination_order, which is PLAYER-KEYED). So duplicate rows can
    # inflate the displayed kill STAT while leaving the rating largely intact. Measure all three.
    # ============================================================================================
    from pathlib import Path
    from match_detector import detect_matches_from_db, get_player_survival
    from elo_engine import dedup_kill_rows   # the production stat-dedup fix, validated below

    # match_detector's EXACT ELO-feeding filter (Kill + both-name BleedOut). These are the rows that
    # become total_kills. ChampionEliminated is attacker-less -> excluded (no ELO credit), by design.
    elo_rows = conn.execute(
        "SELECT timestamp, attacker, victim FROM events "
        "WHERE (event_type='Kill' OR (event_type='BleedOut' AND attacker!='' AND victim!=''))"
    ).fetchall()
    elo_surv = [{"t": ts_to_t(r["timestamp"]), "vic": r["victim"], "atk": r["attacker"]} for r in elo_rows]
    elo_distinct = collapse(elo_surv)                 # respawn-aware -> distinct real deaths credited
    R, D = len(elo_rows), len(elo_distinct)

    # deaths represented ONLY by attacker-less elim rows (a death is captured, but NO killer gets ELO)
    atkless = [d for d in db_deaths if not d["atks"]]

    matches = detect_matches_from_db(Path(tmp))
    elim_players, credited_raw, credited_fix = set(), {}, {}
    for m in matches:
        eo, _ = get_player_survival(m)
        elim_players |= set(eo.keys())
        for k in m.kills:                              # OLD total_kills path: counts raw rows
            if k.attacker:
                credited_raw[k.attacker] = credited_raw.get(k.attacker, 0) + 1
        for k in dedup_kill_rows(m.kills):             # NEW total_kills path: respawn-aware de-dup
            if k.attacker:
                credited_fix[k.attacker] = credited_fix.get(k.attacker, 0) + 1
    total_raw, total_fix = sum(credited_raw.values()), sum(credited_fix.values())
    credited_kills = credited_fix                       # for the top-attacker display below
    golden_creditable = sum(1 for g in golden if g["atk"])   # golden deaths that HAVE a known killer

    print("\n" + "=" * 78)
    print("BOUNDARY #1  --  do db_log survivors convert to ELO CREDIT?  (survival != rating)")
    print("=" * 78)
    print(f"ELO-feeding rows kept (Kill + both-name BleedOut) : {R}")
    print(f"  -> distinct real deaths they represent          : {D}   (respawn-aware victim collapse)")
    print(f"deaths captured but attacker-less (NO killer credited): {len(atkless)}   "
          f"(victim death recorded, but no player gets the ELO kill)")
    print(f"\ntotal_kills STAT (elo_engine): OLD raw-row count vs NEW dedup_kill_rows() production fix")
    print(f"  OLD (raw rows)         : {total_raw}   (over-count {total_raw/D:.2f}x vs {D} distinct deaths)")
    print(f"  NEW (dedup_kill_rows)  : {total_fix}   (over-count {total_fix/D:.2f}x)  <- removes db_log's cap-2 dup rows")
    print(f"  residual above 1.0x is OCR NAME-JITTER (one death read with 2 attacker spellings, e.g.")
    print(f"  calamorii/calamoriii) -- a separate canonicalization problem (beads 1gn/op1), NOT db_log.")
    # The pairwise ELO rating is driven by elimination_order, which is PLAYER-keyed -- so db_log's
    # duplicate cap-2 rows cluster at the same victim/time and do NOT reorder distinct players (they
    # can't inflate the rating). That is the boundary-#1 answer. The count below is COVERAGE (a player
    # was eliminated SOMEWHERE), NOT proof the relative survival ORDER is correct -- see the caveat.
    print(f"\nRATING (pairwise, elimination_order is PLAYER-keyed -> cap-2 duplicates do NOT inflate it):")
    print(f"  players eliminated (coverage, not ordering): {len(elim_players)} vs golden deaths {len(golden)}")
    print(f"  CAVEAT: match_detector split this ONE game into {len(matches)} chunks (recursive stitch"
          f" splitter; only ~2 real >90s kill-gaps exist) -> pairwise comparisons are confined WITHIN"
          f" chunks, a SEPARATE rating distortion, orthogonal to db_log dedup and UNMEASURED here.")
    top = sorted(credited_kills.items(), key=lambda x: -x[1])[:6]
    print("  top credited attackers (NEW deduped kill count):")
    for a, c in top:
        print(f"     {a[:28]:<28} {c}")

    db_log.time = __import__("time")   # restore
    print(f"\n(temp DB: {tmp}  -- throwaway; elo.db / killfeed.db untouched)")


if __name__ == "__main__":
    main()
