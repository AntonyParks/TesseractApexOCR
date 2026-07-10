import sys
import sqlite3
import os
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def main():
    elo_db_path = Path("elo.db")
    killfeed_db_path = Path("killfeed.db")
    crops_dir = Path("crops").absolute()

    if not elo_db_path.exists() or not killfeed_db_path.exists():
        print("Database files not found.")
        return

    # Get top 10 players from elo.db
    conn_elo = sqlite3.connect(str(elo_db_path))
    conn_kf = sqlite3.connect(str(killfeed_db_path))
    
    cur_elo = conn_elo.cursor()
    cur_kf = conn_kf.cursor()

    cur_elo.execute("SELECT player, elo FROM player_ratings ORDER BY elo DESC LIMIT 10")
    top_10 = cur_elo.fetchall()

    print(f"Tracing top 10 players back to screenshots...\n")

    for idx, (player, elo) in enumerate(top_10, 1):
        print(f"=== [{idx}] Player: {player} (ELO: {elo:.2f}) ===")
        
        # Get up to 3 events where this player was attacker or victim
        cur_elo.execute("""
            SELECT match_id, timestamp, attacker, victim
            FROM match_kills
            WHERE attacker = ? OR victim = ?
            ORDER BY timestamp DESC
            LIMIT 3
        """, (player, player))
        kills = cur_elo.fetchall()

        if not kills:
            print("   No match kills found in ELO database.\n")
            continue

        for match_id, timestamp, attacker, victim in kills:
            # Match_id contains streamer name, e.g. Maxg4Metv_1781144147
            streamer = match_id.split('_')[0]
            
            # Format timestamp YYYY-MM-DD HH:MM:SS to YYYYMMDD_HHMMSS
            dt_part = timestamp.replace("-", "").replace(":", "").replace(" ", "_")
            
            # Look up raw text in killfeed.db
            cur_kf.execute("""
                SELECT raw_text
                FROM events
                WHERE streamer = ? AND timestamp = ?
                LIMIT 1
            """, (streamer, timestamp))
            kf_row = cur_kf.fetchone()
            raw_text = kf_row[0] if kf_row else "(not found in events log)"

            # Look up crop file in crops/<streamer>/
            streamer_dir = crops_dir / streamer
            matching_crops = []
            if streamer_dir.exists():
                matching_crops = list(streamer_dir.glob(f"{dt_part}*.png"))

            role = "Attacker" if attacker == player else "Victim"
            opponent = victim if attacker == player else attacker
            print(f"   - Match: {match_id} | Time: {timestamp}")
            print(f"     Role: {role} vs {opponent} | Raw OCR: {raw_text!r}")
            if matching_crops:
                for crop in matching_crops:
                    # Format absolute path with forward slashes for Windows compatibility in markdown links
                    fpath = str(crop.resolve()).replace("\\", "/")
                    print(f"     Screenshot: file:///{fpath}")
            else:
                print(f"     Screenshot: (No PNG crop found under crops/{streamer}/{dt_part}*.png)")
        print()

    conn_elo.close()
    conn_kf.close()

if __name__ == "__main__":
    main()
