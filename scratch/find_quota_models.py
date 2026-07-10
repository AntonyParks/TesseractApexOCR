import os
import requests
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

candidate_models = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview"
]

print("Testing candidates...")
for model in candidate_models:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": "Hello"}]}]
    }
    try:
        r = requests.post(url, json=payload)
        if r.status_code == 200:
            print(f" - {model}: OK (200)")
        elif r.status_code == 429:
            err = r.json().get("error", {}).get("message", "")
            # Print short reason
            reason = "Quota limit"
            if "limit: 20" in err:
                reason = "Limit 20 RPD exceeded"
            elif "limit: 0" in err:
                reason = "Limit 0 (not available)"
            else:
                reason = err.split(".")[0]
            print(f" - {model}: 429 ({reason})")
        else:
            print(f" - {model}: {r.status_code} ({r.text[:100]})")
    except Exception as e:
        print(f" - {model}: Error: {e}")
