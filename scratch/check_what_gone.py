import sqlite3
import json
from pathlib import Path

# Check killfeed.db
conn_kf = sqlite3.connect("killfeed.db")
c_kf = conn_kf.cursor()
c_kf.execute("SELECT COUNT(*) FROM events WHERE attacker='what' OR victim='what'")
kf_count = c_kf.fetchone()[0]
conn_kf.close()

# Check elo.db
conn_elo = sqlite3.connect("elo.db")
c_elo = conn_elo.cursor()
# Check if player ratings table exists
c_elo.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_ratings'")
if c_elo.fetchone():
    c_elo.execute("SELECT * FROM player_ratings WHERE player='what'")
    elo_rows = c_elo.fetchall()
else:
    elo_rows = []
conn_elo.close()

# Check player_names.json
player_names_path = Path("player_names.json")
in_json = False
if player_names_path.exists():
    try:
        with player_names_path.open("r", encoding="utf-8") as f:
            players = json.load(f)
            in_json = "what" in players
    except Exception:
        pass

print(f"Results:")
print(f"  - Count in killfeed.db: {kf_count}")
print(f"  - Rows in elo.db:       {elo_rows}")
print(f"  - In player_names.json: {in_json}")
