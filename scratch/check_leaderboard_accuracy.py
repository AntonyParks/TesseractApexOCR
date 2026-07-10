import os
import sqlite3
import base64
import json
import requests
from pathlib import Path

env_path = Path(".env")
api_key = None
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "GEMINI_API_KEY=" in line:
            api_key = line.split("=", 1)[1].strip()

if not api_key:
    print("Error: GEMINI_API_KEY not set")
    exit(1)

# Get the events that created the leaderboard
# Since Keon is the only streamer with matches right now, we can just grab all Kills from Keon
conn = sqlite3.connect("killfeed.db")
conn.row_factory = sqlite3.Row

events = conn.execute(
    "SELECT id, streamer, timestamp, raw_text, canonical, attacker, victim, crop_filename "
    "FROM events "
    "WHERE event_type = 'Kill' AND crop_filename != '' "
    "ORDER BY timestamp DESC"
).fetchall()

print(f"Found {len(events)} kill events with crops attached.")

MODEL = "gemini-3.1-flash-lite"

def call_gemini(b64_data):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"
    prompt = (
        "You are an expert OCR AI. Transcribe the text in this Apex Legends killfeed crop exactly as it appears. "
        "Use <GUN_ICON> for any weapon/action icons between names. "
        "Output ONLY the raw transcribed text. Do not add any conversational filler or quotes."
    )
    payload = {
        "contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": b64_data}}, {"text": prompt}]}],
        "generationConfig": {"temperature": 0.0}
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif r.status_code == 429:
                import time
                time.sleep(5)
                continue
            return f"Error: {r.status_code}"
        except Exception as e:
            import time
            time.sleep(3)
            continue
    return "Error: Timed out after 3 attempts"

results = []
for ev in events:
    stem = ev['crop_filename']
    if not stem.endswith('.png'):
        stem += '.png'
    crop_path = Path("crops") / ev['streamer'] / stem
    
    if not crop_path.exists():
        print(f"Crop not found for event {ev['id']}: {crop_path}")
        continue
        
    b64 = base64.b64encode(crop_path.read_bytes()).decode('utf-8')
    gemini_text = call_gemini(b64)
    
    easyocr_text = ev['raw_text']
    match = (gemini_text.lower() == easyocr_text.lower())
    
    res = (
        f"Event ID: {ev['id']} ({ev['timestamp']})\n"
        f"  EasyOCR: {easyocr_text}\n"
        f"  Gemini : {gemini_text}\n"
        f"  Match  : {'YES' if match else 'NO'}\n"
    )
    print(res)
    results.append(res)

with open(r"C:\Users\anton\.gemini\antigravity-ide\brain\d375a404-f651-426c-8536-6759a297562e\easyocr_vs_gemini_report.md", "w", encoding="utf-8") as f:
    f.write("# EasyOCR vs Gemini Accuracy Report\n\n```text\n")
    f.write("\n".join(results))
    f.write("\n```\n")
print("Report saved!")
