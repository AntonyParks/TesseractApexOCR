import sqlite3
from pathlib import Path

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found")
        return
        
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    print("Searching for variations containing 'big' or 'wind' in events:")
    rows = conn.execute("""
        SELECT DISTINCT attacker, victim 
        FROM events 
        WHERE attacker LIKE '%big%' 
           OR victim LIKE '%big%' 
           OR attacker LIKE '%wind%' 
           OR victim LIKE '%wind%'
    """).fetchall()
    
    conn.close()
    
    names = set()
    for r in rows:
        if r["attacker"]:
            names.add(r["attacker"])
        if r["victim"]:
            names.add(r["victim"])
            
    print("Found names:")
    for name in sorted(names):
        if "big" in name.lower() or "wind" in name.lower() or "ight" in name.lower():
            print(f"  - {name}")

if __name__ == "__main__":
    main()
