import sqlite3
import os
import re
from pathlib import Path

def main():
    db_path = Path("elo.db")
    crops_dir = Path("crops")
    
    if not db_path.exists():
        print("elo.db not found.")
        return
        
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # Get top 100 players
    cur.execute("SELECT player, elo FROM player_ratings ORDER BY elo DESC LIMIT 100")
    top_players = cur.fetchall()
    
    print(f"Top 100 ELO Players count: {len(top_players)}")
    
    found_count = 0
    missing_count = 0
    
    for idx, (player, elo) in enumerate(top_players, 1):
        # Find kill events for this player
        cur.execute("""
            SELECT streamer, timestamp, attacker, victim, kill_order
            FROM match_kills k
            JOIN matches m ON k.match_id = m.match_id
            WHERE attacker = ? OR victim = ?
            LIMIT 5
        """, (player, player))
        events = cur.fetchall()
        
        crop_paths = []
        for streamer, timestamp, attacker, victim, kill_order in events:
            # timestamp format: 2026-03-12 14:31:09
            # crop filename format: YYYYMMDD_HHMMSS_lineN_hex4.png
            dt_part = timestamp.replace("-", "").replace(":", "").replace(" ", "_")
            # We look in crops/<streamer> for files starting with dt_part
            streamer_dir = crops_dir / streamer
            if streamer_dir.exists():
                matching_files = list(streamer_dir.glob(f"{dt_part}*.png"))
                for f in matching_files:
                    crop_paths.append(f)
                    
        if crop_paths:
            found_count += 1
            print(f"[{idx}] {player} (ELO: {elo:.1f}): found {len(crop_paths)} crop(s)")
            for p in crop_paths[:2]:
                print(f"   - {p}")
        else:
            missing_count += 1
            print(f"[{idx}] {player} (ELO: {elo:.1f}): NO crop found (checked {len(events)} events)")
            
    print(f"\nSummary: Found crops for {found_count} players, missing crops for {missing_count} players.")

if __name__ == "__main__":
    main()
