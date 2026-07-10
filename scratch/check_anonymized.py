import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import sqlite3
import re
from config import APEX_LEGENDS_CANONICAL

def check_anonymized():
    conn = sqlite3.connect("killfeed.db")
    cursor = conn.cursor()
    
    # Fetch all attacker and victim names
    cursor.execute("SELECT DISTINCT attacker FROM events WHERE attacker != ''")
    attackers = [r[0] for r in cursor.fetchall()]
    
    cursor.execute("SELECT DISTINCT victim FROM events WHERE victim != ''")
    victims = [r[0] for r in cursor.fetchall()]
    
    all_names = set(attackers + victims)
    
    # Legend patterns
    legend_patterns = set()
    for legend in APEX_LEGENDS_CANONICAL:
        l_low = legend.lower()
        legend_patterns.add(l_low)
        legend_patterns.add(l_low.replace(" ", ""))
        legend_patterns.add(l_low.replace(" ", "-"))
        if l_low == "valkyrie":
            legend_patterns.add("valk")
        if l_low == "madmaggie":
            legend_patterns.add("maggie")
            
    anonymized_candidates = []
    
    for name in sorted(all_names):
        name_low = name.lower().strip()
        # Strip brackets
        clean = re.sub(r'[\[\]\(\)]', '', name_low).strip()
        
        is_anon = False
        if clean in legend_patterns:
            is_anon = True
        else:
            for l_pat in legend_patterns:
                if name_low.startswith(l_pat):
                    remainder = name_low[len(l_pat):].strip()
                    if remainder and remainder.isdigit():
                        is_anon = True
                        break
                        
        if is_anon:
            # Count occurrences in events table
            cursor.execute("SELECT COUNT(*) FROM events WHERE attacker = ? OR victim = ?", (name, name))
            count = cursor.fetchone()[0]
            anonymized_candidates.append((name, count))
            
    conn.close()
    
    print("Anonymized-pattern names found in killfeed.db and their frequency:")
    # Sort by frequency descending
    anonymized_candidates.sort(key=lambda x: -x[1])
    for name, count in anonymized_candidates:
        print(f"  {name:25s} count: {count}")

if __name__ == "__main__":
    check_anonymized()
