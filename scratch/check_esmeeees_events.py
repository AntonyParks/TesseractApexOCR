import sqlite3

conn = sqlite3.connect("killfeed.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("""
    SELECT * FROM events 
    WHERE streamer='Esmeeees' 
      AND timestamp >= '2026-06-12 13:03:00' 
      AND timestamp <= '2026-06-12 13:06:00'
""")
rows = c.fetchall()

print(f"Events for Esmeeees between 13:03:00 and 13:06:00:")
for row in rows:
    print(dict(row))

conn.close()
