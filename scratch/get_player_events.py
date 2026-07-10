import sqlite3
from pathlib import Path
from datetime import datetime
import os

def parse_ts(ts_str):
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found.")
        return
        
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    player_name = "shoes"
    rows = conn.execute("""
        SELECT id, streamer, timestamp, raw_text, attacker, victim 
        FROM events 
        WHERE attacker = ? OR victim = ?
        ORDER BY timestamp ASC
    """, (player_name, player_name)).fetchall()
    
    conn.close()
    
    print(f"\nFound {len(rows)} events associated with '{player_name}':")
    print("-" * 100)
    
    for idx, r in enumerate(rows[:10], start=1):
        streamer = r["streamer"]
        ts_str = r["timestamp"]
        raw_text = r["raw_text"]
        attacker = r["attacker"]
        victim = r["victim"]
        
        ts = parse_ts(ts_str)
        if not ts:
            print(f"{idx:2d}. Streamer: {streamer} | TS: {ts_str} | Raw: {raw_text!r} (Attacker: {attacker}, Victim: {victim})")
            continue
            
        target_prefix = ts.strftime("%Y%m%d_%H%M%S")
        streamer_dir = Path("crops") / streamer
        matching_crops = []
        if streamer_dir.exists():
            for filename in os.listdir(streamer_dir):
                if filename.endswith(".png") and not filename.endswith("_raw.png"):
                    parts = filename.split("_")
                    if len(parts) >= 2:
                        file_ts_str = parts[0] + "_" + parts[1]
                        try:
                            file_ts = datetime.strptime(file_ts_str, "%Y%m%d_%H%M%S")
                            if abs((file_ts - ts).total_seconds()) <= 5:
                                matching_crops.append(filename)
                        except ValueError:
                            continue
                            
        crop_info = "No matching crop found"
        if matching_crops:
            crop_info = f"Crops: {', '.join(matching_crops)}"
            
        role = "Attacker" if attacker == player_name else "Victim"
        print(f"{idx:2d}. Role: {role:8s} | TS: {ts_str} | Raw: {raw_text!r}")
        print(f"    Streamer: {streamer} | {crop_info}")
        print("-" * 100)

if __name__ == "__main__":
    main()
