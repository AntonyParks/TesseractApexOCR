import glob
from pathlib import Path

crops_dir = Path("crops")
patterns = [
    "crops/Esmeeees/20260612_1304*.png",
    "crops/Esmeeees/*asbqde*.png",
    "crops/Esmeeees/*Ash*.png",
    "crops/*20260612_1304*.png",
    "crops/**/*20260612_1304*.png"
]

print("Searching crops matching patterns:")
for pattern in patterns:
    files = list(glob.glob(pattern, recursive=True))
    if files:
        print(f"Pattern '{pattern}':")
        for f in files:
            print(f"  {f}")
