import os
from pathlib import Path

def list_matching(dir_path):
    p = Path(dir_path)
    if not p.exists():
        print(f"Directory {p} does not exist.")
        return []
    matches = []
    for root, dirs, files in os.walk(p):
        for f in files:
            if "20260612_1304" in f:
                matches.append(os.path.join(root, f))
    return matches

print("Matching in crops:")
for m in list_matching("crops"):
    print(f"  {m}")

print("Matching in crops_noise:")
for m in list_matching("crops_noise"):
    print(f"  {m}")
