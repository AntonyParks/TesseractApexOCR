import sqlite3

conn = sqlite3.connect("killfeed.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

rows = cursor.execute(
    "SELECT id, timestamp, raw_text, canonical, attacker, victim, source, gemini_corrected FROM events WHERE streamer='Wavybenji_' AND timestamp LIKE '2026-06-11%'"
).fetchall()

print(f"Found {len(rows)} events:")
for r in rows:
    print(f"ID {r['id']} | {r['timestamp']}")
    print(f"  Raw:       {r['raw_text']!r}")
    print(f"  Canonical: {r['canonical']!r}")
    print(f"  Attacker:  {r['attacker']!r}")
    print(f"  Victim:    {r['victim']!r}")
    print(f"  Source:    {r['source']} | Corrected: {r['gemini_corrected']}")
    print()

conn.close()
