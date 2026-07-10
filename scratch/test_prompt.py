import base64
import os
import sys
import time
from pathlib import Path
import requests
import pprint

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

crop_path = Path(__file__).parent.parent / "crops/Nicksxn/20260610_221844_line1_4ac2.png"
if not crop_path.exists():
    print(f"Error: {crop_path} does not exist")
    sys.exit(1)

with open(crop_path, "rb") as f:
    b64_data = base64.b64encode(f.read()).decode()

MODEL = "gemini-2.5-flash"
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"

prompt_text = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

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


def call_api_with_retry(payload):
    while True:
        try:
            print("Sending API request to Gemini...")
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 429:
                # Attempt to parse retryDelay
                delay = 10
                try:
                    res_json = response.json()
                    details = res_json.get("error", {}).get("details", [])
                    for detail in details:
                        if "retryDelay" in detail:
                            delay_str = detail["retryDelay"]
                            # convert e.g. "53s" or "53.078802013s" to float/int
                            if delay_str.endswith("s"):
                                delay_str = delay_str[:-1]
                            delay = int(float(delay_str)) + 2
                except Exception as ex:
                    print(f"Failed to parse retry delay: {ex}")
                print(f"Got 429 Rate Limit. Sleeping {delay} seconds...")
                time.sleep(delay)
                continue
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Request failed: {e}")
            if 'response' in locals() and response is not None:
                print(f"Response: {response.text}")
            print("Sleeping 10s and retrying...")
            time.sleep(10)

t0 = time.time()
res_json = call_api_with_retry(payload)
elapsed = time.time() - t0

print(f"--- RAW JSON RESPONSE ({elapsed:.2f}s total) ---")
pprint.pprint(res_json)
