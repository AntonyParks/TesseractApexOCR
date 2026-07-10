import sqlite3

conn = sqlite3.connect("killfeed.db")
c = conn.cursor()
c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'")
print(c.fetchone()[0])
conn.close()
