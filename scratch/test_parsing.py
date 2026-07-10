import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from database import PlayerDatabase
from parsers import parse_killfeed_line, _parse_killfeed_line_raw, split_by_gun_icon, extract_player_from_segment

def test():
    db = PlayerDatabase()
    db.load_databases()
    
    line = "Ryan 558D:6330xGolyte <GUN_ICON> Fox"
    print(f"Testing line: {line!r}")
    
    canonical = "ryan 558d:6330xgolyte <gun_icon> fox"
    print(f"extract_player_from_segment('ryan 558d:6330xgolyte'): {extract_player_from_segment('ryan 558d:6330xgolyte')}")
    print(f"extract_player_from_segment('fox'): {extract_player_from_segment('fox')}")
    
    atk_part, vic_part = split_by_gun_icon(canonical)
    print(f"  atk_part: {atk_part!r}")
    print(f"  vic_part: {vic_part!r}")
    
    print("\nCalling parse_killfeed_line:")
    res = parse_killfeed_line(line, db)
    print(f"Result: {res}")

if __name__ == "__main__":
    test()
