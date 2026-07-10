import sqlite3
from pathlib import Path

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found.")
        return
        
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # Total count
    cur.execute("SELECT COUNT(*) FROM events")
    total = cur.fetchone()[0]
    print(f"Total events in database: {total}")
    
    # Count since we started (using recent timestamp)
    # The current local time is around 2026-06-11 14:45
    # Let's see events from 2026-06-11 14:00 onwards
    cur.execute("SELECT COUNT(*) FROM events WHERE timestamp >= '2026-06-11 14:03:00'")
    recent = cur.fetchone()[0]
    print(f"Recent events (since worker start): {recent}")
    
    # Display last 5 events
    cur.execute("SELECT timestamp, streamer, event_type, raw_text, canonical FROM events ORDER BY timestamp DESC LIMIT 5")
    rows = cur.fetchall()
    print("\nLast 5 logged events:")
    for r in rows:
        print(f" - [{r[0]}] {r[1]}: {r[2]} | Raw: '{r[3]}' | Canonical: '{r[4]}'")
        
    conn.close()

if __name__ == "__main__":
    main()
