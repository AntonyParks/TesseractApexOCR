import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import sqlite3
from difflib import SequenceMatcher

def merge_ash_typos():
    db_path = "killfeed.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Target name
    target = "ash9009"
    
    # Find all unique player names in events table
    cursor.execute("SELECT DISTINCT attacker FROM events WHERE attacker != ''")
    attackers = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT DISTINCT victim FROM events WHERE victim != ''")
    victims = [r[0] for r in cursor.fetchall()]
    
    all_names = set(attackers + victims)
    
    # We want to merge names that start with 'ash' and are highly similar to 'ash9009'
    to_merge = []
    for name in all_names:
        name_low = name.lower().strip()
        if name_low == target:
            continue
        if name_low.startswith("ash"):
            # Check similarity
            ratio = SequenceMatcher(None, name_low, target).ratio()
            # Also check if it ends with digits
            remainder = name_low[3:]
            if remainder.isdigit() and ratio >= 0.70:
                to_merge.append((name, ratio))
                
    print(f"Found {len(to_merge)} names to merge into '{target}':")
    for name, ratio in to_merge:
        print(f"  '{name}' (similarity: {ratio:.2%})")
        
    if not to_merge:
        conn.close()
        return
        
    # Perform update in database
    for name, _ in to_merge:
        # Update attacker
        cursor.execute("UPDATE events SET attacker = ? WHERE attacker = ?", (target, name))
        # Update victim
        cursor.execute("UPDATE events SET victim = ? WHERE victim = ?", (target, name))
        
    conn.commit()
    print("Successfully merged in killfeed.db!")
    
    # Let's count the total events for ash9009 now
    cursor.execute("SELECT COUNT(*) FROM events WHERE attacker = ? OR victim = ?", (target, target))
    total = cursor.fetchone()[0]
    print(f"Total events for '{target}' in killfeed.db: {total}")
    
    conn.close()

if __name__ == "__main__":
    merge_ash_typos()
