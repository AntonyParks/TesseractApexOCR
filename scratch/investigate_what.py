import sqlite3
import sys
from pathlib import Path
from datetime import datetime

sys.path.append(str(Path(__file__).parent.parent))
from config import KILLFEED_DB_PATH
from viewer import _scan_streamer_dir, _find_crops_in_window
from gemini_validator import validate_killfeed_crop
import cv2

def main():
    conn = sqlite3.connect("killfeed.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Query events for player 'what'
    rows = cursor.execute(
        "SELECT id, timestamp, streamer, event_type, raw_text, canonical, attacker, victim FROM events WHERE attacker='what' OR victim='what'"
    ).fetchall()
    
    print(f"Found {len(rows)} events in killfeed.db for player 'what':\n")

    # Scan and match crops
    unique_streamers = {row["streamer"] for row in rows}
    crop_index = {s: _scan_streamer_dir(s) for s in unique_streamers}

    for row in rows:
        print(f"--- Event ID {row['id']} ---")
        print(f"Timestamp:  {row['timestamp']}")
        print(f"Streamer:   {row['streamer']}")
        print(f"Raw text:   {row['raw_text']}")
        print(f"Canonical:  {row['canonical']}")
        print(f"Attacker:   {row['attacker']}")
        print(f"Victim:     {row['victim']}")

        ts_list, fn_list = crop_index.get(row["streamer"], ([], []))
        crops = _find_crops_in_window(row["timestamp"], row["streamer"], ts_list, fn_list)
        
        print(f"Matching crops found: {len(crops)}")
        for i, c in enumerate(crops):
            crop_path = Path("crops") / c["streamer_dir"] / c["filename"]
            print(f"  [{i}] Crop filename: {c['filename']}")
            print(f"      Full path:     {crop_path}")

            if crop_path.exists():
                img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    # Let's run it through Gemini
                    print("      Running through Gemini...")
                    gemini_text = validate_killfeed_crop(img)
                    print(f"      Gemini Read:   {gemini_text!r}")
                else:
                    print("      Could not load image with OpenCV.")
            else:
                print("      Crop file does not exist on disk.")
        print()

    conn.close()

if __name__ == "__main__":
    main()
