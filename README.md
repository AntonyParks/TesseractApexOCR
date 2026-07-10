# TesseractApexOCR: Master Guidance & Documentation

This document provides a comprehensive overview of the **TesseractApexOCR** project structure, architecture, running commands, database schemas, and critical engineering rules. **AI LLMs should refer to this document before making any changes.**

---

## 📌 Project Overview
TesseractApexOCR is a real-time, multi-channel Twitch stream capture and parser for **Apex Legends killfeeds**. It locates the killfeed region on screen, extracts text via OCR, groups events chronologically into match sessions, normalizes player names, and computes career ELO ratings based on match survival order.

**OCR engine**: `EasyOCR` (fine-tuned, `config.py` → `USE_EASYOCR=True`) is the active engine. A fine-tuned `TrOCR` model also exists (`models/trocr_apex/`, `config.py` → `USE_TROCR`) and historically measured ~97% avg string similarity on an older, now-deleted ground-truth set — that number is unverified against current data. A fresh, human-verified benchmark of the current EasyOCR model measured ~67% avg similarity. **This is an open, unresolved decision, not a settled one** — EasyOCR is active mainly because it was already mid-flight when this was last touched; revisit before trusting production accuracy numbers. `pytesseract` remains as a last-resort fallback.

---

## 🚀 Running Commands

### 1. Live OCR Stream Capture
Launches streamlink workers to watch configured Twitch channels, detect killfeeds, and record events:
```powershell
# Recommended PowerShell startup script (automatic rotation and recovery)
powershell -ExecutionPolicy Bypass -File start.ps1

# Direct python execution
.venv\Scripts\python.exe ocr.py --channel faide,sang,apryze
```

### 2. Visual Crop Viewer
FastAPI web server serving a custom single-page app at `http://localhost:8081`. Allows browsing every event, crop image, and raw frame comparison:
```powershell
.venv\Scripts\python.exe viewer.py --port 8081
```

### 3. ELO Reprocessing & Deduplication
Wipes the ELO database, groups killfeed logs into matches, re-runs ELO, and merges near-duplicate spelling variants:
```powershell
# Reprocess all matches and deduplicate player ratings
.venv\Scripts\python.exe reprocess.py --reset --dedupe
```

### 4. System Diagnostics
Runs data integrity, parsing quality, Gemini correction statistics, and ELO outlier health checks:
```powershell
.venv\Scripts\python.exe diagnose.py
```

---

## 🛠️ Architecture & Key Components

1. **`ocr.py`**: Stream worker manager (`ChannelWorker`, one thread per streamer). Demuxes Twitch live frames using `pyav`, resolves a killfeed search zone (hardcoded → auto-calibration cache → generic fallback, see `detect_killfeed.py` below), pre-processes the frame per active engine, runs OCR, and saves raw/processed crops.
2. **`parsers.py`**: Tokenizes text segments, cleans special characters, resolves gun-icon separations, and parses actions (Kills, BleedOuts, Revives, CarePackages, Pings).
3. **`database.py`**: Implements the player name normalization database (`player_names.json`) with temporal tracking (`recent_names`) to group typo variants under a canonical name.
4. **`detect_killfeed.py`**: Scans video frames to find killfeed line bounding boxes using brightness row/column projection. Search area is resolved per streamer, in priority order: `config.py`'s hand-calibrated `STREAMER_SEARCH_ZONES` → `calibrate_zone.py`'s auto-calibration cache → a loose generic fallback box. A fixed-position Apex HUD exclusion mask (squad counter) is zeroed out of the brightness map before line detection runs.
5. **`calibrate_zone.py`**: For a streamer with no hardcoded zone, runs a bounded, self-verifying Gemini-vision loop the first time they're seen: propose a zone from real detected regions → classify each region (killfeed / webcam / chat / hud_banner / ping_marker / menu_loading / other_noise) → tighten and re-verify with fresh samples → cache the result in `calibration_cache.json`. If contamination can't be resolved within the round/time budget, the streamer is **permanently bypassed** (not OCR'd at all) rather than falling back to a known-noisy zone — see `get_search_zone()`'s docstring and the `BYPASS` sentinel. Standalone CLI: `python calibrate_zone.py <channel> [--force] [--unblock]`.
6. **`gemini_queue.py`**: Async background-thread queue that validates TrOCR/EasyOCR output against Gemini (rate-limited to 15 RPM, shared with `calibrate_zone.py` via `GeminiValidationQueue.call_sync()` so the two never combine to exceed the budget) and collects training data (`labels/gemini_confirmed/`, `labels/gemini_corrections/`).
7. **`elo_engine.py`**: Dynamic career ELO calculator based on survival hierarchy (outlasting other players in a match).
8. **`elo_db.py`**: SQLite adapter for ELO metrics, matches, and career placement logs.

---

## ⚠️ Critical AI Constraints & Systemic Rules

Any developer AI working on this codebase **must** adhere to these strict logical invariants:

### 1. Double-Line Crop Splitting (Anti-Garbage Rule)
* **Rule**: Bounding boxes taller than `_MAX_LINE_HEIGHT` (55px) must attempt `_force_split_tall_region()` **before** being rejected for exceeding the height limit, not after. `detect_killfeed_regions()` must never `continue`/drop an oversized cluster outright — it must hand it to the splitter first and only reject the resulting sub-regions individually if they fail the width/banner filters.
* **Why**: An earlier version rejected oversized clusters before the splitter ever ran, silently discarding merged multi-line killfeed (or killfeed-merged-with-webcam/overlay) content instead of separating it — a real, previously-shipped bug that caused missed kills, not just corrupt names.
* **Location**: `detect_killfeed.py` -> `detect_killfeed_regions()` (split-then-filter ordering) and `_force_split_tall_region()`.

### 1a. Per-Streamer Zones Are Fractions of the Correct Reference Resolution
* **Rule**: `STREAMER_SEARCH_ZONES` / `calibration_cache.json` entries are fractions (`x0_frac`, `y0_frac`, etc.) of *frame* width/height, always — never store or assume absolute pixels without first confirming the source capture resolution.
* **Why**: `STREAMER_KILLFEED_CONFIGS` (legacy pixel data, still in `config.py` for reference) was calibrated at 2560×1440 but its accompanying comments assumed 1080p, causing real, wrong-by-~10%-of-frame-height zones until cross-checked against a fresh live capture. Always verify a derived zone against a live/VOD frame before trusting it, not just the arithmetic.
* **Location**: `config.py` -> `STREAMER_SEARCH_ZONES` comments; `calibrate_zone.py` -> `_derive_zone_from_regions()`.

### 1b. Bypass, Don't Silently Degrade, on Unresolvable Overlay Contamination
* **Rule**: If `calibrate_zone.py`'s calibration loop finds real killfeed co-occurring with contamination (webcam/chat/banner/etc.) that survives tightening across the full round budget, the streamer must be marked permanently bypassed (`BYPASS` sentinel, `calibration_cache.json`'s `"bypass"` section) — never fall back to the noisy generic box for a streamer already confirmed to have unresolvable overlay contamination.
* **Why**: The generic fallback box exists for streamers with *no evidence either way* (new/untested). Using it for a streamer *already proven* to have inseparable overlay contamination would silently produce known-bad data instead of just skipping them.
* **Location**: `calibrate_zone.py` -> `_run_calibration()` (`ever_saw_contamination` tracking), `get_search_zone()`; `ocr.py` -> `ChannelWorker.run()`'s `BYPASS` check.

### 2. Name Normalization Order of Operations
* **Rule**: In `database.py` -> `normalize_player_name_with_confidence`, the call to `find_best_canonical_match` **must** run *before* `add_name_observation`.
* **Why**: If a new name/typo is added to `recent_names` before matching is calculated, it matches itself in the cache on step 2, achieving a confidence of `1.0` and completely bypassing the Gemini validation queue.

### 3. Dynamic Confidence for Exact Matches
* **Rule**: Exact matches in the database for non-protected players do *not* automatically return `1.0` confidence. Their confidence is computed dynamically: `0.6 + 0.4 * get_name_confidence_score(name)`.
* **Why**: Prevents rare typos that were written directly to the database from immediately matching future instances with perfect confidence, forcing them through verification until they have been seen enough times.

### 4. Anonymized Player Name Filtering
* **Rule**: Players with a high digit ratio (`PLAYER_NAME_MAX_DIGIT_RATIO = 0.4`) or whose names match `LegendName + digits` (e.g. `Ash9009`, `Valkyrie4823`) must be classified as anonymized names and **filtered out** from career ratings.
* **Why**: Anonymized player names change every match and do not represent a consistent player identity.
* **Location**: `elo_engine.py` -> `_is_anonymized_player` and `config.py` -> `PLAYER_NAME_MAX_DIGIT_RATIO`.

---

## 🗄️ Database Schemas

### 1. `killfeed.db` (Primary Event Database)
Contains the historical record of parsed OCR events. Note: this project's dev practice is to
intentionally wipe `killfeed.db`/`elo.db` after significant pipeline changes (e.g. an OCR engine
swap) so old data doesn't confound judging whether the updated pipeline works — a small event
count here after such a change is expected, not necessarily data loss.
* **`events`** Table:
  * `id` (INTEGER PRIMARY KEY AUTOINCREMENT)
  * `streamer` (TEXT) - Name of the stream source (e.g. `Faide`)
  * `timestamp` (TEXT) - Event time (`YYYY-MM-DD HH:MM:SS`)
  * `raw_text` (TEXT) - Raw OCR output line
  * `canonical` (TEXT) - Typos-corrected text line
  * `event_type` (TEXT) - `Kill`, `BleedOut`, `Revive`, `CarePackage`, etc.
  * `attacker` (TEXT) - Normalized attacker name
  * `victim` (TEXT) - Normalized victim name
  * `attacker_conf` (REAL) / `victim_conf` (REAL) - OCR confidence scores
  * `source` (TEXT) - `easyocr`, `trocr`, or `gemini` (if corrected)
  * `gemini_corrected` (INTEGER) - `1` if Gemini fixed the name, `0` otherwise

### 2. `elo.db` (Leaderboard Database)
Stores ELO calculations and career statistics.
* **`player_ratings`** Table:
  * `player` (TEXT PRIMARY KEY) - Canonical player name
  * `elo` (REAL DEFAULT 1000.0)
  * `matches_played` (INTEGER)
  * `total_kills` (INTEGER)
  * `total_deaths` (INTEGER)
  * `peak_elo` (REAL)
  * `last_updated` (TEXT)
* **`matches`** Table:
  * `match_id` (TEXT PRIMARY KEY) - Format: `streamer_unixTimestamp`
  * `streamer` (TEXT)
  * `start_time` / `end_time` (TEXT)
  * `kill_count` (INTEGER)
  * `players_observed` (INTEGER)
* **`match_kills`** Table:
  * `id` (INTEGER PRIMARY KEY AUTOINCREMENT)
  * `match_id` (TEXT)
  * `timestamp` (TEXT)
  * `attacker` / `victim` (TEXT)
  * `kill_order` (INTEGER)
  * `attacker_conf` / `victim_conf` (REAL)
* **`match_placements`** Table:
  * `match_id` (TEXT)
  * `player` (TEXT)
  * `kill_order_out` (INTEGER) - Order index at which the player died
  * `elo_before` / `elo_after` / `elo_change` (REAL)

---

## 🎯 Auto-Calibration System (`calibrate_zone.py`)

For any streamer not in `config.py`'s hand-verified `STREAMER_SEARCH_ZONES`, `ChannelWorker`
auto-calibrates a search zone the first time it sees them, via a bounded Gemini-vision loop.
Key config flags (`config.py`, `AUTO-CALIBRATION` section):

| Flag | Default | Meaning |
|---|---|---|
| `AUTO_CALIBRATE_ZONES` | `True` | Master toggle. `False` = always generic-box fallback (zero Gemini cost). |
| `AUTO_CALIBRATE_TIME_BUDGET` | `45.0` | Hard wall-clock ceiling (seconds) per streamer, per launch. |
| `AUTO_CALIBRATE_MAX_ROUNDS` | `3` | Max propose/verify/tighten rounds before giving up. |
| `AUTO_CALIBRATE_VERIFY_SAMPLES` | `2` | Extra time-spaced re-checks of a seemingly-clean zone before trusting it — a single clean frame isn't proof a persistent overlay (sub-goal bar, browser watermark, etc.) won't show up in the next one. |
| `AUTO_CALIBRATE_MAX_CONCURRENT` | `2` | Max `ChannelWorker`s calibrating via Gemini at once — bounds contention when many streamers launch together (e.g. `--top N`). |

**Outcomes**, persisted in `calibration_cache.json` (git-ignored, generated at runtime):
- **Success** → zone cached under `"zones"`, reused forever (or until `calibrate_zone.py <channel> --unblock`).
- **Bypass** → cached under `"bypass"`; the streamer is never OCR'd. Only happens when real killfeed was actually observed *together with* contamination that survived the full round budget — being between matches, or a temporarily quiet moment with no visible kills, does **not** trigger this (see rule 1b above).
- **Inconclusive** (no ranked game found, Gemini unreachable, etc.) → falls back to the generic box for this session only; retried fresh on the next launch.

**Cost note**: calibration shares the same 15 RPM Gemini budget as the live validation queue
(`gemini_queue.py`). Launching many never-before-seen streamers at once will visibly slow down
both. First-time large-scale runs should budget for this.

**CLI**: `python calibrate_zone.py <channel>` (dry-run, uses the cache), `--force` (bypass cache,
re-run for comparison), `--unblock` (clear a cached/bypassed entry). Pair with
`python detect_killfeed.py <channel> --use-cache --debug` to visually verify a calibrated zone's
detected regions land on real killfeed text before trusting it in production.
