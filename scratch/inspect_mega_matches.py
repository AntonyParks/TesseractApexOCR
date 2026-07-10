import sqlite3
from pathlib import Path
import sys
from datetime import datetime

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from match_detector import detect_matches_from_db
except Exception as e:
    print(f"Imports failed: {e}")

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found")
        return
        
    print("=" * 80)
    print("INSPECTING MEGA-MATCHES IN DATABASE")
    print("=" * 80)
    
    matches = detect_matches_from_db(db_path)
    mega_matches = [m for m in matches if m.kill_count >= 100]
    
    print(f"Total mega-matches (>=100 kills) found: {len(mega_matches)}")
    
    for m in mega_matches:
        duration = (m.end_time - m.start_time).total_seconds()
        
        # Count unique players
        seen_players = set()
        for k in m.kills:
            if k.attacker:
                seen_players.add(k.attacker)
            if k.victim:
                seen_players.add(k.victim)
                
        # Find internal gaps
        gaps = []
        for i in range(len(m.kills) - 1):
            gap = (m.kills[i+1].timestamp - m.kills[i].timestamp).total_seconds()
            gaps.append(gap)
            
        max_gap = max(gaps) if gaps else 0
        print(f"\nMatch ID: {m.match_id}")
        print(f"  Streamer: {m.streamer}")
        print(f"  Duration: {duration/60:.1f} minutes")
        print(f"  Kills:    {m.kill_count}")
        print(f"  Unique Players: {len(seen_players)}")
        print(f"  Max Internal Gap: {max_gap:.1f} seconds")
        
        # Show top 5 gaps
        sorted_gaps = sorted(gaps, reverse=True)[:5]
        print(f"  Top 5 gaps: {sorted_gaps}")

if __name__ == "__main__":
    main()
