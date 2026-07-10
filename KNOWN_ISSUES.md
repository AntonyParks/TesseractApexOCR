# Known Issues / Deferred Work

Running list of things flagged during development but deliberately not fixed at the time —
usually because they're not blocking, or because fixing them properly needs a decision the
user hasn't made yet. Add an entry whenever you'd otherwise just say "worth fixing later" in
conversation. Once something is fixed, strike the heading (`~~text~~ RESOLVED <date>`) and keep
a short summary of what was found and how it was fixed — don't delete outright unless the fix
is also captured in a git commit message; until then this file is the only record.

Each entry should have enough context that someone (or some future Claude session) can act on
it without re-reading old conversation history.

---

## Killfeed zone calibration ported Gemini → Claude vision (2026-07-05); success still timing-bound

The Gemini free tier that `calibrate_zone.py` used for region classification was retired, so
auto-calibration was dead. **Ported the one vision call (`_classify_regions`) to Claude** (Anthropic
SDK, `ANTHROPIC_API_KEY` from `.env`, model `config.CALIBRATE_VISION_MODEL = "claude-sonnet-5"`,
forced tool-use for structured output). Everything else (region derivation, tightening, re-verify,
caching, bypass) is model-agnostic and unchanged. The async OCR-correction queue in
`gemini_queue.py` is a SEPARATE system and was left on Gemini (still dead — different concern).

Validated 2026-07-05:
- Plumbing (smoke test, synthetic image): Claude tool-use returns valid structured labels. Note
  `temperature` is **deprecated** for claude-sonnet-5 — it must NOT be passed (400 error otherwise).
- Accuracy (real live frames, saved + eyeballed): on Demoniio mid-fight, Claude correctly labeled
  the 3 real killfeed lines `killfeed`, the "sub goal" overlay `hud_banner`, and the facecam
  `other_noise` — exactly the killfeed-vs-webcam/banner separation the generic box fails at. On a
  Gdolphn inventory-menu frame and a Clarouille5 frame (they'd switched to a different game), it
  correctly found NO killfeed (no hallucination).

**Live periodic calibration (implemented 2026-07-05, live since the 15:06 restart):** the old
blocking 45s-at-startup loop is replaced by a live in-loop poll. An unconfigured streamer starts
on the generic box; the OCR loop fires ONE Claude classification ~once a minute
(`AUTO_CALIBRATE_INTERVAL_SECONDS`), only on frames that already have killfeed candidates, until a
clean killfeed is caught — solving the "empty feed at sample time" timing problem (kills happen
eventually; we just wait for one). See `calibrate_zone.attempt_calibration_from_frame` +
`ocr.py` ChannelWorker loop.

**Two-strike confirmation (why single-frame isn't enough):** validation caught that on a sparse
feed the detected boxes can land on a FACECAM and Claude mislabels them `killfeed` (seen on Mande;
Dancingyoda's zone anchored below the real line). So a zone is only LOCKED after TWO independent
attempts (>= a minute apart) agree on an overlapping zone (`zones_overlap`), and only the confirmed
zone is cached. A one-off mislabel doesn't reproduce and never locks; the streamer just stays on the
generic box (no regression). Derived zones use a GENEROUS default height (kills stack downward, so a
too-tight y1 would silently drop a later burst) pulled up only by a contaminant actually below.
Confirmed working in production: 7 streamers auto-locked within minutes of restart (incl. Mllinze,
which used to fail with dead-Gemini, and Misterosey1 the #1 Predator).

## ~~Calibration lacks a COVERAGE guard — some zones mis-anchor and drop real kills~~ RESOLVED 2026-07-09

**Fix (2026-07-09):** added a geometric coverage guard in `calibrate_zone._derive_zone_from_regions`,
grounded in the invariant that the Apex killfeed always STARTS in the upper-right band (hand-verified
zones top out at y0=0.216). (1) Reject any candidate whose topmost killfeed region starts below
`KILLFEED_TOP_MAX_FRAC` (0.26) — that's the facecam/HUD-below mislabel that caused every measured
mis-anchor (Xcamorex y0=0.327, Fredstxr 0.416, etc.). (2) Drop killfeed regions sitting far below the
top line. (3) Cap zone height at `KILLFEED_MAX_SPAN_FRAC` (0.24) so a zone can't extend into the
facecam (fixes the too-tall Misterosey1/Angelicdomi cases). 10-case unit test
(`test_coverage_guard.py`) covers the exact measured mis-anchors + borderlines + height cap. The
36-zone cache was cleared (backup `calibration_cache.json.bak_pre_guard_20260709`) so every zone
re-derives with the guard, and `AUTO_CALIBRATE_ZONES` was re-enabled. Original finding below.

**Guard now LOGS every decision (2026-07-09, live-validation pass).** The original resolution left
guard rejections *silent* ("rejected facecam frames log nothing"), so a full run couldn't actually
confirm the guard live. Added `[Calibrate:{streamer}]` log lines in `_derive_zone_from_regions` at
each branch: GUARD 1 reject (topmost y0 > 0.26), GUARD 2 drop (regions below the band), GUARD 3
height cap (span > 0.24), and a `Derived zone ... guard PASS` line showing `y0<=0.26, span<=0.24` on
every accepted zone. This function is the SHARED derive path — the production live loop reaches it via
`attempt_calibration_from_frame` (ocr.py:623), so the guard is now observable during the real run,
not just in `--force`. `test_coverage_guard.py` (prev-session scratchpad) still passes with all
branches firing.

**Live-validation status:** offline reject-path fully proven (unit test, every branch logs). LIVE
reject-path on a *known previously-mis-anchored* streamer (Xcamorex/Fredstxr/Swole_Hokage/Kirubie/
Dinerosenseii) was NOT achievable 2026-07-09 — none were live in a ranked Apex match (Dinerosenseii
was streaming ARC Raiders). Live smoke test DID confirm: streams open + classify, the guard fires and
logs live (Mllinze reject-log observed), and the classifier correctly labels webcams as `webcam`
(zkmushroom) rather than the killfeed-mislabel Guard 1 defends against. Single-shot `--force` is a
poor live-repro tool: it must confirm ranked AND catch >=2 clean killfeed lines inside one 45s budget;
the production loop retries ~once/min over hours and is far more likely to exercise the accept path.
Definitive live reject-path proof is deferred to the full run (now that the log makes it visible) or
to re-running a mis-anchored streamer when they're live-in-ranked. NOTE: the `--force`
`_run_calibration` path can print "would be BYPASSED" on an unlucky sparse window (seen on Mllinze) —
this is NOT production behavior; ocr.py uses the non-bypassing two-strike path and just stays on the
generic box until two attempts agree.

## (original) Calibration lacks a COVERAGE guard — some zones mis-anchor and drop real kills (measured 2026-07-05)

First real generic-vs-calibrated measurement (Claude classification, currently-live streamers,
contamination counted only over active/killfeed-present frames):
- **Generic box on un-calibrated streamers: ~42% region contamination** (Fuhhnq 77%, Sgtslvr 50%,
  Enemyapex/Crooklol ~15%). Confirms the worst un-calibratable streamers are genuinely leaky and
  are reasonable BYPASS candidates.
- **Calibrated zones: contamination 21% → 13%** inside the zone (modest help) BUT **~41% aggregate
  killfeed coverage loss**. That headline is partly a metric artifact (wide killfeed lines whose
  left edge starts left of the zone's x0 get counted "outside" even though production detection
  would still catch the overlapping part), but partly REAL: some zones are mis-anchored. Confirmed
  from the cache — good zones anchor at `y0≈0.17` (Msheartattack: 0% coverage loss, 4% contam),
  but Xcamorex locked `y0=0.327` (killfeed is ABOVE it → 100% loss) and Misterosey1 `y0=0.243,
  span 0.243` (too tall, catches HUD below).

**Root cause:** two-strike confirmation only checks that two frames AGREE on a zone — not that the
zone actually CONTAINS the killfeed the generic box sees. So a facecam/HUD element that reads as
killfeed on two frames locks a confident, wrong zone that silently drops kills — worse than the
generic box (which has full coverage). Net: calibration is not yet trustworthy to rely on.

**Fix options (not yet done):**
1. Add a COVERAGE guard: before locking, run detection with BOTH the generic box and the candidate
   zone on the same frame; reject the zone if it misses killfeed the generic box found (and/or
   require the zone's killfeed regions to sit near the TOP of the generic-box killfeed span, not
   below it). This directly kills the Xcamorex mis-anchor case.
2. Tighten the generous-y1 default / require a contaminant actually below before extending down
   (fixes the Misterosey1 too-tall case).
3. Interim-safe posture: set `AUTO_CALIBRATE_ZONES = False` (full generic-box coverage) and instead
   do SELECTIVE bypass of the few high-contamination streamers (Fuhhnq-type) — trades a little
   contamination for zero dropped kills until the coverage guard exists.

## Inherent limit: for streamers whose facecam physically overlaps the killfeed's screen region
(Mande, Dancingyoda), no zone fully separates them — the generic box has the same problem. Those
tend to stay on the generic box (attempts disagree, never confirm), which is the safe fallback.
Monitor `calibration_cache.json` after new streamers appear; `python calibrate_zone.py <name>
--unblock` clears a bad cached zone to force re-calibration.

---

## ~~Persistent killfeed line re-read as many "kills" — OCR jitter defeats exact-tuple sticky dedup~~ RESOLVED 2026-07-05

Confirmed 2026-07-05 (user flagged it). A single on-screen killfeed line
(`kpaub9sing <kill_icon> [ofps] vesoson` on Clarouille5) was logged **11 times across 13 minutes**
(~once/min), not 11 kills. The sticky-line suppression in `db_log.py` keys on the EXACT
`(streamer, event_type, attacker, victim)` tuple, but EasyOCR garbles the names slightly on each
re-read (`kpaubgsing`/`kpaub9sing`/`kpaubssing`, `ofps vesoson`/`vesoson`), so every re-read is a
"new" tuple and no suppression chain ever forms. This inflates/fragments the leaderboard.

This is a DEDUP bug, independent of calibration and OCR accuracy.

**Fix (implemented 2026-07-05, live since the 15:06 restart):** the sticky-chain now clusters
inserts by a FUZZY signature — `db_log.py` `_line_sig()` normalizes `attacker|victim` to lowercase
alnum, and inserts within a `(streamer, event_type)` group whose signatures are `SequenceMatcher
>= STICKY_SIM_THRESHOLD` (0.82) similar share one chain, suppressed past `STICKY_CHAIN_MAX_ROWS`
(4). Deliberately NOT applied to the 20s exact dedup (fuzzy there could drop a real second kill
within the window); safe here because suppression only triggers after 4 continuous rows, which
distinct kills never produce. Tests: the original 8-case sticky suite still passes, plus a new
case proving 11 jittered reads collapse to 4 while 6 distinct-victim kills and a knock+finish pair
are all preserved. `reprocess.py --dedupe` still collapses any residual near-dup names after ELO.

**Refinement — span guard, event-type aware (built 2026-07-05, deploys on next restart):** raw
count-of-4 suppression risked dropping a legit `knock -> revive -> re-knock` burst (~0.45% of knock
clusters were legit 5-6-row bursts inside 150s). This only applies to KNOCKS: a player can't be
re-killed until they respawn (minutes), so Kill/BleedOut have no legit same-pair repeat inside the
window (verified — their >4 short-span clusters are all sticky; BleedOut had zero). So suppression
is now event-type aware: `STICKY_ELO_EVENT_TYPES` (Kill, BleedOut) stay STRICT (suppress past 4,
killing phantom ELO events fast); non-ELO (Knock etc.) only suppress recurrence SUSTAINED past
`STICKY_CHAIN_MIN_SPAN_SECONDS` (180s) with a `STICKY_CHAIN_HARD_CAP` (8) backstop — a short
in-fight burst survives, a multi-minute sticky line is still crushed. `test_span_guard.py` covers
all four cases.

---

## ~~`reprocess.py` without `--reset` silently compounds the leaderboard~~ RESOLVED 2026-07-05

**Fix (2026-07-05):** `reprocess.py` now **rebuilds from a clean slate by default** (the only
correct behavior, since every run replays ALL matches). The legacy stacking behavior is behind an
explicit `--append` flag that prints a loud COMPOUND warning when writing onto a non-empty DB.
`--reset` still works (now the default). Verified idempotent: running the default twice leaves
`matches_played` at small integers (max 7, not 14). `elo.db` was rebuilt clean — 505 players, 23
with 3+ matches, ELO 792–1197 (was falsely reporting 414/414 qualified). Stale DB backed up at
`elo.db.bak_pre_clean_rebuild_20260705`. Original finding below for the record.

Found 2026-07-05 while auditing the pipeline. `reprocess.py` with **no flag** (the documented
default: `python reprocess.py   # process all data → elo.db`) is **not idempotent**. It does not
wipe `elo.db` first, and `batch_reprocess` seeds each player's ELO/`matches_played` from the
*existing* DB row on first touch (`_get_or_default` → `get_player_rating`), then re-applies the
full ELO pass and `+1` per match appearance. So every re-run adds another pass on top of the
previous one. The `matches` table stays stable (idempotent upsert keyed on `match_id`), which
hides the problem — only `player_ratings` compounds.

Evidence in the committed `elo.db` at audit time:
- `matches_played` for every player was an exact multiple of ~30 (324 players at exactly 30; the
  rest 60/90/…/450, plus one 309 from passes accumulating as `killfeed.db` grew) → ~30 redundant
  passes had accumulated.
- `diagnose.py` reported **414/414 players "qualified" (3+ matches)** — meaningless, since the
  floor was 30.
- ELO spread was 181–1732 (distorted, not linearly inflated: `k_factor` is pinned to `K_LOW`
  once `matches_played ≥ 30`, so later passes damp rather than multiply).

A clean rebuild (`reprocess.py --reset --db elo_clean.db`, non-destructive, real `elo.db` left
untouched) produced the correct picture: **505 players, only 23 with 3+ matches, ELO 792–1197**,
`matches_played` as small integers (danykap=3, ireina=6). Backup of the stale DB kept at
`elo.db.bak_pre_clean_rebuild_20260705`; the clean rebuild is at `elo_clean.db`.

Not fixed here (per "detect issues, don't fix unless asked"). Options when the user wants to act:
1. Promote the clean rebuild: `mv elo_clean.db elo.db` (or just rerun `reprocess.py --reset`).
2. Harden the footgun: make the default `reprocess.py` imply `--reset` (or refuse to run onto a
   non-empty `elo.db` without an explicit `--append`), so `python reprocess.py` can't corrupt the
   board by being run twice. The docstring already documents `--reset` as the "from scratch" path,
   but nothing warns that the default *accumulates*.

Note: this only affects the ELO/leaderboard half. Match detection, merging, survivor placements,
and all collection-time parsing/OCR fixes are unaffected (verified: match-merge, sticky-chain, and
kill-icon unit tests all pass; OCR holdout benchmark 91.1% similarity / 16.4% CER).

---

## CJK / Cyrillic character-set decision needed

27.2% of rows (252/926) in `labels_clean.csv` contain characters outside the training charset
(confirmed via audit, 2026-07-02) — but this collapses to ~5 distinct recurring real names/tags
repeated many times each, not broad random noise:
- `KONRAD ER MIT NAVNNN <GUN_ICON> [DMA] 正義` (~17 variants, Chinese)
- `tiktok.com/rCreed <GUN_ICON> Дочь Шишки` (~11 variants, Cyrillic)
- `我想吃GREGGS <GUN_ICON> Nati` (~8 variants, Chinese prefix on a Latin name — confirmed correct
  via direct image inspection, not a mislabel)
- one `Seer.57↑` (arrow glyph)

These get silently stripped by `dataset.py`'s unconditional out-of-charset regex at training
load time (label truncated, image unchanged) — see the (now fixed) en-dash issue this was
originally grouped with. The model will never learn to transcribe these specific names. Not a
big deal in the current scope (~3 players), but if the streamer roster grows to include more
non-Latin-script names this will need a real decision.

**Status (2026-07-02):** on hold. User's call: don't touch this unless it's shown to be a real,
measured problem (e.g. shows up as a specific, attributable failure mode in a future
`eval_holdout.csv` benchmark), not just a theoretical charset gap. Nothing to build right now —
revisit only if concrete evidence of impact turns up.

**Data point (2026-07-03):** first measured evidence exists but user's call is still hold —
other things first. `pipeline_evaluator.py` on the full 94-row holdout shows `ä -> a` as the
single top character confusion (13 occurrences). Also observed in production: Cyrillic victim
names (`Дочь Шишки`) OCR'd to garbage (`do43 mukm`, `dgol mgra`) in the (junk, since deleted)
Mande 2026-07-02 session — garbling like this weakens fuzzy dedup/merge matching wherever
non-Latin names appear, independent of the sticky-line issue.

**Fix (if it does become worth pursuing):** expanding the character set to include CJK is a much
bigger lift than Latin diacritics (thousands of glyphs vs. a handful) — probably not worth it for
a few players. Cheapest partial fix: add common Latin-1 diacritics (ä, ö, ü, é, etc.).

## Remaining non-killfeed / garbled entries in the leaderboard after the 2026-07-03 cleanup

The `killfeed.db` cleanup (see the leaderboard-integrity entry below) deliberately used narrow,
well-justified criteria (confirmed-contaminated-zone legacy data + GUN_ICON leaks). A few
observed-bad entries didn't meet either criterion and are still there:
- `nerf` (3 matches, K=4) — from the recurring `nerf seer pllssssssss <GUN_ICON> Aurocs` line
  (`Matafe_`/`Zuni`, 2026-07-02), a non-killfeed banner misparsed as a Kill, not a real player.
- `atgreggs`, `kour`, `duourpgrua`, `mrfsersssssssa` and similar — plausible-looking but
  unverified; could be real (if unusual) player names or could be further OCR noise. Not
  investigated individually.
- `counler`, `biocc` and similar low-stat entries (added 2026-07-03) — likely the kept-first-4
  remnants of sticky-line chains (the sticky-chain cleanup/live fix deliberately keeps up to 4
  rows per chain, so a garbled sticky line can leave a phantom player with a few matches and
  single-digit K/D). Same caveat as above: no safe blanket rule without per-case checking.

**Why not cleaned:** no safe, generalizable rule distinguishes these from genuinely unusual real
player names without per-case investigation (unlike the GUN_ICON leak or the confirmed-bad-zone
criteria, which were well-evidenced). Deleting on a blanket heuristic here risks discarding real
data.

**Fix (if pursued):** add `"nerf"` to `parsers.py`'s `invalid_patterns` blocklist (well-evidenced,
low risk, consistent with the many similar entries already there) as a first, easy step. The rest
would need individual verification (check the underlying crop/match_kills rows) before deciding.

## ~~Persistent on-screen kill line generates repeat events for 30+ minutes~~ FIXED 2026-07-03

Post-run audit of the 2026-07-03 collection run (09:48-17:01, the first full run with the dedup +
GUN_ICON fixes live) confirmed the `elonmusk`/`miracle`-style pattern is real, streamer-agnostic,
and was the single largest data-quality issue: **75 (streamer, event_type, attacker, victim)
tuples repeated >=4x accounted for 694 excess rows — ~22% of the run's 3,085 events.** Worst case:
`mrspecter -> csl2 ghost` on Mande, 60 rows over 38 minutes. Crop inspection
(`crops/Mande/20260703_110935_line0_0009_raw.png`, `..._111718_line0_2124_raw.png`,
`..._114736_line1_222c_raw.png`) showed the same genuine, game-rendered line on screen across
the whole span — a persistent game-UI element (most plausibly the death/spectate-screen "your
killer" line), visually indistinguishable from a killfeed entry, re-admitted every time the 20s
dedup window lapsed.

**Important design constraint (user correction, 2026-07-03):** a long pair-level dedup horizon
would be WRONG. In Apex the same (attacker, victim) pair legitimately repeats: a knockdown and
its finisher are *separate* killfeed entries, and after a revive or respawn-beacon cycle the
pair can legitimately recur minutes later in the same match. The distinguishing signal is
**cadence**, not the pair: legit repeats come as short bursts (knock+finish) or minutes apart
(revive/respawn cycles); sticky lines recur continuously every 20-150s for many minutes.

**Fix (implemented, `db_log.py`):** chain-based suppression in `insert_event()`. Inserts of the
same tuple landing within `STICKY_CHAIN_GAP_SECONDS` (150s) of the previous one form a chain;
once a chain exceeds `STICKY_CHAIN_MAX_ROWS` (4), further inserts are suppressed. 4 rows still
comfortably fits knock + finish + a full respawn-cycle re-knock + re-finish; an unbroken
every-minute recurrence does not. Chain state is in-memory but seeded from a DB lookback
(`STICKY_SEED_LOOKBACK_SECONDS`, 600s) on a cold key, so worker restarts don't reset an active
chain. Unit-tested against a temp DB (8 scenarios incl. legit knock/finish/respawn, restart
resilience, 20s-dedup coexistence, per-event-type independence) — all pass.

**Retroactive cleanup (2026-07-03, backup: `killfeed.db.bak_pre_sticky_cleanup_20260703`):**
three passes, 10,730 -> 7,318 events (3,412 rows removed):
1. Exact-tuple chain pass mirroring the live fix: 1,923 rows across 229 tuples (incl. 65 of the
   69 remaining `elonmusk -> miracle` rows and 55/60 of `mrspecter -> csl2 ghost`).
2. Fuzzy pass (same chaining, `attacker|victim` string similarity >= 0.75 per ocr.py's
   `find_recent_match` threshold) for legacy old-model-era rows where garbling made each re-read
   a different tuple: 1,378 rows across 278 chains.
3. Scoped deletion of Mande's entire 2026-07-02 session (111 rows, 09:38-10:2x): one
   contaminated sticky/loop feed the whole time (same victim "killed" 6x in 90s; `seer3721` and
   `tiktok.com/rCreed` garbles vs Cyrillic victim names) — same precedent as the Apryze/Gent
   zone cleanup.

`elo.db` rebuilt (`--reset --dedupe`) after each pass. Final sanity: max single-attacker kills
in one grouped match dropped 46 -> 17 (plausible with knocks+finishes), no 100+ kill
mega-matches remain, phantom high-ELO entries (`powpamjam`, `se8r3721am`) gone. **Residual by
design:** each sticky chain keeps its first ~4 rows (matching live-fix semantics), so low-stat
phantom names (e.g. `counler`, 3 matches / K=4) can still appear — see the "Remaining
non-killfeed / garbled entries" entry.

## ~~Knockdowns counted as kills~~ FIXED 2026-07-04 (skull-based knock/kill distinction)

User-identified (2026-07-04): the killfeed shows a knockdown (`A [weapon] B`) and the actual
elimination (`A [weapon] [RED skull] B`) as separate lines; the gap heuristic collapsed both
into `<GUN_ICON>` -> event_type='Kill', so every knock counted as a kill in ELO. Confirmed
against real crops, including a clean knock->finish sequence 32s apart on the same victim
(`crops/Berbatow/20260703_151351...` weapon-only, then `..._151422...` weapon+red skull).
Measured on 1,846 crop-backed Kill rows: **only 34% had the skull — two-thirds of "kills" were
knockdowns.** A look-alike glyph, the ORANGE circular kill-leader badge, appears on both line
types and means nothing about elimination; it must not be confused with the red skull.

**Fix (implemented):**
- `ocr.py::detect_kill_skull` — color analysis of the icon gap in the original crop (thresholds
  calibrated on labeled crops; rejects the kill-leader badge, solid red UI bars, red-tinted
  weapon icons, and bails to knock on whole-gap red flashes where multi-read voting decides).
- `ocr_with_easyocr(..., color_img=...)` emits `<KILL_ICON>` for skull gaps, `<GUN_ICON>`
  otherwise; `find_recent_match` never merges across marker classes (a knock line and its
  finish line are different events).
- `parsers.py` maps skull lines to 'Kill', plain gap lines to 'Knock'
  (`config.KNOCK_KILL_DISTINCTION`, off = legacy behavior for TrOCR/Tesseract paths).
- 17-case test suite (knock, finish, badge, red-bar, red-flash, streamer-styled lines) passes.

**Retro data (backup `killfeed.db.bak_pre_knock_reclass_20260704`):** 1,846 crop-backed rows
re-classified with the live detector: 635 Kill / 1,206 Knock. 1,092 crop-less Kill rows were
marked **`KillUnverified`** — no evidence either way, so they're excluded from ELO (match
grouping reads 'Kill' only) but preserved; revert is one UPDATE. Going forward every line is
classified live (color is always available in the worker), so this pool never grows.

**Consequences / open points:**
- The verified-only leaderboard is THIN right now — expect it to fill in quickly once
  collection runs with live classification.
- ~~`BleedOut` rows not counted in ELO~~ **RESOLVED 2026-07-04, user decision: bleedouts ARE
  kill-equivalents.** `match_detector.py` (both DB and CSV loaders) now includes BleedOut rows
  with BOTH names present ("Knocker [Bleed Out] Victim", exactly how the game credits it).
  Single-sided BleedOut rows stay excluded: they're ambiguous between a truncated read and an
  unattributed ring/fall death where the lone name is the VICTIM in the attacker field —
  crediting those would hand kills to dying players.
- Including bleedouts surfaced two follow-up fixes, both done: (a) `[Bleed Out]` tag fragments
  and clan-tag-only names (`int[Bleed`, `[x78]`) leaking into player names — now rejected in
  both `parsers.is_invalid_player_name` and `elo_engine._is_valid_player`; (b) **Zaine's entire
  dataset (488 rows, all from 2026-07-02 19:00-23:00) deleted** — at least four independent
  sticky lines confirmed in that one session (`[x78] elonmusk dick [Bleed Out] m...`,
  `rrodya -> momentum`, `keepgoingstepbro [Bleed Out] [LIVE] ttv kayomi` — that last one is
  literally a stream overlay, not killfeed), same whole-stream contamination precedent as
  Apryze/Gent/Mande-07-02.
- Match grouping now keys on far fewer events, so match boundaries/session stats shifted vs
  the old inflated boards.

## Predator leaderboard ground truth: auto-refresh opens a visible browser window

`apex_ranked_leaderboard.csv` (Predator names seeded as protected 'pro' entries at pipeline
start) is refreshed by `update_leaderboard.py` from apexlegendsstatus.com. Refreshed
2026-07-05: 746 players, 632 of them new vs the stale previous list — the board rotates
fast, hence the periodic design: `ocr.py` runs the refresh at startup (right before pro
seeding, which is the only moment the CSV is read), age-gated to
`LEADERBOARD_MAX_AGE_HOURS` (24h), sanity floor of 200 rows before overwriting, dated
backup kept, any failure falls back to the existing CSV. Toggle:
`LEADERBOARD_AUTO_REFRESH` in config.py.

**The quirk:** the site is behind Cloudflare, which permanently blocks headless browsers
(verified: "Just a moment..." forever in `--headless=new`; plain requests/WebFetch get 403).
The scraper therefore drives a HEADED Edge window via Selenium — it flashes on screen for
~10-15s once per day at pipeline start. Data extraction is trivial once past the challenge:
the page is a client-side DataTable with all ~750 rows in memory (`page.len(-1)` exposes
them). If the window flash ever becomes a problem, the clean alternative is the site's
official API (api.mozambiquehe.re) with a free registered key — swap `scrape()` for an API
call and headless goes away entirely.

Also hardened `database.py::load_top_players` to parse the CSV with `csv.reader` (the old
naive `split(',')` would silently corrupt any player name containing a comma).

## Cross-streamer match merging + survivor placements (built 2026-07-04)

Two features added per user direction, plus one structural grouping change they forced:

1. **Cross-streamer lobby merging** (`match_detector.py::merge_cross_streamer_matches`):
   matches from different streamers merge into one when they share >= 3 events
   (fuzzy `attacker|victim` similarity >= 0.75, <= 60s apart). Twitch delay differs per
   stream, so the median offset over shared events aligns the secondary's timeline before
   interleaving; shared kills dedup (keeping max confidence per side); `matches.merged_from`
   records provenance. Fixes double-rating (one real lobby watched by two streamers used to
   rate every shared kill twice) and widens lobby coverage. Fully covered by synthetic tests
   (incl. transitive A~B~C and fingerprint-driven merges).
2. **Knock/unverified events as fingerprints + window glue**: knocks render identically on
   every stream watching the lobby and are ~3x more numerous than confirmed finishes, so
   they now (a) hold match windows together during gap-grouping — confirmed finishes are
   routinely > 90s apart in a real game, which fragmented matches — and (b) serve as merge
   fingerprints. They still never touch ELO. Relatedly, the MIN_KILLS floor now applies
   AFTER merging, so a thin single-stream view (1-2 finishes) survives when another
   stream's view corroborates it.
3. **Survivor placement rows**: `match_placements.survived=1` rows for attacker-only
   players — `kill_order_out` is then their survival FLOOR ("alive at least until kill N"),
   distinct from an elimination position. Verified live: 274 survivor rows, 204 with real
   ELO gains (e.g. danykap +112 for outlasting 7 eliminations).

**Caveat:** the one known historical shared lobby (Zuni/Matafe_ 2026-07-02 09:38-09:44) does
NOT merge — its overlap turns out to be mostly shared sticky-line remnants (`nerf seer
pllssssssss|Aurocs` banner + a 12-minute `ayakowalski|hattiwari` line visible on BOTH
streams), with only ~2 genuine shared kills, below the threshold. That's the conservative
criteria working as intended on degraded old-model data; expect real merges on fresh
collection (dense, clean knock fingerprints). Watch the `[merge]` log lines on future
reprocess runs to confirm.

**Residual noise made more visible (pre-existing, not new):** garbled variants of an
attacker name (e.g. `twltol`/`qdolpi`/`ptphn` for `twitch gdolphn`) each get their own
survivor placement row and matches_played increment when `--dedupe` similarity can't bridge
the garble. Same category as the `oiphn` leaderboard entries.

## Gemini validation queue is effectively dead on the free tier (20 requests/DAY)

Confirmed during the 2026-07-03 run audit: free tier for `gemini-2.5-flash` is 5 RPM **and 20
requests per day** (AI Studio dashboard, 2026-07-02 — noted in `config.py` but the consequence
wasn't followed through). The run enqueued ~1,535 Kill events for validation; **only ~4 got
validated all day.** Auto-calibration (`calibrate_zone.py`) shares the same budget and attempted
~15 classification calls across 17 uncached streamers at startup — 11 failed ("Gemini
classification failed" / inconclusive), burning most of the daily quota in the first minutes and
starving the validation queue for the rest of the day (0/17 calibrations accepted this run).

Compounding it: **every failure path is silent.** `_call_gemini_api()` returns `None` on any
non-429 error, the 429 path re-queues with 60s backoff without printing, and the queue's stats
line only prints when `validated > 0` and the queue goes idle — so a fully-starved queue logs
*nothing at all* (confirmed: zero `[Gemini]` lines in 7 hours of run log).

**Fix options:** (a) pay tier / different key; (b) accept the free tier as calibration-only —
set `GEMINI_VALIDATE = False` so the 20 RPD go to zone calibration, which is the higher-value
use; (c) keep both but gate the queue to a token budget (e.g. 10/day) and log 429/quota state
loudly either way. Whichever is chosen, add a visible log line when the queue hits sustained
429s — silence here cost a full day of assumed-but-absent validation.

**Status (2026-07-03, user decision):** leave as-is for now. Validation not running is
acceptable since collection accuracy improved with the word-level model — this entry stays as
the record. Revisit if a use case for the validation labels (e.g. another training round) comes
back.

## Minor: `get_name_confidence_score` ignores the passed timestamp

`database.py::get_name_confidence_score` computes `time_since = time.time() - entry["last_seen"]`
using the real wall clock, not the `timestamp` parameter that `find_best_canonical_match` and
callers thread through everywhere else. Found while investigating the near-duplicate-player
merging issue (2026-07-03) — not confirmed to cause any actual bug. Doesn't matter for the live
pipeline (`timestamp` ≈ `time.time()` there anyway), but would matter for a hypothetical replay/
reprocessing tool that re-runs `PlayerDatabase` logic against historical timestamps, where
confidence scores would be computed relative to *now* instead of the event's actual time.

**Fix (if pursued):** `time_since = (timestamp or time.time()) - entry.get("last_seen", 0)`,
accepting an optional `timestamp` param matching the pattern used elsewhere in the class.

## pipeline_evaluator.py's OCR check can false-alarm from sampling noise

`evaluate_ocr_accuracy()` draws an unseeded `random.sample()` of `--sample` rows (default 20)
from `eval_holdout.csv`'s 94 killfeed rows each run. Observed directly (2026-07-03): a 30-row
draw read CER at 20.25%, tripping the `> 0.20` FAIL threshold, while the full 94-row population
(no sampling) read 15.16% — identical to the original deployment benchmark, i.e. no real
regression, just a harder-than-average random subset. With a small population (94) and no fixed
seed, this kind of threshold-crossing false alarm is expected to recur occasionally.

**Fix (if pursued):** fix a seed for reproducibility, and/or default `--sample` higher (or to the
full population, since 94 rows is cheap to run every time) so single-run noise doesn't trip the
FAIL threshold. Low priority — when this fires, cross-check with `--sample 94` before trusting it.

## ~~Ground-truth label errors in `labels/gemini_corrections/`~~ AUDITED 2026-07-02, narrow and bounded

Full audit done: sampled 15 of 32 non-empty-label `gemini_corrections/` rows, viewed 8 crops
directly. Result is **isolated to the `gent` subdirectory specifically, not the label source in
general**:
- 4 of 5 viewed `gent` crops were wrong — including `128 lob` (actual: `realodd`), `realodon`
  (actual: `realodd`), an extra hallucinated `<GUN_ICON>` on a transition-frame crop, and one
  case where an unrelated XP popup got merged into an adjacent line's label
  (`XP Input spotted an enemy.` when only `Input spotted an enemy.` was the real line).
- 3 of 3 viewed non-`gent` crops (`nati`, `zuni`, `matafe_`) were exactly correct.

This lines up with `gent` already being flagged earlier in the project's history as having badly
contaminated zone detection (33% real content) before being removed from
`STREAMER_SEARCH_ZONES` — these are very likely the same transition/overlap frames that caused
that, now showing up as bad Gemini-correction labels rather than bad crops.

**Scope:** all 19 non-empty-label `gent` rows in `labels_clean.csv` come from
`gemini_corrections/` (~2% of the 926-row training-eligible dataset). The word-level model still
beat stock despite this contamination, so it wasn't a fatal issue, but removing these ~19 rows
before the next training round is a cheap, well-evidenced improvement.

**Fix (if pursued):** exclude `gent` rows from the next training data build (e.g. filter in
`prepare_wordlevel_dataset.py` / `label_crops.py`'s `_collect_crop_paths`), or spot-check and
hand-correct the 19 rows individually since the population is small enough to do by hand.

## ~~`models/easyocr_custom/` has accumulated backup/candidate files~~ RESOLVED 2026-07-02

The word-level retrain (fixed the granularity + polarity mismatches, see
`prepare_wordlevel_dataset.py`) beat stock on `eval_holdout.csv`: **89.4% parsed-correct vs
stock's 83.0%** (similarity 0.912 vs 0.895, CER 0.152 vs 0.167). Deployed as `apex.pth`.
Losing candidates (old broken finetune, full-line candidate) deleted; one clearly-labeled
backup (`apex.pth.bak_old_finetune_beaten_by_wordlevel_20260702`) kept.

## ~~Leaderboard integrity: event dedup, GUN_ICON leak, near-dup players~~ FIXED 2026-07-03

Found by actually looking at a freshly-rebuilt leaderboard rather than trusting it. Three real
bugs, root-caused against live `match_kills` rows, not guessed:

1. **Same visible kill banner logged as a new event repeatedly.** `ocr.py`'s in-memory
   `event_tracker` groups repeated OCR reads of one banner within `EVENT_WINDOW` (`config.py`,
   was 3.0s) and flushes once no new matching read arrives. Killfeed banners routinely stay
   visible longer than that (revive sequences, queued kills), so the same banner gets flushed,
   written, and re-detected as "new" on the next read, repeatedly, for as long as it's visible.
   Confirmed: one revive banner produced 109 separate DB rows over 44 seconds (`nati` victim,
   `Nati_1782998720` match). Fixed two ways: `EVENT_WINDOW` raised to 6.0s (reduces how often
   this triggers), and a DB-layer safety net added in `db_log.py` (`DEDUP_WINDOW_SECONDS = 20`)
   that skips inserting a (streamer, event_type, attacker, victim) tuple already logged within
   the last 20s — this is the real guarantee, independent of in-memory window tuning or worker
   restarts.
2. **`<GUN_ICON>` marker leaking into name fields as `un_icon`.** The marker is a tiny weapon-icon
   graphic, not real text, so OCR occasionally drops a character (e.g. the leading "g"). The
   existing filter in `parsers.py::is_invalid_player_name` required both `"gun"` and `"icon"`
   substrings, so a partial read like `un_icon` slipped through. Broadened to reject any short
   (≤12 char) name containing `"icon"` — not a plausible substring of a real Apex player name.
3. **Near-duplicate identities not merged** (e.g. `yayakowalski` split across `ayakowalski` /
   `aakowalski` / `eyeakomatskia`, each with separate ELO/match history). Not a code bug —
   `reprocess.py` has a `--dedupe` flag (connected-component clustering, hybrid similarity
   threshold) built exactly for this, it just wasn't passed on the `--reset` rebuild. Verified
   it correctly merges `ayakowalski`/`aakowalski` (ratio 0.95) while correctly leaving
   `eyeakomatskia` alone (ratio 0.61-0.67, too dissimilar — merging it would risk false-positive
   merges of genuinely different players elsewhere). Always pass `--dedupe` on reprocess runs.

**Not fixed at the time — since investigated:** a structurally different pattern where
`event_type='Kill'` repeats for the same (attacker, victim) pair *many* times across minutes
(e.g. `elonmusk` → `miracle`, 222 rows, 2026-07-02). Crop inspection done 2026-07-03 — see the
open entry "Persistent on-screen kill line generates repeat events for 30+ minutes" above for
the confirmed mechanism and fix direction.

**Legacy data cleanup (2026-07-03):** ran a one-time retroactive cleanup on `killfeed.db` since
the fixes above only prevent *new* bad rows, not already-stored ones. Removed 5,110 of 12,823
rows (40%):
- 1,091 rows: pre-2026-07-02 events from `Apryze`/`Gent` (confirmed-contaminated zones, removed
  from `STREAMER_SEARCH_ZONES` that day) plus any-date GUN_ICON-fragment leaks.
- 4,019 rows: retroactive application of the dedup logic (same (streamer, event_type, attacker,
  victim) tuple collapsed if within 20s of the previous kept occurrence) — far larger than the
  single `nati` example suggested; over a third of all logged events were dedup-bug duplicates.
Backed up first to `killfeed.db.bak_pre_cleanup_20260703`. The `elonmusk`/`miracle` pattern above
was deliberately left untouched — 20s is much too short a window to have affected it either way.
