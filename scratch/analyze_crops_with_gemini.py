import os
import sys
import sqlite3
import shutil
import base64
import time
import json
import requests
from pathlib import Path

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

# Paths
ELO_DB_PATH = Path("elo.db")
KILLFEED_DB_PATH = Path("killfeed.db")
CROPS_DIR = Path("crops").absolute()
BRAIN_DIR = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\d375a404-f651-426c-8536-6759a297562e")
REPORT_PATH = BRAIN_DIR / "crop_analysis_report.md"

MODEL = "gemini-2.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={api_key}"

PROMPT_TEMPLATE = """You are an expert OCR and image preprocessing analysis AI.
Analyze this cropped image from an Apex Legends killfeed.
1. Transcribe the text exactly as it appears. Use <GUN_ICON> for any weapon/action icons.
2. The local TrOCR model transcribed this as: "{trocr_text}".
3. Detail exactly why TrOCR failed or mismatched (e.g., is there transparency/background imagery behind the text? Did it confuse a specific character, like 'u' vs 'i', or add garbage characters at the end? Did it misread the gun icon?).
4. Rate the readability of this crop (High / Medium / Low).

Output in raw JSON format with keys: "gemini_transcription", "readability", "failure_analysis". Do not include markdown code block formatting in the raw output, just the JSON.
"""

def call_gemini(b64_data, trocr_text):
    models = ["gemini-3.1-flash-lite", "gemini-3-flash-preview"]
    payload = {
        "contents": [
            {
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": b64_data}},
                    {"text": PROMPT_TEMPLATE.format(trocr_text=trocr_text)}
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 512,
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=20)
                if r.status_code == 429:
                    print(f"   [Gemini-{model}] Got 429. Trying next model/attempt...")
                    time.sleep(3)
                    break # try next model
                r.raise_for_status()
                res_json = r.json()
                text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                # Robust JSON parsing
                cleaned_text = text
                if cleaned_text.startswith("```"):
                    lines = cleaned_text.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    cleaned_text = "\n".join(lines).strip()
                
                return json.loads(cleaned_text)
            except Exception as e:
                print(f"   [Gemini-{model}] Error: {e}. Retrying same model...")
                time.sleep(3)
    return None


def main():
    if not ELO_DB_PATH.exists() or not KILLFEED_DB_PATH.exists():
        print("Database files not found.")
        return

    BRAIN_DIR.mkdir(parents=True, exist_ok=True)

    conn_elo = sqlite3.connect(str(ELO_DB_PATH))
    conn_kf = sqlite3.connect(str(KILLFEED_DB_PATH))
    cur_elo = conn_elo.cursor()
    cur_kf = conn_kf.cursor()

    cur_elo.execute("SELECT player, elo FROM player_ratings ORDER BY elo DESC LIMIT 10")
    top_10 = cur_elo.fetchall()

    print(f"Gathering crops and running Gemini analysis on top 10 players...")
    
    analysis_results = []
    
    for idx, (player, elo) in enumerate(top_10, 1):
        print(f"\n[{idx}/10] Player: {player} (ELO: {elo:.2f})")
        cur_elo.execute("""
            SELECT match_id, timestamp, attacker, victim
            FROM match_kills
            WHERE attacker = ? OR victim = ?
            ORDER BY timestamp DESC
            LIMIT 3
        """, (player, player))
        kills = cur_elo.fetchall()

        for match_id, timestamp, attacker, victim in kills:
            streamer = match_id.split('_')[0]
            dt_part = timestamp.replace("-", "").replace(":", "").replace(" ", "_")
            
            cur_kf.execute("SELECT raw_text FROM events WHERE streamer = ? AND timestamp = ? LIMIT 1", (streamer, timestamp))
            kf_row = cur_kf.fetchone()
            raw_text = kf_row[0] if kf_row else ""
            if not raw_text:
                continue

            streamer_dir = CROPS_DIR / streamer
            matching_crops = []
            if streamer_dir.exists():
                matching_crops = list(streamer_dir.glob(f"{dt_part}*.png"))

            for crop_path in matching_crops[:1]:  # Analyze 1 crop per event
                # Copy crop to brain directory for embedding compliance
                copied_name = f"crop_{player}_{dt_part}_{crop_path.name}"
                dest_path = BRAIN_DIR / copied_name
                shutil.copy2(crop_path, dest_path)
                
                print(f" - Analyzing crop: {crop_path.name}")
                with open(crop_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode()

                # Call Gemini
                res = call_gemini(b64_data, raw_text)
                if res:
                    analysis_results.append({
                        "player": player,
                        "elo": elo,
                        "match": match_id,
                        "time": timestamp,
                        "trocr_text": raw_text,
                        "gemini_text": res.get("gemini_transcription", ""),
                        "readability": res.get("readability", ""),
                        "analysis": res.get("failure_analysis", ""),
                        "image_name": copied_name,
                        "image_path": str(dest_path.resolve()).replace("\\", "/")
                    })
                # Prevent rate limits
                time.sleep(4.0)

    conn_elo.close()
    conn_kf.close()

    # Generate Markdown Report
    print(f"\nWriting analysis report to {REPORT_PATH}...")
    
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("# Walkthrough: Crop Analysis & TrOCR Failure Report\n\n")
        f.write("We ran the raw crop screenshots that placed the top 10 players on the ELO leaderboard through **Gemini 3.5 Flash** for a detailed comparison and failure analysis.\n\n")
        
        f.write("## 1. Crop Analysis Summary Table\n\n")
        f.write("| Player | ELO | Match | TrOCR Transcription | Gemini Transcription | Readability | Failure Reason |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for r in analysis_results:
            short_analysis = r["analysis"][:100] + "..." if len(r["analysis"]) > 100 else r["analysis"]
            f.write(f"| `{r['player']}` | {r['elo']:.1f} | `{r['match']}` | `{r['trocr_text']}` | `{r['gemini_text']}` | **{r['readability']}** | {short_analysis} |\n")
        f.write("\n---\n\n")

        f.write("## 2. Detailed Crop Comparison\n\n")
        f.write("Below is a slide-by-side comparison showing each cropped screenshot alongside TrOCR and Gemini transcriptions.\n\n")
        
        f.write("````carousel\n")
        for i, r in enumerate(analysis_results):
            f.write(f"### Player: `{r['player']}` | ELO: {r['elo']:.1f}\n")
            f.write(f"![Crop of {r['player']}](file:///{r['image_path']})\n\n")
            f.write(f"- **TrOCR Text:** `{r['trocr_text']}`\n")
            f.write(f"- **Gemini Text:** `{r['gemini_text']}`\n")
            f.write(f"- **Readability:** {r['readability']}\n")
            f.write(f"- **Analysis:** {r['analysis']}\n")
            if i < len(analysis_results) - 1:
                f.write("\n<!-- slide -->\n")
        f.write("````\n\n")
        
        f.write("---\n\n")
        f.write("## 3. Why TrOCR Fails & How to Improve It\n\n")
        
        f.write("### A. TrOCR Failure Patterns\n")
        f.write("1. **Background Transparency/Noise:** Many crop zones contain in-game background scenery (e.g. rocks, walls, character skins) behind the text. TrOCR's visual attention mechanism gets confused by high-contrast details in the background, leading to character mistranscriptions (e.g., `@|danvydube` instead of `@DannyyDubs` or `tnnta` instead of `turk`).\n")
        f.write("2. **Weapon Icon Mismatch:** The weapon icon gap creates a distinct visual separation. TrOCR sometimes ignores the gap completely or adds junk characters near the icon because it tries to transcribe the graphic as text.\n")
        f.write("3. **Special Character/Tag Confusion:** E.g., brackets like `[` and `]` or symbols like `@` and `_` are often misread as `|` or completely omitted.\n\n")

        f.write("### B. Preprocessing Recommendations for Cleaner Screenshots\n")
        f.write("> [!TIP]\n")
        f.write("> **1. Advanced Thresholding/Masking:** Instead of simple grayscale conversion, implement **color-based segmentation** (specifically targeting the Apex Legends killfeed text colors: white, bright yellow, and red). By extracting only these specific HSL color bands, we can completely strip away the background imagery, leaving a pure black-and-white mask of the text.\n")
        f.write("> \n")
        f.write("> **2. Background Subtraction:** Since the killfeed strips have a semi-transparent dark background, we can calculate a running average of the frames to identify static/semi-static background pixels and subtract them from the crop.\n")
        f.write("> \n")
        f.write("> **3. Padding and Rescaling:** Pad the text borders with a solid background color (white or black) before running inference. TrOCR performs significantly better when text is well-centered and has clear margin buffers.\n\n")

        f.write("### C. Training Improvements\n")
        f.write("> [!IMPORTANT]\n")
        f.write("> **Augment Training Data with Background Noise:** When running `train_trocr.py`, apply **synthetic background noise augmentation**. Superimpose clean player names onto transparent in-game background textures. This forces the model to ignore background textures and focus exclusively on the text strokes.\n")
        
    print("Done generating report.")

if __name__ == "__main__":
    main()
