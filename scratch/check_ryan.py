import sqlite3

conn = sqlite3.connect("elo.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Check gata
cursor.execute("SELECT * FROM player_ratings WHERE player='gata'")
row = cursor.fetchone()
if row:
    print("Player 'gata' rating stats:")
    for key in row.keys():
        print(f"  {key}: {row[key]}")
else:
    print("Player 'gata' is not rated in elo.db.")

conn.close()
