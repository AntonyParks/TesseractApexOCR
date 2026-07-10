import sqlite3
from pathlib import Path
from difflib import SequenceMatcher
import re

_DEDUPE_PREFIX = 3
_DEDUPE_MIN_LEN = 4  # Lowered from 6 to 4 to include short names

def main():
    db_path = Path("elo.db")
    if not db_path.exists():
        print("elo.db not found")
        return
        
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    players = conn.execute("SELECT player, matches_played, total_kills, total_deaths, peak_elo FROM player_ratings").fetchall()
    conn.close()
    
    # Build adjacency list
    adj = {p["player"]: set() for p in players}
    n_players = len(players)

    for i in range(n_players):
        p1 = players[i]
        name1 = p1["player"].lower()
        if len(name1) < _DEDUPE_MIN_LEN:
            continue
            
        for j in range(i + 1, n_players):
            p2 = players[j]
            name2 = p2["player"].lower()
            if len(name2) < _DEDUPE_MIN_LEN:
                continue

            ratio = SequenceMatcher(None, name1, name2).ratio()
            
            # Hybrid threshold check
            if name1[:_DEDUPE_PREFIX] == name2[:_DEDUPE_PREFIX]:
                is_similar = (ratio >= 0.70)
            else:
                is_similar = (ratio >= 0.82)
                
            if is_similar:
                adj[p1["player"]].add(p2["player"])
                adj[p2["player"]].add(p1["player"])

    # Traverse graph to find clusters
    visited = set()
    clusters = []
    player_by_name = {p["player"]: p for p in players}

    for p in players:
        name = p["player"]
        if name in visited:
            continue
            
        component = []
        queue = [name]
        visited.add(name)
        
        while queue:
            curr = queue.pop(0)
            component.append(player_by_name[curr])
            for neighbor in adj[curr]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
                    
        clusters.append(component)

    print("=" * 80)
    print("HYBRID DEDUPLICATION SIMULATION RESULTS")
    print("=" * 80)
    
    total_merges = 0
    for cluster in clusters:
        if len(cluster) <= 1:
            continue
            
        total_merges += 1
        
        # Pick canonical representative
        def get_canonical_score(r):
            p_name = r["player"]
            has_upper = any(c.isupper() for c in p_name)
            has_lower = any(c.islower() for c in p_name)
            mixed_case_bonus = 1 if (has_upper and has_lower) else 0
            return (mixed_case_bonus, r["matches_played"], len(p_name))
            
        canonical_row = max(cluster, key=get_canonical_score)
        canonical = canonical_row["player"]
        duplicates = [r["player"] for r in cluster if r["player"] != canonical]
        
        print(f"\nMerge Group #{total_merges}:")
        print(f"  Canonical -> '{canonical}'")
        print(f"  Duplicates -> {duplicates}")

if __name__ == "__main__":
    main()
