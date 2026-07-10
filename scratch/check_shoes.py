import sqlite3

def main():
    conn = sqlite3.connect("killfeed.db")
    conn.row_factory = sqlite3.Row
    
    targets = ['shoes', 'sheet', 'sheess', 'shed']
    for t in targets:
        rows = conn.execute("""
            SELECT raw_text, count(*) as count 
            FROM events 
            WHERE attacker = ? 
            GROUP BY raw_text
        """, (t,)).fetchall()
        
        print(f"\nEvents for '{t}':")
        if not rows:
            print("  None")
        for r in rows:
            print(f"  - {r['count']}x: {r['raw_text']!r}")
            
    conn.close()

if __name__ == "__main__":
    main()
