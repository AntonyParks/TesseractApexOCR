import csv
import time
from pathlib import Path
from datetime import datetime
import sys

# Add root folder to python path so we can import config, database, and parsers
sys.path.append(str(Path(__file__).parent.parent))

from config import PLAYER_DB_PATH
from database import PlayerDatabase
from parsers import parse_killfeed_line

def parse_ts(ts_str):
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def main():
    log_path = Path("killfeed_log.csv")
    if not log_path.exists():
        print("killfeed_log.csv not found.")
        return

    # 1. Reset player_names.json to empty
    if PLAYER_DB_PATH.exists():
        PLAYER_DB_PATH.unlink()
        print("Deleted old player_names.json")

    # 2. Initialize fresh PlayerDatabase
    db = PlayerDatabase()
    db.load_databases() # This will seed pro players and legends

    # 3. Read raw lines and re-parse them
    rows = []
    with log_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Loaded {len(rows)} rows from killfeed_log.csv.")
    print("Reparsing all lines...")

    updated_rows = []
    for i, row in enumerate(rows):
        raw_text = row["raw_text"]
        ts_str = row["timestamp"]
        streamer = row["streamer"]
        
        ts = parse_ts(ts_str)
        timestamp_float = ts.timestamp() if ts else time.time()

        # Call parse_killfeed_line with fresh DB and timestamp
        parsed = parse_killfeed_line(raw_text, db, timestamp=timestamp_float)

        updated_row = {
            "streamer": streamer,
            "timestamp": ts_str,
            "raw_text": raw_text,
            "canonical": parsed.get("canonical", ""),
            "event_type": parsed.get("event_type", ""),
            "attacker": parsed.get("attacker", "") or "",
            "victim": parsed.get("victim", "") or "",
            "attacker_conf": parsed.get("attacker_conf", 0.0),
            "victim_conf": parsed.get("victim_conf", 0.0)
        }
        updated_rows.append(updated_row)

        if (i + 1) % 2000 == 0:
            print(f"  Processed {i + 1}/{len(rows)} lines...")

    # 4. Write back to killfeed_log.csv
    fieldnames = ["streamer", "timestamp", "raw_text", "canonical", "event_type", "attacker", "victim", "attacker_conf", "victim_conf"]
    with log_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    # 5. Save the updated player name database
    db.save_player_database()
    print("Saved updated player_names.json")
    print("Successfully reparsed killfeed_log.csv")

if __name__ == "__main__":
    main()
