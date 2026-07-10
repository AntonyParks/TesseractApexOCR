import base64
import os
import sys
import time
import shutil
import random
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

# Set stdout encoding
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CROPS_DIR = Path(__file__).parent.parent / "crops"
ARTIFACTS_DIR = Path("C:/Users/anton/.gemini/antigravity-ide/brain/a52ac73c-e640-491c-b4ec-ba79ac991851")
AUDIT_MD_PATH = ARTIFACTS_DIR / "gemini_transcription_audit.md"

# Collect all crop PNGs
all_crops = list(CROPS_DIR.glob("**/*.png"))
if not all_crops:
    print("Error: No crops found in crops/ directory")
    sys.exit(1)

# Sample 20 random crops
sample_size = min(20, len(all_crops))
sampled_crops = random.sample(all_crops, sample_size)
print(f"Sampled {sample_size} crops out of {len(all_crops)}")

MODEL = "gemini-2.5-flash-lite"
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"

prompt_text = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)


def call_api_with_retry(b64_data):
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

    while True:
        try:
            response = requests.post(url, json=payload, timeout=12)
            if response.status_code == 429:
                delay = 10
                try:
                    res_json = response.json()
                    details = res_json.get("error", {}).get("details", [])
                    for detail in details:
                        if "retryDelay" in detail:
                            delay_str = detail["retryDelay"]
                            if delay_str.endswith("s"):
                                delay_str = delay_str[:-1]
                            delay = int(float(delay_str)) + 2
                except Exception as ex:
                    print(f"Failed to parse retry delay: {ex}")
                print(f"Rate limited (429). Sleeping {delay} seconds...")
                time.sleep(delay)
                continue
            
            response.raise_for_status()
            res_json = response.json()
            text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            return text
        except Exception as e:
            print(f"API Error: {e}. Retrying in 10 seconds...")
            time.sleep(10)


results = []

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

for i, crop_path in enumerate(sampled_crops, 1):
    print(f"[{i}/{sample_size}] Processing: {crop_path.name}")
    
    # 1. Load image and encode to base64
    with open(crop_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode()
        
    # 2. Query Gemini 2.5 Flash Lite
    t0 = time.time()
    transcription = call_api_with_retry(b64_data)
    elapsed = time.time() - t0
    print(f"  Transcription: {transcription!r} ({elapsed:.1f}s)")
    
    # 3. Copy image to artifacts directory
    artifact_filename = f"audit_crop_{i}.png"
    dest_path = ARTIFACTS_DIR / artifact_filename
    shutil.copy(crop_path, dest_path)
    
    results.append({
        "index": i,
        "filename": crop_path.name,
        "streamer": crop_path.parent.name,
        "artifact_path": f"file:///{dest_path.as_posix()}",
        "transcription": transcription,
        "elapsed": elapsed
    })
    
    # Sleep 4 seconds between requests to be gentle to the rate limiter
    time.sleep(4)

# Generate Markdown Audit Report
md_lines = [
    "# Gemini Vision OCR Transcription Audit Report",
    "",
    "This audit presents a side-by-side comparison of 20 random capture crops and their visual transcriptions using **Gemini 2.5 Flash Lite** (with thinking disabled for maximum speed).",
    "",
    "| Index | Crop Image | Streamer | Gemini Transcription | Latency |",
    "|---|---|---|---|---|",
]

for res in results:
    img_markdown = f"![{res['filename']}]({res['artifact_path']})"
    md_lines.append(
        f"| {res['index']} | {img_markdown} | {res['streamer']} | `{res['transcription']}` | {res['elapsed']:.2f}s |"
    )

md_lines.append("")
md_lines.append(f"Audit completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

AUDIT_MD_PATH.write_text("\n".join(md_lines), encoding="utf-8")
print(f"Audit report written to {AUDIT_MD_PATH}")
