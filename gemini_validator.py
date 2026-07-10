"""Gemini vision API wrapper for killfeed OCR validation.

Provides `validate_killfeed_crop()` for direct synchronous use (e.g. test scripts)
and the underlying `_ndarray_to_b64()` / `_call_gemini_api()` helpers used by
`gemini_queue.py` for the always-on async validation pipeline.

Requires: GEMINI_API_KEY environment variable.
"""

import base64
import csv
import io
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
import requests

PROMPT = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

MODEL        = "gemini-2.5-flash"
MAX_TOKENS   = 128
TRAINING_DIR = Path("labels/gemini_training")
LABELS_CSV   = Path("labels/labels_clean.csv")

_lock = threading.Lock()


def _ndarray_to_b64(img: np.ndarray) -> str:
    """Encode a numpy image array to base64 PNG string."""
    buf = io.BytesIO()
    if img.ndim == 2:
        pil = Image.fromarray(img, mode="L").convert("RGB")
    else:
        pil = Image.fromarray(img)
    pil.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def validate_killfeed_crop(crop: np.ndarray) -> str | None:
    """Synchronously ask Gemini to read a preprocessed killfeed crop.

    Intended for direct/test use. The live pipeline uses gemini_queue.py instead.
    Returns the transcription string (may contain <GUN_ICON>) or None on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] Error: GEMINI_API_KEY environment variable is not set.")
        return None

    try:
        b64 = _ndarray_to_b64(crop)
    except Exception as e:
        print(f"[Gemini] Error converting image to base64: {e}")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": b64
                        }
                    },
                    {
                        "text": PROMPT
                    }
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            "temperature": 0.0,
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[Gemini] API Error: {e}")
        return None

    if not text or text == "EMPTY" or "\n" in text:
        return None
    if len(text) < 4:
        return None
    # Reject non-Latin text (CJK, Arabic, etc.) — Apex player names are ASCII
    latin_ratio = sum(ord(c) < 128 for c in text) / len(text)
    if latin_ratio < 0.8:
        return None
    alpha_ratio = sum(c.isalnum() or c in " <>" for c in text) / len(text)
    if alpha_ratio < 0.5:
        return None
    return text


def save_training_sample(crop: np.ndarray, label: str, streamer: str) -> None:
    """Save a validated crop as a training sample.

    Writes the PNG to labels/gemini_training/<streamer>/ and appends a row
    to labels/labels_clean.csv.
    """
    quality = "high" if "<GUN_ICON>" in label else ("medium" if len(label) >= 12 else "low")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{ts}_gemini.png"
    out_dir = TRAINING_DIR / streamer.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    fpath = out_dir / fname

    # Save PNG (convert grayscale to RGB for consistency with training)
    try:
        if crop.ndim == 2:
            Image.fromarray(crop, mode="L").convert("RGB").save(fpath)
        else:
            Image.fromarray(crop).save(fpath)
    except Exception as e:
        print(f"[Gemini] Error saving training crop: {e}")
        return

    row = [fname, streamer.lower(), str(fpath), label, quality]
    with _lock:
        write_header = not LABELS_CSV.exists()
        LABELS_CSV.parent.mkdir(parents=True, exist_ok=True)
        try:
            with LABELS_CSV.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["filename", "streamer", "filepath", "label", "quality"])
                w.writerow(row)
        except Exception as e:
            print(f"[Gemini] Error writing to labels_clean.csv: {e}")
