import sqlite3
import json
from pathlib import Path
from difflib import SequenceMatcher
import re

def is_garbled(name: str) -> bool:
    # Garbled if it has too many consecutive consonants, weird symbols, or is too short
    name_clean = re.sub(r'[^a-zA-Z]', '', name)
    if not name_clean:
        return True
    
    # Check for long runs of consonants (difficult to pronounce, typical of OCR noise)
    consonants = "bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ"
    vowels = "aeiouAEIOU"
    consonant_run = 0
    max_consonant_run = 0
    for char in name_clean:
        if char in consonants:
            consonant_run += 1
            max_consonant_run = max(max_consonant_run, consonant_run)
        else:
            consonant_run = 0
            
    if max_consonant_run >= 8:
        return True
        
    return False

def main():
    db_path = Path("elo.db")
    player_db_path = Path("player_names.json")
    
    if not db_path.exists():
        print("elo.db not found")
        return
        
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    players = conn.execute("""
        SELECT player, elo, matches_played, total_kills, total_deaths, peak_elo 
        FROM player_ratings 
        ORDER BY elo DESC
    """).fetchall()
    conn.close()
    
    player_data = {}
    if player_db_path.exists():
        with open(player_db_path, "r", encoding="utf-8") as f:
            player_data = json.load(f)
            
    print("=" * 80)
    print("LEADERBOARD SANITY CHECK AND AUDIT")
    print("=" * 80)
    
    # 1. Inspect Top 50 Players for Noise/Garbage
    print("\n[Audit] 1. SUSPICIOUS/GARBLED PLAYER NAMES IN LEADERBOARD:")
    suspicious_count = 0
    for r in players[:100]:
        name = r["player"]
        if is_garbled(name) or len(name) < 4:
            suspicious_count += 1
            db_entry = player_data.get(name, {})
            variants = list(db_entry.get("variants", {}).keys())[:3]
            print(f"  - Rank: ELO={r['elo']:.1f} | Name: '{name}' | Matches: {r['matches_played']} | Kills: {r['total_kills']}")
            print(f"    Learned variants: {variants}")
    if suspicious_count == 0:
        print("  No obviously garbled names found in the top 100.")
        
    # 2. Identify Near-Duplicates (Potential Missing Merges)
    print("\n[Audit] 2. UNMERGED NEAR-DUPLICATES IN LEADERBOARD:")
    unmerged_pairs = []
    player_names = [r["player"] for r in players if r["matches_played"] >= 2]
    
    for i in range(len(player_names)):
        n1 = player_names[i]
        n1_low = n1.lower()
        for j in range(i + 1, len(player_names)):
            n2 = player_names[j]
            n2_low = n2.lower()
            
            # Check similarity
            ratio = SequenceMatcher(None, n1_low, n2_low).ratio()
            if ratio >= 0.70 and ratio < 1.0:
                # Did they fail to merge? Check if they share the 3-char prefix
                shared_prefix = n1_low[:3] == n2_low[:3]
                reason = "Different 3-char prefix" if not shared_prefix else f"Similarity too low for standard threshold ({ratio:.2%})"
                unmerged_pairs.append((n1, n2, ratio, reason))
                
    unmerged_pairs.sort(key=lambda x: x[2], reverse=True)
    for n1, n2, ratio, reason in unmerged_pairs[:15]:
        print(f"  - '{n1}' vs '{n2}' | Similarity: {ratio:.1%} | Reason: {reason}")
    if not unmerged_pairs:
        print("  No near-duplicates detected.")
        
    # 3. High-Variance Players (Challenging OCR targets)
    print("\n[Audit] 3. PLAYERS WITH HIGH VARIANT COUNT (Hard to OCR):")
    high_var = []
    for name, data in player_data.items():
        if data.get("protected", False):
            continue
        variants = data.get("variants", {})
        if len(variants) >= 4:
            high_var.append((name, len(variants), sum(variants.values())))
            
    high_var.sort(key=lambda x: x[1], reverse=True)
    for name, v_count, obs in high_var[:15]:
        print(f"  - Player: '{name}' | Unique OCR spelling variations: {v_count} | Total observations: {obs}")
    if not high_var:
        print("  No high-variance player entries.")

    # 4. Check for capitalization promotions
    print("\n[Audit] 4. LOWERCASE CANONICAL NAMES WITH BETTER CASING IN VARIANTS:")
    better_casing_found = False
    for name, data in player_data.items():
        if name.islower() and not data.get("protected", False):
            # Check if there is a camelcase or mixed-case variant
            for var in data.get("variants", {}):
                has_upper = any(c.isupper() for c in var)
                has_lower = any(c.islower() for c in var)
                if has_upper and has_lower:
                    print(f"  - Canonical: '{name}' | Better variant found: '{var}'")
                    better_casing_found = True
                    break
    if not better_casing_found:
        print("  All canonical names matching mixed-casing rules have been correctly promoted.")

if __name__ == "__main__":
    main()
