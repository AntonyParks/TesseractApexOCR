import sqlite3
import csv
from pathlib import Path

def main():
    db_path = Path("elo.db")
    labels_csv_path = Path("labels/labels_clean.csv")
    crops_dir = Path("crops")
    output_path = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\a52ac73c-e640-491c-b4ec-ba79ac991851\leaderboard_name_audit.md")
    
    # Ensure artifacts folder exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
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
    
    markdown_content = []
    markdown_content.append("# ELO Leaderboard Player Name Audit (Top 100)")
    markdown_content.append("\nWe audited the top 100 players in the ELO database by comparing the registered ELO player name with the ground-truth text verified by Claude 3 Haiku in the saved crop screenshots.\n")
    
    # Statistics counts
    total_flagged = 0
    overlay_count = 0
    system_text_count = 0
    mangled_count = 0
    correct_count = 0
    
    table_rows = []
    
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
        
        verified_label = "No screenshot found"
        matched_crop = None
        
        for streamer, timestamp, attacker, victim, kill_order in events:
            dt_part = timestamp.replace("-", "").replace(":", "").replace(" ", "_")
            streamer_dir = crops_dir / streamer
            if streamer_dir.exists():
                matching_files = list(streamer_dir.glob(f"{dt_part}*.png"))
                if matching_files:
                    matched_crop = matching_files[0]
                    norm_path = str(matched_crop).replace("\\", "/")
                    if norm_path in labels:
                        verified_label = labels[norm_path]
                        break
                    else:
                        verified_label = f"Crop found (`{matched_crop.name}`) but no Haiku label"
        
        # Check name status
        status = "Correct"
        reason = "Valid Player Name"
        
        if verified_label != "No screenshot found" and not verified_label.startswith("Crop found"):
            label_lower = verified_label.lower()
            if "vst" in label_lower or "live" in label_lower or "tvinano" in label_lower:
                status = "Flagged"
                reason = "Twitch / Stream Overlay Watermark"
                overlay_count += 1
                total_flagged += 1
            elif player_lower not in label_lower:
                status = "Flagged"
                reason = "Mismangled OCR Name / Noise"
                mangled_count += 1
                total_flagged += 1
            elif any(x in player_lower for x in ["directly", "clearly", "gettingt", "pushed", "pinged"]):
                status = "Flagged"
                reason = "Apex System Text"
                system_text_count += 1
                total_flagged += 1
            else:
                correct_count += 1
        else:
            # Fallback direct string check
            if any(x in player_lower for x in ["vst", "live"]):
                status = "Flagged"
                reason = "Twitch / Stream Overlay Watermark (Heuristic)"
                overlay_count += 1
                total_flagged += 1
            elif any(x in player_lower for x in ["directly", "clearly", "getting", "pushed", "pinged", "shield", "broken"]):
                status = "Flagged"
                reason = "Apex System Text (Heuristic)"
                system_text_count += 1
                total_flagged += 1
            elif len(player) < 3:
                status = "Flagged"
                reason = "Noise (Too short)"
                mangled_count += 1
                total_flagged += 1
            else:
                correct_count += 1

        badge = "✅ **Correct**" if status == "Correct" else f"❌ **{reason}**"
        
        # Format crop link if available
        crop_link = "—"
        if matched_crop:
            crop_link = f"[{matched_crop.name}](file:///{matched_crop.absolute().as_posix()})"
            
        clean_label = verified_label.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
        table_rows.append(f"| {idx} | `{player}` | {elo:.1f} | {badge} | {clean_label} | {crop_link} |")
        
    markdown_content.append("## Audit Statistics\n")
    markdown_content.append(f"- **Total Players Audited:** 100")
    markdown_content.append(f"- **Valid/Correct Players:** {correct_count} / 100")
    markdown_content.append(f"- **Flagged Incorrect Players:** {total_flagged} / 100")
    markdown_content.append(f"  - *Twitch Overlay Watermarks:* {overlay_count}")
    markdown_content.append(f"  - *Apex System Notification Text:* {system_text_count}")
    markdown_content.append(f"  - *Mismangled OCR / Clan Tag Noise:* {mangled_count}\n")
    
    markdown_content.append("## Recommended Fixes\n")
    markdown_content.append("> [!IMPORTANT]\n")
    markdown_content.append("> 1. **Filter Twitch Overlays:** Reject OCR lines containing `twitch.tv`, `twitch`, `vst`, or similar social handle templates in the parser.\n")
    markdown_content.append("> 2. **Block System Notification Phrases:** Exclude lines containing common Apex system strings like `pinged loot`, `directly`, `clearly`, `shield broken`, or `upgrades`.\n")
    markdown_content.append("> 3. **Purge ELO Database:** Reset or clean up `elo.db` to remove these garbage players and reprocess the match ratings.\n\n")
    
    markdown_content.append("## Detailed Audit Table\n")
    markdown_content.append("| Rank | Player Name | Current ELO | Status | Haiku Verified Label | Screenshot Crop Link |")
    markdown_content.append("| :---: | :--- | :---: | :--- | :--- | :--- |")
    markdown_content.extend(table_rows)
    
    output_path.write_text("\n".join(markdown_content), encoding="utf-8")
    print(f"Audit markdown written successfully to {output_path}")

if __name__ == "__main__":
    main()
