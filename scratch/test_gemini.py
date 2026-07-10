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

import base64

# Create a small dummy image for testing
import numpy as np
import cv2
dummy_img = np.zeros((40, 200), dtype=np.uint8)
cv2.putText(dummy_img, "Test", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 255, 2)
_, buf = cv2.imencode(".png", dummy_img)
b64_img = base64.b64encode(buf).decode()

for test_model in ["gemini-flash-latest", "gemini-3.5-flash"]:
    url_test = f"https://generativelanguage.googleapis.com/v1beta/models/{test_model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/png", "data": b64_img}},
            {"text": "Transcribe this text exactly."}
        ]}],
        "generationConfig": {
            "maxOutputTokens": 64,
            "temperature": 0.0
        }
    }
    try:
        r = requests.post(url_test, json=payload, timeout=10)
        print(f"Test Call ({test_model}) Status:", r.status_code)
        if r.status_code == 200:
            print("Response:", r.json()["candidates"][0]["content"]["parts"][0]["text"])
        else:
            print("Response Body:", r.text)
    except Exception as e:
        print("Error testing call:", e)
