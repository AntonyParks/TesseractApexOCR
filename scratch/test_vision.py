import os
import requests
import base64
import json
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

api_key = os.environ.get("GEMINI_API_KEY")
brain_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\a52ac73c-e640-491c-b4ec-ba79ac991851")

# Use one of the copied images
image_file = list(brain_dir.glob("crop_*.png"))[0]
print("Testing with image:", image_file.name)
with open(image_file, "rb") as f:
    b64_data = base64.b64encode(f.read()).decode()

PROMPT_TEMPLATE = """You are an expert OCR and image preprocessing analysis AI.
Analyze this cropped image from an Apex Legends killfeed.
1. Transcribe the text exactly as it appears. Use <GUN_ICON> for any weapon/action icons.
2. The local TrOCR model transcribed this as: "some_test_text".
3. Detail exactly why TrOCR failed or mismatched.
4. Rate the readability of this crop (High / Medium / Low).

Output in raw JSON format with keys: "gemini_transcription", "readability", "failure_analysis". Do not include markdown code block formatting in the raw output, just the JSON.
"""

for model in ["gemini-3.1-flash-lite", "gemini-3-flash-preview"]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": b64_data}},
                    {"text": PROMPT_TEMPLATE}
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 512,
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }
    try:
        r = requests.post(url, json=payload)
        print(f"Model {model} Status:", r.status_code)
        if r.status_code == 200:
            print("Response:", r.json()["candidates"][0]["content"]["parts"][0]["text"])
        else:
            print("Error body:", r.text)
    except Exception as e:
        print(f"Error for {model}:", e)
