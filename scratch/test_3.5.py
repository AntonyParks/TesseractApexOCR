import base64
import os
import sys
from pathlib import Path
import requests

# Load .env file
env_path = Path(__file__).parent.parent / ".env"
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

# Enable UTF-8 stdout
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MODEL = "gemini-2.5-flash-lite"

crops = [
    ("xoWay [weapon] Rampart7801", "crops/Nicksxn/20260610_221844_line1_4ac2.png"),
    ("Enemy Shield Broken -", "crops/Nicksxn/20260610_225340_line2_18e6.png")
]

prompt_text = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

for label, rel_path in crops:
    crop_path = Path(__file__).parent.parent / rel_path
    if not crop_path.exists():
        print(f"Error: {crop_path} does not exist")
        continue

    with open(crop_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": b64_data
                        }
                    },
                    {
                        "text": prompt_text
                    }
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 128,
            "temperature": 0.0,
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }
    
    print(f"Testing {MODEL} on {label}...")
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        res_json = response.json()
        candidate = res_json["candidates"][0]
        text = candidate["content"]["parts"][0]["text"].strip()
        finish_reason = candidate.get("finishReason")
        
        print(f"Result: {text!r}")
        print(f"Finish Reason: {finish_reason}")
    except Exception as e:
        print(f"Error: {e}")
        if 'response' in locals() and response is not None:
            print(f"Response body: {response.text}")
    print("-" * 50)
