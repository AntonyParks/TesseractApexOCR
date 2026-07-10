import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime

# Add root folder to python path
sys.path.append(str(Path(__file__).parent.parent))

from config import PLAYER_DB_PATH, KILLFEED_DB_PATH
from database import PlayerDatabase
from parsers import parse_killfeed_line

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

    # Delete old player_names.json to start fresh with seeding
    if PLAYER_DB_PATH.exists():
        PLAYER_DB_PATH.unlink()
        print("Deleted old player_names.json")

    db = PlayerDatabase()
    db.load_databases() # Seeds pro players and legends

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("SELECT id, streamer, timestamp, raw_text FROM events").fetchall()
    print(f"Loaded {len(rows)} events from SQLite database.")
    print("Reparsing events...")

    updates = []
    for i, row in enumerate(rows):
        row_id = row["id"]
        raw_text = row["raw_text"]
        ts_str = row["timestamp"]
        
        ts = parse_ts(ts_str)
        timestamp_float = ts.timestamp() if ts else time.time()

        parsed = parse_killfeed_line(raw_text, db, timestamp=timestamp_float)

        updates.append((
            parsed.get("canonical", ""),
            parsed.get("event_type", ""),
            parsed.get("attacker", "") or "",
            parsed.get("victim", "") or "",
            parsed.get("attacker_conf", 0.0),
            parsed.get("victim_conf", 0.0),
            row_id
        ))

        if (i + 1) % 1000 == 0 or (i + 1) == len(rows):
            print(f"  Processed {i + 1}/{len(rows)} events...")

    # Bulk update
    cursor.executemany("""
        UPDATE events
        SET canonical = ?,
            event_type = ?,
            attacker = ?,
            victim = ?,
            attacker_conf = ?,
            victim_conf = ?
        WHERE id = ?
    """, updates)

    conn.commit()
    conn.close()

    db.save_player_database()
    print("Saved updated player_names.json")
    print("Successfully reparsed SQLite killfeed.db")

if __name__ == "__main__":
    main()
