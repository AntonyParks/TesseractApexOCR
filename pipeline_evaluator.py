#!/usr/bin/env python3
"""
pipeline_evaluator.py — Unified Pipeline Health Evaluator and OCR Accuracy Diagnostics

This script allows AIs and human developers to evaluate if the entire stream capture,
database, parsing, ELO, and OCR pipelines are working properly. It runs a deep OCR
accuracy evaluation against ground truth labels, calculates Levenshtein similarity,
Character Error Rate (CER), Word Error Rate (WER), and outputs detailed diagnostics.

Usage:
    python pipeline_evaluator.py [--sample N] [--json] [--artifact-dir PATH]
"""

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

# Windows UTF-8 Console output fallback
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Import project settings and modules
try:
    import config
    from database import PlayerDatabase
    from parsers import parse_killfeed_line, is_invalid_player_name, is_weapon_or_attachment
    from elo_db import ELO_DB_PATH
except ImportError as e:
    print(f"[-] Critical Error importing TesseractApexOCR modules: {e}")
    sys.exit(1)

# PyTesseract setup
try:
    import pytesseract
    if hasattr(config, "TESSERACT_CMD") and config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
except Exception:
    pass


# ---------------------------------------------------------------------------
# Core Pure-Python Algorithms (Levenshtein & Alignment)
# ---------------------------------------------------------------------------

def character_levenshtein(s1: str, s2: str) -> int:
    """Compute the Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return character_levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def word_levenshtein(w1: list[str], w2: list[str]) -> int:
    """Compute the Levenshtein distance at the word/token level."""
    if len(w1) < len(w2):
        return word_levenshtein(w2, w1)
    if len(w2) == 0:
        return len(w1)

    previous_row = range(len(w2) + 1)
    for i, token1 in enumerate(w1):
        current_row = [i + 1]
        for j, token2 in enumerate(w2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (token1 != token2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def align_and_find_confusions(gt: str, ocr: str) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Align strings and extract character/substring substitutions, insertions, and deletions."""
    matcher = SequenceMatcher(None, gt, ocr)
    substitutions = []
    deletions = []
    insertions = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            gt_part = gt[i1:i2]
            ocr_part = ocr[j1:j2]
            # Map 1-to-1 if lengths are equal
            if len(gt_part) == len(ocr_part):
                for g_c, o_c in zip(gt_part, ocr_part):
                    if g_c != o_c:
                        substitutions.append((g_c, o_c))
            else:
                substitutions.append((gt_part, ocr_part))
        elif tag == "delete":
            deletions.append(gt[i1:i2])
        elif tag == "insert":
            insertions.append(ocr[j1:j2])

    return substitutions, deletions, insertions


# ---------------------------------------------------------------------------
# OCR Runner Wrapper
# ---------------------------------------------------------------------------

def run_local_ocr(raw_img: np.ndarray) -> tuple[str, str, float]:
    """Run whichever OCR engine config.py currently has configured, using the exact same
    dispatch order and preprocessing ocr.py uses in production (EasyOCR -> TrOCR -> Tesseract).

    Unlike the old implementation, this takes a RAW (unpreprocessed, color) crop image and
    applies the engine-specific preprocessing itself -- the previous version expected an
    already-preprocessed grayscale image but callers were passing raw grayscale crops with no
    inversion/upscale/padding applied, and never exercised EasyOCR at all despite USE_EASYOCR
    being the actual production setting.

    Returns: (text, engine_used, confidence)
    """
    if config.USE_EASYOCR:
        from ocr import preprocess_for_easyocr, ocr_with_easyocr
        processed, _, _ = preprocess_for_easyocr(raw_img)
        try:
            text = ocr_with_easyocr(processed)
            return text, "easyocr", 1.0
        except Exception as e:
            return "", f"failed: {e}", 0.0

    if config.USE_TROCR:
        if not Path(config.TROCR_MODEL_PATH).exists():
            return "", f"failed: TrOCR model path not found: {config.TROCR_MODEL_PATH}", 0.0
        from ocr import preprocess_for_trocr
        from trocr_inference import ocr_with_trocr
        processed_list, _, _ = preprocess_for_trocr(raw_img)
        try:
            text, conf = ocr_with_trocr(processed_list, [], Path(config.TROCR_MODEL_PATH))
            return text, "trocr", conf
        except Exception as e:
            return "", f"failed: {e}", 0.0

    # Tesseract
    from ocr import preprocess, ocr_with_positions
    from config import TESSERACT_CONFIG
    processed, _, _ = preprocess(raw_img)
    try:
        text = ocr_with_positions(processed, TESSERACT_CONFIG)
        return text, "tesseract", 1.0
    except Exception as e:
        return "", f"failed: {e}", 0.0


# ---------------------------------------------------------------------------
# Health Assessment Pipeline
# ---------------------------------------------------------------------------

class PipelineEvaluator:
    def __init__(self, sample_size: int = 20):
        self.sample_size = sample_size
        self.db_path = config.KILLFEED_DB_PATH
        self.elo_db_path = ELO_DB_PATH
        # eval_holdout.csv, not labels_clean.csv -- the latter is training data and structurally
        # excluded from it (see label_crops.py's EVAL_HOLDOUT_CSV exclusion), so benchmarking
        # against it would be circular for anything trained on labels_clean.csv.
        self.labels_csv = Path("labels/eval_holdout.csv")

    def evaluate_config(self) -> dict:
        """Stage 1: Verify Configuration Settings."""
        warnings = []
        status = "ok"

        # Model existence
        model_exists = Path(config.TROCR_MODEL_PATH).exists()
        if config.USE_TROCR and not model_exists:
            warnings.append(f"USE_TROCR is True but model directory is missing: {config.TROCR_MODEL_PATH}")
            status = "fail"

        # Threshold ranges
        if not (0.1 <= config.TROCR_CONF_THRESHOLD <= 0.7):
            warnings.append(f"TROCR_CONF_THRESHOLD ({config.TROCR_CONF_THRESHOLD}) is outside the recommended range [0.1, 0.7]")
            status = "warn"

        if not (0.5 <= config.GEMINI_AGREE_THRESHOLD <= 0.99):
            warnings.append(f"GEMINI_AGREE_THRESHOLD ({config.GEMINI_AGREE_THRESHOLD}) is outside the recommended range [0.5, 0.99]")
            status = "warn"

        if not config.GEMINI_VALIDATE:
            warnings.append("GEMINI_VALIDATE is False in config.py — online verification is disabled.")
            status = "warn"

        ocr_mode = "EasyOCR" if config.USE_EASYOCR else ("TrOCR" if config.USE_TROCR else "Tesseract Only")
        custom_easyocr_model = (config.EASYOCR_CUSTOM_MODEL_DIR / "apex.pth").exists() if config.USE_EASYOCR else None

        return {
            "status": status,
            "metrics": {
                "OCR_MODE": ocr_mode,
                "USE_EASYOCR": config.USE_EASYOCR,
                "custom_easyocr_model_active": custom_easyocr_model,
                "USE_TROCR": config.USE_TROCR,
                "TROCR_MODEL_PATH": str(config.TROCR_MODEL_PATH),
                "trocr_model_exists": model_exists,
                "TROCR_CONF_THRESHOLD": config.TROCR_CONF_THRESHOLD,
                "GEMINI_VALIDATE": config.GEMINI_VALIDATE,
                "GEMINI_AGREE_THRESHOLD": config.GEMINI_AGREE_THRESHOLD,
                "channel_count": len(config.TWITCH_CHANNELS)
            },
            "warnings": warnings
        }

    def evaluate_database(self) -> dict:
        """Stage 2: Audit Event Database Health and Freshness."""
        warnings = []
        status = "ok"
        metrics = {}

        if not self.db_path.exists():
            return {
                "status": "warn",
                "metrics": {},
                "warnings": [f"killfeed.db database file not found at: {self.db_path}"]
            }

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            
            # Event type break down
            rows = conn.execute("SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type").fetchall()
            event_types = {r["event_type"]: r["cnt"] for r in rows}
            
            # Null check in Kills
            kill_counts = conn.execute(
                """SELECT 
                     COUNT(*),
                     SUM(CASE WHEN attacker IS NULL OR attacker='' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN victim IS NULL OR victim='' THEN 1 ELSE 0 END)
                   FROM events WHERE event_type='Kill'"""
            ).fetchone()
            
            tot_kills, null_atk, null_vic = kill_counts[0] or 0, kill_counts[1] or 0, kill_counts[2] or 0
            
            # Freshness (last written event)
            last_ts_str = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()[0]
            
            # Gemini metrics
            gemini_stats = conn.execute(
                "SELECT COUNT(*), SUM(gemini_corrected) FROM events WHERE source='gemini'"
            ).fetchone()
            gemini_events = gemini_stats[0] or 0
            gemini_corrections = gemini_stats[1] or 0

            # Double check for recent pipeline activity
            pipeline_stalled = False
            last_event_time = None
            if last_ts_str:
                try:
                    last_event_time = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                    delta = datetime.now() - last_event_time
                    if delta.days >= 1:
                        pipeline_stalled = True
                        warnings.append("Pipeline Stalled: No events written to database in the last 24 hours.")
                        status = "warn"
                except Exception:
                    pass

            metrics = {
                "total_events": total_events,
                "event_types": event_types,
                "total_kills": tot_kills,
                "null_attacker_pct": round(100.0 * null_atk / tot_kills, 2) if tot_kills else 0.0,
                "null_victim_pct": round(100.0 * null_vic / tot_kills, 2) if tot_kills else 0.0,
                "last_recorded_event": last_ts_str,
                "gemini_db_events": gemini_events,
                "gemini_db_corrections": gemini_corrections,
                "pipeline_stalled": pipeline_stalled
            }

            if tot_kills > 0:
                if (null_atk / tot_kills) > 0.05:
                    warnings.append(f"High null attacker rate: {metrics['null_attacker_pct']}% of kills have no attacker.")
                    status = "warn"
                if (null_vic / tot_kills) > 0.05:
                    warnings.append(f"High null victim rate: {metrics['null_victim_pct']}% of kills have no victim.")
                    status = "warn"

        except Exception as e:
            status = "fail"
            warnings.append(f"Database read query failed: {e}")
        finally:
            conn.close()

        return {"status": status, "metrics": metrics, "warnings": warnings}

    def evaluate_parsing(self) -> dict:
        """Stage 3: Check Parsing Integrity for Leaked Keywords / Self-Kills."""
        warnings = []
        status = "ok"
        metrics = {}

        if not self.db_path.exists():
            return {"status": "skipped", "metrics": {}, "warnings": []}

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Self kills count
            self_kills = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='Kill' AND LOWER(attacker) = LOWER(victim) AND attacker != ''"
            ).fetchone()[0]

            # Legend leaks and common words leaks
            rows = conn.execute("SELECT attacker, victim FROM events WHERE event_type='Kill'").fetchall()
            
            legend_lower = {l.lower() for l in config.APEX_LEGENDS_CANONICAL}
            common_lower = {w.lower() for w in config.COMMON_WORDS}
            
            leaked_legends = Counter()
            leaked_commons = Counter()
            
            for r in rows:
                a, v = (r["attacker"] or "").strip(), (r["victim"] or "").strip()
                for slot in (a, v):
                    if not slot:
                        continue
                    sl = slot.lower()
                    if sl in legend_lower:
                        leaked_legends[slot] += 1
                    if sl in common_lower:
                        leaked_commons[slot] += 1

            total_leaks = sum(leaked_legends.values()) + sum(leaked_commons.values())

            metrics = {
                "self_kills": self_kills,
                "leaked_legend_count": sum(leaked_legends.values()),
                "leaked_common_words_count": sum(leaked_commons.values()),
                "top_leaked_legends": leaked_legends.most_common(5),
                "top_leaked_commons": leaked_commons.most_common(5)
            }

            if self_kills > 10:
                warnings.append(f"Parser Alert: {self_kills} self-kills detected. This suggests name-splitting/spacing OCR bugs.")
                status = "warn"
            if total_leaks > 10:
                warnings.append(f"Parser Alert: {total_leaks} system keywords or legend names leaked into player columns.")
                status = "warn"

        except Exception as e:
            status = "fail"
            warnings.append(f"Parsing check query failed: {e}")
        finally:
            conn.close()

        return {"status": status, "metrics": metrics, "warnings": warnings}

    def evaluate_ocr_accuracy(self) -> dict:
        """Stage 4: Evaluate OCR Accuracy Against Labeled Ground Truth Crops."""
        warnings = []
        status = "ok"

        if not self.labels_csv.exists():
            return {
                "status": "warn",
                "metrics": {},
                "warnings": [f"Holdout eval file not found at: {self.labels_csv}. Cannot calculate OCR accuracy."]
            }

        # Load samples from CSV. Restrict to quality="killfeed" rows -- eval_holdout.csv also
        # contains "nonkillfeed" (contamination/banners, different label semantics) and "noise"
        # (label literally "EMPTY") rows that aren't meaningful for text-accuracy metrics.
        samples = []
        with self.labels_csv.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("quality") != "killfeed":
                    continue
                p = Path(r["filepath"])
                if p.exists():
                    samples.append(r)

        if not samples:
            return {
                "status": "warn",
                "metrics": {},
                "warnings": ["No valid killfeed-quality crop image files found in eval_holdout.csv. Cannot evaluate OCR accuracy."]
            }

        # Perform random sampling
        import random
        # Seed for reproducibility if needed, but none here for live pipeline check
        test_samples = random.sample(samples, min(self.sample_size, len(samples)))

        similarities = []
        cer_scores = []
        wer_scores = []
        exact_matches = 0
        engines_used = Counter()

        confusions = Counter()
        deletions_log = Counter()
        insertions_log = Counter()

        for idx, s in enumerate(test_samples, 1):
            filepath = Path(s["filepath"])
            ground_truth = s["label"].strip()

            # Load raw color image -- run_local_ocr applies the correct engine-specific
            # preprocessing itself (grayscale/invert/upscale for EasyOCR, HSV+Otsu for TrOCR,
            # etc.), matching what ocr.py actually feeds each engine in production.
            img = cv2.imread(str(filepath))
            if img is None:
                continue

            # Run OCR
            ocr_text, engine, confidence = run_local_ocr(img)
            ocr_text = ocr_text.strip()
            engines_used[engine] += 1

            # Match statistics
            similarity = SequenceMatcher(None, ground_truth.lower(), ocr_text.lower()).ratio()
            similarities.append(similarity)

            if ocr_text.lower() == ground_truth.lower():
                exact_matches += 1

            # Character Error Rate (CER)
            dist = character_levenshtein(ground_truth, ocr_text)
            cer = dist / max(1, len(ground_truth))
            cer_scores.append(cer)

            # Word Error Rate (WER)
            w_gt = ground_truth.split()
            w_ocr = ocr_text.split()
            w_dist = word_levenshtein(w_gt, w_ocr)
            wer = w_dist / max(1, len(w_gt))
            wer_scores.append(wer)

            # Accumulate Alignments / Confusions
            subs, dels, ins = align_and_find_confusions(ground_truth, ocr_text)
            for sub in subs:
                confusions[sub] += 1
            for d in dels:
                deletions_log[d] += 1
            for i in ins:
                insertions_log[i] += 1

        # Summary stats
        avg_similarity = statistics.mean(similarities) if similarities else 0.0
        avg_cer = statistics.mean(cer_scores) if cer_scores else 0.0
        avg_wer = statistics.mean(wer_scores) if wer_scores else 0.0
        exact_match_pct = (exact_matches / len(test_samples)) * 100.0 if test_samples else 0.0

        metrics = {
            "eval_sample_count": len(test_samples),
            "exact_match_pct": round(exact_match_pct, 2),
            "average_similarity": round(avg_similarity, 4),
            "average_cer": round(avg_cer, 4),
            "average_wer": round(avg_wer, 4),
            "engines_used": dict(engines_used),
            "top_character_confusions": [[f"'{c[0]}' -> '{c[1]}'", cnt] for c, cnt in confusions.most_common(5)],
            "top_deletions": deletions_log.most_common(5),
            "top_insertions": insertions_log.most_common(5)
        }

        # Check thresholds
        if avg_similarity < 0.75:
            warnings.append(f"OCR Alert: Low average OCR similarity ({avg_similarity:.2f}). OCR engine requires retuning.")
            status = "fail"
        elif avg_similarity < 0.85:
            warnings.append(f"OCR Alert: Sub-optimal average OCR similarity ({avg_similarity:.2f}).")
            status = "warn"

        if avg_cer > 0.20:
            warnings.append(f"OCR Alert: High Character Error Rate ({avg_cer:.1%}). OCR is misreading too many characters.")
            status = "fail"

        return {"status": status, "metrics": metrics, "warnings": warnings}

    def evaluate_elo_and_sessions(self) -> dict:
        """Stage 5: Evaluate ELO Engine and Session Grouping Health."""
        warnings = []
        status = "ok"
        metrics = {}

        if not self.elo_db_path.exists():
            return {
                "status": "warn",
                "metrics": {},
                "warnings": [f"elo.db leaderboard database file not found at: {self.elo_db_path}. Run reprocess.py first."]
            }

        conn = sqlite3.connect(str(self.elo_db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Check tables
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "player_ratings" not in tables or "matches" not in tables:
                return {
                    "status": "fail",
                    "metrics": {},
                    "warnings": ["elo.db is missing required player_ratings or matches tables. Database requires initialization."]
                }

            total_players = conn.execute("SELECT COUNT(*) FROM player_ratings").fetchone()[0]
            qualified_players = conn.execute("SELECT COUNT(*) FROM player_ratings WHERE matches_played >= 3").fetchone()[0]
            
            ratings = [r[0] for r in conn.execute("SELECT elo FROM player_ratings").fetchall()]
            
            elo_min = min(ratings) if ratings else 1000.0
            elo_max = max(ratings) if ratings else 1000.0
            elo_avg = statistics.mean(ratings) if ratings else 1000.0
            elo_std = statistics.stdev(ratings) if len(ratings) > 1 else 0.0

            # Match stats
            total_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            mega_matches = conn.execute("SELECT COUNT(*) FROM matches WHERE kill_count >= 100").fetchone()[0]

            # Near duplicate check
            # Find players with 2+ matches to avoid one-off typos inflating comparison list size
            players = [r[0] for r in conn.execute("SELECT player FROM player_ratings WHERE matches_played >= 2").fetchall()]
            
            by_prefix = defaultdict(list)
            for p in players:
                if len(p) >= 3:
                    by_prefix[p[:3].lower()].append(p)
            
            dup_pairs = 0
            for group in by_prefix.values():
                if len(group) < 2 or len(group) > 50:
                    continue
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        ratio = SequenceMatcher(None, group[i].lower(), group[j].lower()).ratio()
                        if ratio >= 0.8:
                            dup_pairs += 1

            metrics = {
                "total_players": total_players,
                "qualified_players_count": qualified_players,
                "elo_min": round(elo_min, 1),
                "elo_max": round(elo_max, 1),
                "elo_avg": round(elo_avg, 1),
                "elo_std": round(elo_std, 1),
                "total_matches": total_matches,
                "mega_matches_count": mega_matches,
                "near_duplicate_players_count": dup_pairs
            }

            if elo_max > 1900.0:
                warnings.append(f"ELO Alert: Outlier maximum ELO detected ({elo_max:.1f}). This usually signals lobby stitching or crop duplicates.")
                status = "warn"

            if mega_matches > 0:
                warnings.append(f"ELO Alert: {mega_matches} mega-matches (100+ kills) detected. Gap threshold is likely too high.")
                status = "warn"

            if dup_pairs > 100:
                warnings.append(f"ELO Alert: {dup_pairs} near-duplicate player names detected in leaderboard. Run reprocessing with deduplication.")
                status = "warn"

        except Exception as e:
            status = "fail"
            warnings.append(f"ELO check queries failed: {e}")
        finally:
            conn.close()

        return {"status": status, "metrics": metrics, "warnings": warnings}


    def run_all(self) -> dict:
        stages = [
            self.evaluate_config(),
            self.evaluate_database(),
            self.evaluate_parsing(),
            self.evaluate_ocr_accuracy(),
            self.evaluate_elo_and_sessions()
        ]
        
        stage_names = ["config", "database", "parsing", "ocr", "elo"]
        results = {name: stage for name, stage in zip(stage_names, stages)}
        
        # Calculate overall status
        statuses = [s["status"] for s in stages if s["status"] != "skipped"]
        if "fail" in statuses:
            overall = "fail"
        elif "warn" in statuses:
            overall = "warn"
        else:
            overall = "ok"

        results["overall_status"] = overall
        results["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return results


# ---------------------------------------------------------------------------
# Output Renderers
# ---------------------------------------------------------------------------

def render_markdown_report(results: dict) -> str:
    """Generate a clean, styled Markdown report from results."""
    status_emoji = {"ok": "🟢 OK", "warn": "🟡 WARNING", "fail": "🔴 FAILURE", "skipped": "⚪ SKIPPED"}
    
    lines = []
    lines.append(f"# 📊 TesseractApexOCR Pipeline Health Report")
    lines.append(f"**Generated At**: `{results['timestamp']}`  ")
    lines.append(f"**Overall Pipeline Health**: **{status_emoji[results['overall_status']]}**\n")
    
    # 1. Config
    cfg = results["config"]
    lines.append(f"## ⚙️ 1. Configuration Check ({status_emoji[cfg['status']]})")
    for w in cfg["warnings"]:
        lines.append(f"> [!WARNING]\n> {w}\n")
    metrics = cfg["metrics"]
    ocr_mode_line = f"- **OCR Mode**: `{metrics['OCR_MODE']}`"
    if metrics["USE_EASYOCR"]:
        ocr_mode_line += f" (custom fine-tuned model active: `{metrics['custom_easyocr_model_active']}`)"
    lines.append(ocr_mode_line)
    lines.append(f"- **Local TrOCR Model Directory**: `{metrics['TROCR_MODEL_PATH']}` (Exists: `{metrics['trocr_model_exists']}`)")
    lines.append(f"- **TrOCR Conf Threshold**: `{metrics['TROCR_CONF_THRESHOLD']}`")
    lines.append(f"- **Online Gemini Queue**: `{'Enabled' if metrics['GEMINI_VALIDATE'] else 'Disabled'}` (Agree Threshold: `{metrics['GEMINI_AGREE_THRESHOLD']}`)")
    lines.append(f"- **Active Twitch Streamers**: `{metrics['channel_count']}`\n")

    # 2. Database
    db = results["database"]
    lines.append(f"## 🗄️ 2. Database & Capture Freshness ({status_emoji[db['status']]})")
    for w in db["warnings"]:
        lines.append(f"> [!WARNING]\n> {w}\n")
    if db["metrics"]:
        m = db["metrics"]
        lines.append(f"- **Total Database Events**: `{m['total_events']:,}`")
        lines.append(f"- **Total Kills Logs**: `{m['total_kills']:,}`")
        lines.append(f"- **Null Attacker Rate**: `{m['null_attacker_pct']}%` | **Null Victim Rate**: `{m['null_victim_pct']}%`")
        lines.append(f"- **Online Gemini Validations**: `{m['gemini_db_events']}` (Corrections Applied: `{m['gemini_db_corrections']}`)")
        lines.append(f"- **Last Recorded Event**: `{m['last_recorded_event'] or 'Never'}`\n")

    # 3. Parsing
    prs = results["parsing"]
    lines.append(f"## 🔍 3. Parser & Normalizer Integrity ({status_emoji[prs['status']]})")
    for w in prs["warnings"]:
        lines.append(f"> [!WARNING]\n> {w}\n")
    if prs["metrics"]:
        m = prs["metrics"]
        lines.append(f"- **Self-Kills Count**: `{m['self_kills']}` (Indicative of OCR split failures)")
        lines.append(f"- **Leaked Legends/Common Words**: `{m['leaked_legend_count'] + m['leaked_common_words_count']}` events")
        if m["top_leaked_legends"]:
            leaks = ", ".join([f"`{name}` ({cnt}x)" for name, cnt in m["top_leaked_legends"]])
            lines.append(f"- **Top Leaked Legends**: {leaks}")
        if m["top_leaked_commons"]:
            leaks = ", ".join([f"`{name}` ({cnt}x)" for name, cnt in m["top_leaked_commons"]])
            lines.append(f"- **Top Leaked Common Words**: {leaks}")
        lines.append("")

    # 4. OCR
    ocr = results["ocr"]
    lines.append(f"## 👁️ 4. OCR Accuracy Benchmark ({status_emoji[ocr['status']]})")
    for w in ocr["warnings"]:
        lines.append(f"> [!WARNING]\n> {w}\n")
    if ocr["metrics"]:
        m = ocr["metrics"]
        lines.append(f"- **Benchmark Crop Sample Size**: `{m['eval_sample_count']}`")
        lines.append(f"- **Average String Similarity**: `{m['average_similarity']:.2%}`")
        lines.append(f"- **Character Error Rate (CER)**: `{m['average_cer']:.2%}`")
        lines.append(f"- **Word Error Rate (WER)**: `{m['average_wer']:.2%}`")
        lines.append(f"- **Exact Match Accuracy**: `{m['exact_match_pct']}%`")
        lines.append(f"- **OCR Engines Evaluated**: `{m['engines_used']}`")
        
        if m["top_character_confusions"]:
            lines.append("\n### Top OCR Character Confusions:")
            lines.append("| Substitution | Count |")
            lines.append("| --- | --- |")
            for sub, cnt in m["top_character_confusions"]:
                lines.append(f"| `{sub}` | {cnt} |")
        lines.append("")

    # 5. ELO
    elo = results["elo"]
    lines.append(f"## 🏆 5. ELO & Session Grouping Health ({status_emoji[elo['status']]})")
    for w in elo["warnings"]:
        lines.append(f"> [!WARNING]\n> {w}\n")
    if elo["metrics"]:
        m = elo["metrics"]
        lines.append(f"- **Leaderboard Players**: `{m['total_players']:,}` (`{m['qualified_players_count']:,}` with 3+ matches)")
        lines.append(f"- **ELO Rating Distribution**: Mean: `{m['elo_avg']}` | StdDev: `{m['elo_std']}` | Min: `{m['elo_min']}` | Max: `{m['elo_max']}`")
        lines.append(f"- **Total Grouped Matches**: `{m['total_matches']:,}`")
        lines.append(f"- **Mega-Matches (Stitching Bugs)**: `{m['mega_matches_count']}`")
        lines.append(f"- **Near-Duplicate Player Profiles**: `{m['near_duplicate_players_count']}` pairs (Requires deduplication)\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="TesseractApexOCR Pipeline health evaluator")
    parser.add_argument("--sample", type=int, default=20, help="Number of crops to sample for OCR evaluation")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON metrics to stdout")
    parser.add_argument("--artifact-dir", type=str, default=None, help="Directory to save the markdown report file in")
    args = parser.parse_args()

    evaluator = PipelineEvaluator(sample_size=args.sample)
    results = evaluator.run_all()

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        # Generate Markdown
        md_report = render_markdown_report(results)
        print(md_report)

        # Save to local file in current workspace directory
        try:
            Path("pipeline_report.md").write_text(md_report, encoding="utf-8")
            print(f"\n[+] Local markdown report saved to: pipeline_report.md")
        except Exception as e:
            print(f"[-] Failed to save local markdown report: {e}")

        # Save to artifacts directory if specified
        if args.artifact_dir:
            art_dir = Path(args.artifact_dir)
            if art_dir.exists():
                art_file = art_dir / "pipeline_report.md"
                try:
                    art_file.write_text(md_report, encoding="utf-8")
                    print(f"[+] Artifact markdown report saved to: {art_file}")
                except Exception as e:
                    print(f"[-] Failed to save artifact markdown report: {e}")


if __name__ == "__main__":
    main()
