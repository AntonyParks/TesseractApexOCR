import sqlite3
from pathlib import Path

def main():
    db_path = Path("elo.db")
    if not db_path.exists():
        print("elo.db not found.")
        return
        
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # Fetch top 30 players with at least 3 matches played
    rows = conn.execute("""
        SELECT player, elo, matches_played, total_kills, total_deaths, peak_elo 
        FROM player_ratings 
        WHERE matches_played >= 3 
        ORDER BY elo DESC 
        LIMIT 30
    """).fetchall()
    
    conn.close()
    
    print("\n# ELO Leaderboard (Top 30 Players - Min 3 Matches)")
    print("| Rank | Player | ELO | Matches | Kills | Deaths | Peak ELO |")
    print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for i, r in enumerate(rows, start=1):
        print(f"| {i:2d} | {r['player']:25s} | {r['elo']:7.1f} | {r['matches_played']:7d} | {r['total_kills']:5d} | {r['total_deaths']:6d} | {r['peak_elo']:8.1f} |")

if __name__ == "__main__":
    main()
