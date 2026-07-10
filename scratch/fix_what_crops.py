import sys
import sqlite3
import time
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from config import KILLFEED_DB_PATH, USE_TROCR, TROCR_MODEL_PATH, TESSERACT_CONFIG
from database import PlayerDatabase
from parsers import parse_killfeed_line
from viewer import _scan_streamer_dir, _find_crops_in_window
from detect_killfeed import _force_split_tall_region, _MAX_SINGLE_LINE_HEIGHT, _MIN_COL_BRIGHT, _MIN_LINE_WIDTH

def main():
    if not KILLFEED_DB_PATH.exists():
        print("killfeed.db not found.")
        return
        
    db = PlayerDatabase()
    db.load_databases()
    
    conn = sqlite3.connect(str(KILLFEED_DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Query events for player 'what'
    rows = cursor.execute(
        "SELECT id, timestamp, streamer, raw_text, canonical FROM events WHERE attacker='what' OR victim='what'"
    ).fetchall()
    
    print(f"Found {len(rows)} events in killfeed.db for player 'what':\n")
    
    # Scan and match crops
    unique_streamers = {row["streamer"] for row in rows}
    crop_index = {s: _scan_streamer_dir(s) for s in unique_streamers}
    
    # We will record the updates and inserts to execute
    db_updates = []
    db_inserts = []
    
    for row in rows:
        row_id = row["id"]
        ts_str = row["timestamp"]
        streamer = row["streamer"]
        print(f"Processing Event ID {row_id} ({ts_str}, streamer={streamer})...")
        
        # Parse timestamp
        dt = datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
        timestamp_float = dt.timestamp()
        
        ts_list, fn_list = crop_index.get(streamer, ([], []))
        crops = _find_crops_in_window(ts_str, streamer, ts_list, fn_list)
        
        if not crops:
            print("  No crops found for this event timestamp.")
            continue
            
        # We take the first crop
        c = crops[0]
        crop_path = Path("crops") / c["streamer_dir"] / c["filename"]
        print(f"  Using crop: {crop_path}")
        
        if not crop_path.exists():
            print("  Crop file does not exist on disk.")
            continue
            
        img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print("  Failed to load crop image.")
            continue
            
        # Revert scaling and padding to prepare for splitting
        h_img, w_img = img.shape
        unpadded = img[15:h_img-15, 15:w_img-15]
        orig_h = unpadded.shape[0] // 2
        orig_w = unpadded.shape[1] // 2
        orig_img = cv2.resize(unpadded, (orig_w, orig_h), interpolation=cv2.INTER_AREA)
        
        # Convert to simulated binary brightness map
        bmap = cv2.bitwise_not(orig_img)
        bmap = (bmap > 127).astype(np.float32)
        
        # Split using our new logic
        initial_region = {"left": 0, "top": 0, "width": orig_w, "height": orig_h}
        split_regions = _force_split_tall_region(
            initial_region, bmap, 0, 0, _MAX_SINGLE_LINE_HEIGHT
        )
        
        print(f"  Split into {len(split_regions)} region(s).")
        
        # OCR each region
        ocr_texts = []
        for i, reg in enumerate(split_regions):
            l, t, w, h = reg["left"], reg["top"], reg["width"], reg["height"]
            sub_crop = orig_img[t:t+h, l:l+w]
            
            # Re-scale 2x and pad 15px
            sub_upscaled = cv2.resize(sub_crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            sub_padded = cv2.copyMakeBorder(sub_upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
            
            # Run OCR
            if USE_TROCR:
                try:
                    from trocr_inference import ocr_with_trocr
                    text, conf = ocr_with_trocr(sub_padded, [], TROCR_MODEL_PATH)
                except Exception as e:
                    print(f"    TrOCR failed: {e}. Falling back to Tesseract.")
                    from ocr import ocr_with_positions
                    text = ocr_with_positions(sub_padded, TESSERACT_CONFIG)
            else:
                from ocr import ocr_with_positions
                text = ocr_with_positions(sub_padded, TESSERACT_CONFIG)
                
            print(f"    Region {i} OCR text: {text!r}")
            ocr_texts.append(text)
            
        if not ocr_texts:
            print("  No OCR text retrieved.")
            continue
            
        # Parse the OCR texts
        parsed_events = []
        for text in ocr_texts:
            parsed = parse_killfeed_line(text, db, timestamp=timestamp_float)
            parsed_events.append(parsed)
            
        # The first parsed event updates the existing row in killfeed.db
        p0 = parsed_events[0]
        db_updates.append((
            ocr_texts[0],
            p0.get("canonical", ""),
            p0.get("event_type", ""),
            p0.get("attacker", "") or "",
            p0.get("victim", "") or "",
            p0.get("attacker_conf", 0.0),
            p0.get("victim_conf", 0.0),
            "trocr" if USE_TROCR else "tesseract",
            0, # gemini_corrected
            row_id
        ))
        print(f"  -> Event {row_id} update: attacker={p0.get('attacker')}, victim={p0.get('victim')}")
        
        # Any subsequent parsed events are inserted as new rows in killfeed.db
        for j, pj in enumerate(parsed_events[1:], 1):
            db_inserts.append((
                streamer,
                ts_str,
                ocr_texts[j],
                pj.get("canonical", ""),
                pj.get("event_type", ""),
                pj.get("attacker", "") or "",
                pj.get("victim", "") or "",
                pj.get("attacker_conf", 0.0),
                pj.get("victim_conf", 0.0),
                "trocr" if USE_TROCR else "tesseract",
                0 # gemini_corrected
            ))
            print(f"  -> New event insert: attacker={pj.get('attacker')}, victim={pj.get('victim')}")
        print()
        
    # Apply updates
    if db_updates:
        print(f"Updating {len(db_updates)} existing events in killfeed.db...")
        cursor.executemany("""
            UPDATE events
            SET raw_text = ?,
                canonical = ?,
                event_type = ?,
                attacker = ?,
                victim = ?,
                attacker_conf = ?,
                victim_conf = ?,
                source = ?,
                gemini_corrected = ?
            WHERE id = ?
        """, db_updates)
        
    if db_inserts:
        print(f"Inserting {len(db_inserts)} new events in killfeed.db...")
        cursor.executemany("""
            INSERT INTO events
                (streamer, timestamp, raw_text, canonical, event_type,
                 attacker, victim, attacker_conf, victim_conf,
                 source, gemini_corrected)
            VALUES
                (?,?,?,?,?,?,?,?,?,?,?)
        """, db_inserts)
        
    conn.commit()
    conn.close()
    
    # Save the updated player name database
    db.save_player_database()
    print("Saved player names database.")
    print("Successfully corrected double-line events in SQLite database.")

if __name__ == "__main__":
    main()
