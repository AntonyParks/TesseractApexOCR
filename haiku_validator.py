"""Real-time Claude Haiku vision validation for low-confidence killfeed OCR.

For Kill events where attacker_conf or victim_conf falls below HAIKU_CONF_THRESHOLD,
this module synchronously asks Haiku to re-read the preprocessed crop image.
If Haiku produces a better transcription, the corrected text is used for the CSV row.
Every validated crop is also saved as a TrOCR training sample.

Requires: ANTHROPIC_API_KEY environment variable.
"""

import base64
import csv
import io
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

import anthropic

PROMPT = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

MODEL        = "claude-haiku-4-5-20251001"
MAX_TOKENS   = 128
TRAINING_DIR = Path("labels/haiku_training")
LABELS_CSV   = Path("labels/labels_clean.csv")

_lock = threading.Lock()


def _ndarray_to_b64(img: np.ndarray) -> str:
    """Encode a numpy image array to base64 PNG string."""
    buf = io.BytesIO()
    # Grayscale (2D) or BGR — normalise to uint8 PIL image
    if img.ndim == 2:
        pil = Image.fromarray(img, mode="L").convert("RGB")
    else:
        pil = Image.fromarray(img)
    pil.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def validate_killfeed_crop(crop: np.ndarray) -> str | None:
    """Synchronously ask Haiku to read a preprocessed killfeed crop.

    Returns the transcription string (may contain <GUN_ICON>) or None
    if the response is empty, multi-line, or fails quality checks.
    """
    b64 = _ndarray_to_b64(crop)
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = msg.content[0].text.strip()

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
    """Save a validated crop as a TrOCR training sample.

    Writes the PNG to labels/haiku_training/<streamer>/ and appends a row
    to labels/labels_clean.csv in the same format used by label_crops.py.
    """
    quality = "high" if "<GUN_ICON>" in label else ("medium" if len(label) >= 12 else "low")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{ts}_haiku.png"
    out_dir = TRAINING_DIR / streamer.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    fpath = out_dir / fname

    # Save PNG (convert grayscale to RGB for consistency with TrOCR training)
    if crop.ndim == 2:
        Image.fromarray(crop, mode="L").convert("RGB").save(fpath)
    else:
        Image.fromarray(crop).save(fpath)

    row = [fname, streamer.lower(), str(fpath), label, quality]
    with _lock:
        write_header = not LABELS_CSV.exists()
        LABELS_CSV.parent.mkdir(parents=True, exist_ok=True)
        with LABELS_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["filename", "streamer", "filepath", "label", "quality"])
            w.writerow(row)
