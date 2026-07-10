"""
generate_ground_truth.py — Run Gemini once on raw crops to generate ground truth labels.
Saves results to test_crops/ground_truth.json. Resumable if interrupted.
"""
import os
import sys
import base64
import json
import time
from pathlib import Path

# Load .env
env_path = Path(r"g:\PycharmProjects\TesseractApexOCR\.env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY not set")
    sys.exit(1)

# Models to try (Gemini 1.5 Flash stable is multimodal & has quota)
MODEL = "gemini-flash-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"

RAW_DIR = Path(r"g:\PycharmProjects\TesseractApexOCR\test_crops\raw")
GT_FILE = Path(r"g:\PycharmProjects\TesseractApexOCR\test_crops\ground_truth.json")

GEMINI_PROMPT = """You are an expert Apex Legends OCR analyst.
This is a RAW crop from the in-game killfeed.
Transcribe the text exactly as it appears. Use <GUN_ICON> where the weapon icon sits between attacker and victim names.
Output ONLY raw JSON: {"transcription": "exact text here"}
Do not include markdown formatting."""


def call_gemini(img_path: Path) -> str | None:
    import requests
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/png", "data": b64}},
            {"text": GEMINI_PROMPT}
        ]}],
        "generationConfig": {
            "maxOutputTokens": 256,
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }
    
    # Retry loop with exponential backoff on 429
    backoff = 10
    for attempt in range(5):
        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=30)
            if resp.status_code == 429:
                print(f"    [429] Rate limited. Waiting {backoff} seconds...")
                time.sleep(backoff)
                backoff *= 1.5
                continue
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            return data.get("transcription", "").strip()
        except Exception as e:
            print(f"    Error on attempt {attempt+1}: {e}")
            time.sleep(2)
    return None


def main():
    if not RAW_DIR.exists():
        print(f"Raw dir does not exist: {RAW_DIR}")
        return

    # Load existing ground truth if any
    gt_data = {}
    if GT_FILE.exists():
        try:
            gt_data = json.loads(GT_FILE.read_text(encoding="utf-8"))
            print(f"Loaded {len(gt_data)} existing ground truth labels.")
        except Exception as e:
            print(f"Error loading ground_truth.json: {e}")

    # Find raw crops
    crops = sorted(RAW_DIR.glob("*.png"))
    print(f"Found {len(crops)} total raw crops in {RAW_DIR}")

    # We only need 20 good ground truth annotations for a robust benchmark
    target_count = 20
    valid_count = sum(1 for k, v in gt_data.items() if v and "<GUN_ICON>" in v or len(v) > 5)
    
    print(f"Current valid ground truth count: {valid_count} / {target_count}")

    for i, crop_path in enumerate(crops):
        if valid_count >= target_count:
            print(f"Reached target of {target_count} valid ground truth crops.")
            break

        # Check if already processed and has a valid non-empty transcription
        if crop_path.name in gt_data:
            existing = gt_data[crop_path.name]
            if existing and len(existing.strip()) > 0:
                # Count as valid if it looks like a real transcription
                if "<GUN_ICON>" in existing or len(existing) > 5:
                    continue

        print(f"[{i+1}/{len(crops)}] Labeling {crop_path.name}...")
        text = call_gemini(crop_path)
        if text is not None:
            print(f"  Result: {text!r}")
            gt_data[crop_path.name] = text
            # Save progress
            GT_FILE.write_text(json.dumps(gt_data, indent=2, ensure_ascii=False), encoding="utf-8")
            if text and ("<GUN_ICON>" in text or len(text) > 5):
                valid_count += 1
            # Standard rate limit spacing (free tier is 15 RPM, so 4+ seconds between successful requests)
            time.sleep(6.5)
        else:
            print("  Failed to get ground truth after retries.")
            time.sleep(2.0)

    print(f"\nFinished labeling! Ground truth saved to {GT_FILE}")
    print(f"Total labeled crops: {len(gt_data)}")


if __name__ == "__main__":
    main()
