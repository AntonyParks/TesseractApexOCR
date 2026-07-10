import sqlite3

conn = sqlite3.connect("killfeed.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Find events involving 'asbqde'
c.execute("SELECT * FROM events WHERE attacker='asbqde' OR victim='asbqde'")
rows = c.fetchall()

print(f"Found {len(rows)} events involving 'asbqde' in killfeed.db:")
for row in rows:
    print(dict(row))

conn.close()
