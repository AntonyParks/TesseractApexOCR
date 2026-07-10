import sqlite3
import csv
import re
from pathlib import Path

def main():
    db_path = Path("elo.db")
    labels_csv_path = Path("labels/labels_clean.csv")
    crops_dir = Path("crops")
    
    if not db_path.exists():
        print("elo.db not found.")
        return
    if not labels_csv_path.exists():
        print("labels_clean.csv not found.")
        return

    # Load labels_clean.csv
    labels = {}
    with labels_csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filepath = row["filepath"].replace("\\", "/")
            labels[filepath] = row["label"]
            
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # Get top 100 players by ELO
    cur.execute("SELECT player, elo, matches_played, total_kills, total_deaths FROM player_ratings ORDER BY elo DESC LIMIT 100")
    top_players = cur.fetchall()
    
    print(f"Loaded {len(top_players)} top ELO players.\n")
    print(f"{'Rank':<4} {'Player':<20} {'ELO':<8} {'Matches':<8} {'Kills/Deaths':<12} {'Haiku Label / Screenshot Verification'}")
    print("-" * 120)
    
    flagged_players = []
    
    for idx, (player, elo, matches, kills, deaths) in enumerate(top_players, 1):
        player_lower = player.lower()
        # Query kill events
        cur.execute("""
            SELECT streamer, timestamp, attacker, victim, kill_order
            FROM match_kills k
            JOIN matches m ON k.match_id = m.match_id
            WHERE attacker = ? OR victim = ?
            LIMIT 5
        """, (player, player))
        events = cur.fetchall()
        
        verified_label = "No screenshot / crop found"
        matched_crop = None
        
        for streamer, timestamp, attacker, victim, kill_order in events:
            dt_part = timestamp.replace("-", "").replace(":", "").replace(" ", "_")
            streamer_dir = crops_dir / streamer
            if streamer_dir.exists():
                matching_files = list(streamer_dir.glob(f"{dt_part}*.png"))
                if matching_files:
                    matched_crop = matching_files[0]
                    # Format matching file path to match labels dictionary keys (slashes normalized)
                    norm_path = str(matched_crop).replace("\\", "/")
                    if norm_path in labels:
                        verified_label = labels[norm_path]
                        break
                    else:
                        verified_label = f"Crop found ({matched_crop.name}) but no Haiku label in CSV"
        
        # Check if the name looks like noise/overlay
        is_flagged = False
        reason = ""
        
        if verified_label != "No screenshot / crop found" and not verified_label.startswith("Crop found"):
            # Check if player name in verified_label is different
            label_lower = verified_label.lower()
            player_lower = player.lower()
            
            # Check for twitch link overlays or common overlay artifacts
            if "vst" in label_lower or "live" in label_lower or "tvinano" in label_lower:
                is_flagged = True
                reason = "Twitch Overlay / Stream Watermark"
            elif player_lower not in label_lower:
                is_flagged = True
                reason = f"Mismatched Name (Haiku: '{verified_label}')"
            elif len(player) < 3:
                is_flagged = True
                reason = "Too short (probable noise)"
        else:
            # Check name string directly if no crop is found
            if any(x in player_lower for x in ["vst", "live"]):
                is_flagged = True
                reason = "Twitch Overlay / Stream Watermark (Direct Name Check)"
            elif len(player) < 3:
                is_flagged = True
                reason = "Too short (Direct Name Check)"

        status_str = f"OK"
        if is_flagged:
            status_str = f"FLAGGED: {reason}"
            flagged_players.append((idx, player, elo, reason, verified_label))
            
        print(f"{idx:<4} {player:<20} {elo:<8.1f} {matches:<8} {f'{kills}/{deaths}':<12} {verified_label} [{status_str}]")
        
    print("\n" + "="*80)
    print(f"FLAGGED PLAYERS SUMMARY ({len(flagged_players)} players flagged out of 100)")
    print("="*80)
    for idx, p, elo, reason, label in flagged_players:
        print(f"Rank {idx}: {p} (ELO: {elo:.1f}) - {reason}")
        print(f"   Haiku Screenshot Text: '{label}'")
        print("-" * 80)

if __name__ == "__main__":
    main()
