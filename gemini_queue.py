"""Async Gemini validation queue for real-time TrOCR correction and training data collection.

Runs a single background daemon thread that drains a bounded queue at a safe 5 RPM rate.
All 20 stream workers can enqueue crops non-blocking — the queue drops overflow silently.

Usage:
    from gemini_queue import get_queue
    get_queue().enqueue(crop, trocr_text, streamer, event_time)

The queue automatically starts itself on first call to get_queue().
"""

import csv
import io
import json
import os
import queue
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import base64
import numpy as np
import requests
from PIL import Image


# ---------------------------------------------------------------------------
# Config (overridden by config.py values after import)
# ---------------------------------------------------------------------------
COOLDOWN_SECS    = 12.0   # 5 RPM safe — set from config.GEMINI_QUEUE_COOLDOWN
MAX_QUEUE_SIZE   = 200    # drops if full
AGREE_THRESHOLD  = 0.85   # SequenceMatcher ratio to count as agreement
CORRECTION_DIR   = Path("labels/gemini_corrections")
CONFIRMED_DIR    = Path("labels/gemini_confirmed")
LABELS_CSV       = Path("labels/labels_clean.csv")
MODEL            = "gemini-2.5-flash"

PROMPT = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

_csv_lock = threading.Lock()

# Claude/Gemini sometimes use typographic punctuation (en/em dash, curly quotes) that isn't in
# the EasyOCR training charset (see KNOWN_ISSUES.md) -- dataset.py silently strips anything
# outside the charset at training time, so normalize to the ASCII equivalents actually in the
# charset here, at label-extraction time, rather than losing that content later.
_CHAR_NORMALIZE = str.maketrans({
    "–": "-", "—": "-",           # en dash, em dash
    "‘": "'", "’": "'",           # curly single quotes
    "“": '"', "”": '"',           # curly double quotes
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ndarray_to_b64(img: np.ndarray) -> str:
    buf = io.BytesIO()
    if img.ndim == 2:
        pil = Image.fromarray(img, mode="L").convert("RGB")
    else:
        pil = Image.fromarray(img)
    pil.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _call_gemini_api(crop: np.ndarray) -> str | None:
    """Call the Gemini API and return stripped text, or None on failure."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        b64 = _ndarray_to_b64(crop)
    except Exception:
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": b64}},
                    {"text": PROMPT},
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 128,
            "temperature": 0.0,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 429:
            # Return a special sentinel so caller can back off
            return "__RATE_LIMITED__"
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip().translate(_CHAR_NORMALIZE)
        return text if text else None
    except Exception:
        return None


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _save_crop(img: np.ndarray, label: str, streamer: str, out_dir: Path, quality: str) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{ts}_gemini.png"
    sub = out_dir / streamer.lower()
    sub.mkdir(parents=True, exist_ok=True)
    fpath = sub / fname
    try:
        if img.ndim == 2:
            Image.fromarray(img, mode="L").convert("RGB").save(fpath)
        else:
            Image.fromarray(img).save(fpath)
    except Exception:
        return

    row = [fname, streamer.lower(), str(fpath), label, quality]
    with _csv_lock:
        write_header = not LABELS_CSV.exists()
        LABELS_CSV.parent.mkdir(parents=True, exist_ok=True)
        try:
            with LABELS_CSV.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["filename", "streamer", "filepath", "label", "quality"])
                w.writerow(row)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

class GeminiValidationQueue:
    """Single background thread that consumes crops and validates them against Gemini."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._lock = threading.Lock()

        # Player DB for re-parsing corrected text (set by ocr.py via set_player_db)
        self._player_db  = None
        self._player_db_lock: threading.Lock | None = None

        # Stats
        self._validated  = 0
        self._agreed     = 0
        self._corrections = 0
        self._dropped    = 0

        # Rate-limit state
        self._last_call  = 0.0
        self._backoff    = 0.0   # extra sleep after 429

        self._thread = threading.Thread(
            target=self._consume, daemon=True, name="GeminiQueue"
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_player_db(self, db, db_lock: threading.Lock) -> None:
        """Register the PlayerDatabase so corrections can be re-parsed."""
        with self._lock:
            self._player_db      = db
            self._player_db_lock = db_lock

    def enqueue(
        self,
        crop: np.ndarray,
        trocr_text: str,
        streamer: str,
        event_time: float,
    ) -> None:
        """Non-blocking enqueue. Drops silently if queue is full."""
        try:
            self._q.put_nowait((crop, trocr_text, streamer, event_time))
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def call_sync(self, payload: dict, timeout: float = 10.0) -> dict | None:
        """Synchronous, rate-limited Gemini call sharing this queue's cooldown/backoff state.

        Used by calibrate_zone.py for structured-output (responseSchema) calls that the
        fixed-prompt async _call_gemini_api() doesn't support. Sharing _last_call/_backoff
        with the async queue keeps combined traffic under the same 5 RPM budget rather than
        risking two independent limiters exceeding it together.

        Blocks the calling thread until the shared cooldown allows a call, then posts
        *payload* directly. Returns the parsed JSON dict from the response text (the caller
        is expected to have requested responseMimeType: application/json), or None on
        failure/timeout/malformed response. Does not auto-retry on 429 — sets the shared
        backoff and returns None so the caller's own bounded retry budget stays authoritative.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None

        with self._lock:
            wait = max(0.0, COOLDOWN_SECS - (time.time() - self._last_call), self._backoff)
        if wait > 0:
            time.sleep(wait)

        with self._lock:
            self._last_call = time.time()
            self._backoff = 0.0

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{MODEL}:generateContent?key={api_key}"
        )
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 429:
                with self._lock:
                    self._backoff = 60.0
                return None
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(text)
        except Exception:
            return None

    def get_stats(self) -> dict:
        with self._lock:
            v = self._validated
            a = self._agreed
            c = self._corrections
            d = self._dropped
        return {
            "validated":   v,
            "agreed":      a,
            "corrections": c,
            "dropped":     d,
            "agree_rate":  round(a / v, 4) if v else 0.0,
            "queue_size":  self._q.qsize(),
        }

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    def _consume(self) -> None:
        _STATS_INTERVAL = 300   # print summary every 5 minutes
        _last_stats     = time.time()
        _local_validated = 0

        while True:
            try:
                crop, trocr_text, streamer, event_time = self._q.get(timeout=1.0)
            except queue.Empty:
                # Print stats if due, then loop
                if time.time() - _last_stats >= _STATS_INTERVAL and self._validated > 0:
                    s = self.get_stats()
                    print(
                        f"[Gemini] {s['validated']} validated — "
                        f"{s['agree_rate']*100:.1f}% agree, "
                        f"{s['corrections']} corrections saved, "
                        f"{s['dropped']} dropped"
                    )
                    _last_stats = time.time()
                continue

            # Rate limiting: enforce cooldown + any backoff from 429
            now = time.time()
            wait = max(0.0, COOLDOWN_SECS - (now - self._last_call), self._backoff)
            if wait > 0:
                time.sleep(wait)
                self._backoff = 0.0

            # Call API
            self._last_call = time.time()
            result = _call_gemini_api(crop)

            if result == "__RATE_LIMITED__":
                # Back off 60s and re-queue the item
                self._backoff = 60.0
                try:
                    self._q.put_nowait((crop, trocr_text, streamer, event_time))
                except queue.Full:
                    pass
                self._q.task_done()
                continue

            if result is None or result == "EMPTY":
                self._q.task_done()
                continue

            # Compare
            sim = _similarity(trocr_text, result)
            agreed = sim >= AGREE_THRESHOLD

            with self._lock:
                self._validated += 1
                if agreed:
                    self._agreed += 1
                else:
                    self._corrections += 1

            if agreed:
                _save_crop(crop, result, streamer, CONFIRMED_DIR, "confirmed")
            else:
                print(
                    f"[Gemini] CORRECTION [{streamer}] "
                    f"TrOCR={trocr_text!r} -> Gemini={result!r} "
                    f"(sim={sim:.2f})"
                )
                _save_crop(crop, result, streamer, CORRECTION_DIR, "correction")

                # Re-parse corrected text and write a correction row to SQLite
                try:
                    import db_log
                    from parsers import parse_killfeed_line
                    player_db   = self._player_db
                    player_lock = self._player_db_lock
                    if player_db is not None and player_lock is not None:
                        with player_lock:
                            corrected = parse_killfeed_line(result, player_db, event_time)
                        if corrected.get("event_type") == "Kill":
                            ts = datetime.fromtimestamp(event_time).strftime("%Y-%m-%d %H:%M:%S")
                            db_log.insert_event(
                                streamer=streamer,
                                timestamp=ts,
                                raw_text=result,
                                canonical=corrected.get("canonical", result),
                                event_type="Kill",
                                attacker=corrected.get("attacker") or "",
                                victim=corrected.get("victim") or "",
                                attacker_conf=corrected.get("attacker_conf", 1.0),
                                victim_conf=corrected.get("victim_conf", 1.0),
                                source="gemini",
                                gemini_corrected=1,
                            )
                except Exception as exc:
                    print(f"[Gemini] Correction DB write error: {exc}")

            _local_validated += 1
            if _local_validated % 50 == 0:
                s = self.get_stats()
                print(
                    f"[Gemini] {s['validated']} validated — "
                    f"{s['agree_rate']*100:.1f}% agree, "
                    f"{s['corrections']} corrections saved"
                )

            self._q.task_done()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: GeminiValidationQueue | None = None
_instance_lock = threading.Lock()


def get_queue() -> GeminiValidationQueue:
    """Return the global singleton queue, creating it on first call."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                # Pull config overrides
                try:
                    from config import (
                        GEMINI_QUEUE_COOLDOWN,
                        GEMINI_QUEUE_MAX_SIZE,
                        GEMINI_AGREE_THRESHOLD,
                        GEMINI_CORRECTION_DIR,
                        GEMINI_CONFIRMED_DIR,
                    )
                    import gemini_queue as _self
                    _self.COOLDOWN_SECS   = GEMINI_QUEUE_COOLDOWN
                    _self.MAX_QUEUE_SIZE  = GEMINI_QUEUE_MAX_SIZE
                    _self.AGREE_THRESHOLD = GEMINI_AGREE_THRESHOLD
                    _self.CORRECTION_DIR  = GEMINI_CORRECTION_DIR
                    _self.CONFIRMED_DIR   = GEMINI_CONFIRMED_DIR
                except ImportError:
                    pass
                _instance = GeminiValidationQueue()
    return _instance
