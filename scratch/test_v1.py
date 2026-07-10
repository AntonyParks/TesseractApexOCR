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

crop_path = Path(__file__).parent.parent / "crops/Nicksxn/20260610_221844_line1_4ac2.png"
if not crop_path.exists():
    print(f"Error: {crop_path} does not exist")
    sys.exit(1)

with open(crop_path, "rb") as f:
    b64_data = base64.b64encode(f.read()).decode()

models = ["gemini-1.5-flash", "gemini-1.5-pro"]

for model in models:
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"
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
                        "text": "Transcribe the text in this image."
                    }
                ]
            }
        ]
    }
    
    print(f"Testing v1 model: {model}...")
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            print(f"Success! Text: {response.json()['candidates'][0]['content']['parts'][0]['text'].strip()}")
        else:
            print(f"Error Response: {response.text}")
    except Exception as e:
        print(f"Failed: {e}")
    print("-" * 50)
