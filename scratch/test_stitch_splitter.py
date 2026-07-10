import sys
from pathlib import Path
from datetime import datetime, timedelta
import random

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from match_detector import _split_chunk_recursive
except Exception as e:
    print(f"Imports failed: {e}")

def run_case_1():
    print("\n--- CASE 1: Single Match with High Kills (80 kills, 45 unique players) ---")
    # Capped at 45 players to simulate a single match with respawns
    game_players = [f"Player_A_{i}" for i in range(45)]
    start_time = datetime(2026, 6, 30, 12, 0, 0)
    
    kills_chunk = []
    current_time = start_time
    # 80 kills, but all drawn from the same 45-player pool
    for i in range(80):
        current_time += timedelta(seconds=5)
        kills_chunk.append((current_time, {
            "attacker": random.choice(game_players),
            "victim": random.choice(game_players),
            "attacker_conf": 1.0,
            "victim_conf": 1.0
        }))
        
    print(f"Initial chunk size: {len(kills_chunk)} kills")
    
    seen = set()
    for _, d in kills_chunk:
        seen.add(d["attacker"])
        seen.add(d["victim"])
    print(f"Unique players observed: {len(seen)}")
    
    result = _split_chunk_recursive(kills_chunk, max_players=62)
    print(f"Splits returned: {len(result)}")
    
    if len(result) == 1:
        print("SUCCESS: Single match was NOT split.")
        return True
    else:
        print("FAILURE: Single match was incorrectly split.")
        return False

def run_case_2():
    print("\n--- CASE 2: Stitched Matches (80 kills, 71 unique players) ---")
    # Two distinct player pools to simulate two games merged
    game1_players = [f"Player_G1_{i}" for i in range(45)]
    game2_players = [f"Player_G2_{i}" for i in range(45)]
    
    start_time = datetime(2026, 6, 30, 12, 0, 0)
    kills_chunk = []
    current_time = start_time
    
    # Game 1: 40 kills
    for i in range(40):
        current_time += timedelta(seconds=5)
        kills_chunk.append((current_time, {
            "attacker": random.choice(game1_players),
            "victim": random.choice(game1_players),
            "attacker_conf": 1.0,
            "victim_conf": 1.0
        }))
        
    # 40-second quiet gap between matches
    current_time += timedelta(seconds=40)
    
    # Game 2: 40 kills
    for i in range(40):
        current_time += timedelta(seconds=5)
        kills_chunk.append((current_time, {
            "attacker": random.choice(game2_players),
            "victim": random.choice(game2_players),
            "attacker_conf": 1.0,
            "victim_conf": 1.0
        }))
        
    print(f"Initial chunk size: {len(kills_chunk)} kills")
    
    seen = set()
    for _, d in kills_chunk:
        seen.add(d["attacker"])
        seen.add(d["victim"])
    print(f"Unique players observed: {len(seen)}")
    
    result = _split_chunk_recursive(kills_chunk, max_players=62)
    print(f"Splits returned: {len(result)}")
    
    if len(result) == 2 and len(result[0]) == 40 and len(result[1]) == 40:
        print("SUCCESS: Stitched match was correctly split.")
        return True
    else:
        print("FAILURE: Stitched match was NOT split correctly.")
        return False

def main():
    print("=" * 80)
    print("TESTING UNIQUE-PLAYER-ONLY RECURSIVE STITCH SPLITTER")
    print("=" * 80)
    
    success_1 = run_case_1()
    success_2 = run_case_2()
    
    print("\n" + "=" * 80)
    if success_1 and success_2:
        print("OVERALL RESULT: ALL TESTS PASSED SUCCESSFULLY!")
    else:
        print("OVERALL RESULT: SOME TESTS FAILED.")
    print("=" * 80)

if __name__ == "__main__":
    main()
