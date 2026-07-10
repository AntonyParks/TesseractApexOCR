# Design: Persistence-Aware Icon Vote (kill/knock)

> **Tracking moved to Beads (2026-07-10).** Actionable rollout is beads issue
> **`TesseractApexOCR-1t4`** — currently **blocked by** `TesseractApexOCR-1gn` (name canonicalization),
> matching the calibration verdict below. This file remains the design reference; track status via
> `bd show TesseractApexOCR-1t4`, not by editing here.

Status: **IMPLEMENTED, gated OFF** (2026-07-09). Layer 1 (`ocr.py`: `_icon_vote`,
`_apply_icon_decision`, `find_recent_match`, `flush_old_events`), Layer 2 (`db_log.py`:
`insert_event` group_key + `_seed_cluster`), flags in `config.py`. Unit test:
`scratch/test_icon_vote.py` (20 checks, all pass). Behavior is unchanged from legacy while
`ICON_VOTE_ENABLED=False` / `STICKY_CHAIN_MERGE_TYPES=False`. `ICON_VOTE_LOG=True` (default) emits
`[IconVote:...]` instrumentation lines with NO behavior change — that is the calibration-data
source. **Still pending before enabling: run instrumented, tune `ICON_KILL_MIN_RUN`/`_FRAC` on
labeled data (rollout steps 2-5 below).**

## ⚠️ Measured context — this is the SMALLEST of three levers

Decomposition of this-run ELO inflation (Kill+BleedOut). **61% of ELO rows (112/185) are
inflation.** By dominant cause:

| Cause | Share of ELO inflation | Fixed by |
|---|---|---|
| pure sticky (same Kill/BleedOut repeated) | **54%** | Layer 2 cap tuning (see below) |
| name jitter (same line, diff spellings) | **29%** | name canonicalization (separate) |
| icon flip → phantom Kill | **18%** | **this doc (Layer 1)** |

So the icon-vote addresses ~18% of ELO inflation. It is worth building — it is the only lever
that fixes the *label* (correctness, not just count) — but it should NOT lead. Recommended
priority for ELO correctness:
1. **Sticky-cap tuning for ELO types** (54%): the `Kill/BleedOut` strict path currently
   suppresses only past `STICKY_CHAIN_MAX_ROWS=4`. An *exact* same-tuple ELO repeat within
   150s is almost always sticky (you can't re-kill the identical-spelled victim that fast), so
   a much lower cap for exact ELO repeats is the biggest single win. Weigh against the
   deliberate anti-over-suppression tuning ([apex_killfeed_mechanics]).
2. **Name canonicalization** (29%): fold jittered spellings to one identity (reuse leaderboard
   name-matching). Fixes dedup fragmentation AND leaderboard mis-credit.
3. **This icon-vote** (18%): correctness fix for the phantom-Kill label.

All three are facets of one root cause: **line identity computed from noisy OCR.** Sequence by
measured contribution.

## Problem

A single sticky killfeed line, re-OCR'd many times, produces **phantom `Kill` events**
that feed ELO. Root evidence (this-run data):
- Zkmushroom `boutwork → arcticfox20023`: 8 DB rows from **one knock line**; all 4 saved
  crops show a **knock icon**, zero kill-line crops → the 4 "Kills" are false positives.
- ~29% of this-run `Kill` rows are respawn-implausible repeat-kills of the same pair.

## Why current code can't fix it (two structural splits)

1. **Merge layer refuses to cross the icon marker** — `find_recent_match`
   [ocr.py:403-408]. A `<kill_icon>` read and a `<gun_icon>` read of the *same physical
   line* are forced into **separate event tracks before voting**. A single false-positive
   skull read (`detect_kill_skull`, [ocr.py:90]) spawns its own track → phantom Kill. The
   correct knock-reads can't outvote it; they're in a different bucket.
2. **db_log sticky chain keyed by `(streamer, event_type)`** [db_log.py:293]. Kill-reads
   and knock-reads of one sticky line accumulate in **two separate chains**, each under its
   own 4-row cap → cross-window sticky repeats never suppressed.

`EVENT_WINDOW = 6.0s` [config.py:301] means the merge only ever sees one ~6s burst; a line
spanning minutes emits one event per burst, so both layers matter.

## The discriminator

- **True elimination:** the red skull is genuinely present for the elimination line's whole
  ~6s → a **sustained/contiguous run of kill-reads**.
- **False positive (flip):** `detect_kill_skull` trips on an odd frame → **sparse, isolated**
  kill-reads scattered among a dominant knock-read population.

So the vote must use **sustained consensus**, not global majority (a fast finish's knock
reads must not be able to outvote a genuine trailing kill block).

## Design — two coordinated changes

### Layer 1 — merge-layer icon consensus (fixes the LABEL)

Location: `find_recent_match`, `pick_best_variant` / `flush_old_events` in ocr.py.

1. **Line identity ignores the icon.** Normalize the marker to a neutral `<ICON>` token when
   forming the tracker key, so kill-reads and gun-reads of the same name-pair share ONE
   track. Keep each variant's *original* marker in its stored text (already the case — variants
   hold `(now, text)` with the real marker), so we can tally at flush.
2. **Delete the hard `continue` on marker mismatch** [ocr.py:407]; match on name-pair
   similarity alone.
3. **At flush, decide the event's marker by a sustained-kill rule**, not char-voting the
   token. Over the track's time-ordered reads:
   - `Kill` iff there is a contiguous run of `>= ICON_KILL_MIN_RUN` kill-reads AND kill-reads
     are `>= ICON_KILL_MIN_FRAC` of the track (guards against a stubborn 2-3 frame false
     positive).
   - else `Knock`.
   Write the chosen marker into the merged canonical text so `has_kill_marker`
   [parsers.py:456] yields the right `event_type`.
4. **Optional (knock→finish inside one 6s window):** if the track has a leading gun-block AND
   a trailing kill-block (clean monotonic transition), emit BOTH a Knock and a Kill. Default
   OFF — for ELO the Kill alone is sufficient and simpler; enable only if knock-stat
   completeness is wanted. (Cross-window knock→finish is already two tracks → two events, no
   change needed.)

### Layer 2 — db_log chain merge (fixes the residual COUNT)

Location: `insert_event` [db_log.py:293-322].

1. Key the sticky chain by `(streamer, name_pair_sig)` — **drop `event_type` from the key** —
   so kill-labeled and knock-labeled bursts of one sticky line accumulate into ONE chain.
2. Keep the existing cap logic. A real knock→finish is 2 rows (under cap → both kept); a
   sticky line exceeds the cap and gets suppressed. This backstops any burst Layer 1 still
   mislabels.

## New config params (all tunable, default behind a flag)

```
ICON_VOTE_ENABLED      = False   # master flag for staged rollout
ICON_KILL_MIN_RUN      = 3       # contiguous kill-reads required to call it a Kill
ICON_KILL_MIN_FRAC     = 0.50    # kill-reads as fraction of the track
EMIT_KNOCK_AND_KILL    = False   # Layer 1 step 4 (knock→finish within one window)
```

## ⛔ Calibration result 2026-07-09 — DO NOT ENABLE Layer 1 yet (blocked on line-identity stability)

Instrumented run (`ICON_VOTE_LOG`, vote OFF) measured:
- **Merge-reachability 22%** — only 22% of Kills have a same-pair Knock within 6s, so the
  cross-marker merge reaches almost no phantoms.
- **78% of kill tracks have `reads=1`, 89% ≤2** — almost no read population to vote on.
- Detection is healthy (22k active frames, ~0% blank; OCR runs every `FRAME_PROCESS_INTERVAL`
  =0.5s → ~10-12 reads per ~6s line). So `reads=1` is **fragmentation**: one line's reads
  scatter across many single-read tracks because OCR text jitter defeats the 0.75 grouping in
  `find_recent_match`. (Note: the vote-OFF instrumentation only sees the kill-side of each
  marker-split, so `mixed=0` is a barrier tautology, not evidence of no flips.)

**Verdict:** enabling the sustained-run rule now labels `reads=1` kill tracks as Knock →
drops real kills wholesale. The vote premise (read a line many times, then vote) is broken
UPSTREAM: reads don't coalesce into one track. **Precondition for enabling Layer 1 = stable
line identity (name/text canonicalization), which is the 29% lever and unblocks ALL voting
(icon + char). Do #1 (canonicalization) and #2 (Layer-2 sticky-cap, read-count-independent)
first.** Keep this code gated OFF until reads coalesce.

## Calibration — REQUIRED before enabling (do not ship blind)

Per-read icon distributions are **not in the DB** (only one marker per emitted event), so the
thresholds above are guesses. Before enabling:
1. Instrument the merge layer to log, per flushed track: `n_reads`, `n_kill`, `n_gun`, the
   longest kill-run, and the final decision.
2. Run live for a session; hand-label a sample of tracks (crop + VOD) as real-kill /
   phantom / knock→finish.
3. **Measure the kill-run-length distribution for KNOWN-REAL eliminations, not just phantoms.**
   The rule is asymmetric: today's bias is phantom *extra* Kills; `ICON_KILL_MIN_RUN` too high
   flips that into **dropped real Kills** (a fast thirst whose line was read only twice → labeled
   Knock → real ELO point lost). The floor must sit *below* real kills' typical run length.
4. **Verify false positives are transient.** The sustained-run rule assumes `detect_kill_skull`
   FPs are isolated odd frames. If some knock line has a visual feature that trips the skull
   detector for several *consecutive* frames, run+frac both pass → sustained false Kill.
   Check the run-length distribution of confirmed-phantom lines to confirm FPs are isolated.
5. Tune `ICON_KILL_MIN_RUN` / `ICON_KILL_MIN_FRAC` to best separate the labeled classes. Only
   then flip `ICON_VOTE_ENABLED`.

## Edge cases / risks

- **Fast finish, knock-reads outnumber kill-reads globally:** handled — sustained-run rule,
  not majority, so a genuine trailing kill block still wins.
- **Real respawn re-kills of same pair:** chain gap (150s) resets between engagements; cap
  protects up to 4. Unchanged tradeoff.
- **Name jitter fragmenting identity** (Xkaysi: attacker read 9 ways) still splits both the
  merge track and the chain. That is a **separate** problem (name canonicalization) — this
  design does not solve it; note it as follow-up.
- **detect_kill_skull precision** is complementary: fewer false positives = fewer flips to
  vote out. Worth a separate pass, not required by this design.

### Cases Layer 1 does NOT fix (scope honesty)
- **Pure sticky *real*-kill line** (all kill-reads, sustained run): Layer 1 correctly keeps
  labeling it Kill; only Layer 2's cap catches the repeats, and that still leaks up to 4. This
  is the 54% bucket — a Layer-2 story, not solved here.
- **BleedOut** (also ELO; the B0Fs case): it is *text*-marked (`[Bleed Out]`), not icon-marked,
  so Layer 1 ignores it entirely — it rides on Layer 2. Confirm the Layer-2 key-merge
  (`streamer, name_pair_sig`, dropping `event_type`) is intended to group across
  Kill/Knock/**BleedOut**: a knock→bleedout is one real elimination, so merging is probably
  right — but state it explicitly when implementing.
- **Within-window knock→finish loses the knock event** after barrier removal (one track →
  Kill only). Quick finishes are *common*, not an edge case. If anything downstream counts
  knocks, `EMIT_KNOCK_AND_KILL=True` is required to avoid a knock-stat regression.

## Rollout order

1. Instrument (Layer 1 logging) — no behavior change.
2. Measure + tune thresholds on labeled data.
3. Enable Layer 1 behind `ICON_VOTE_ENABLED`; verify phantom-Kill rate drops without losing
   real kills (re-run the repeat-kill metric).
4. Enable Layer 2 chain merge; re-verify.
5. Rebuild ELO with `reprocess.py --reset`.
