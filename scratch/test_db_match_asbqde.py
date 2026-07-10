import sys
from pathlib import Path

# Add root directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from database import PlayerDatabase

db = PlayerDatabase()
db.load_databases()

print("1. Direct find_best_canonical_match without adding observation:")
canon, conf = db.find_best_canonical_match("asbqde")
print(f"  Result: {canon!r} with conf {conf}")

print("\n2. Normal normalize_player_name_with_confidence (adds observation first):")
canon, conf = db.normalize_player_name_with_confidence("asbqde")
print(f"  Result: {canon!r} with conf {conf}")
