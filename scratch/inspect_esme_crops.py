import hashlib
import cv2
from pathlib import Path

crops_dir = Path("crops/Esmeeees")
crop_files = list(crops_dir.glob("20260612_1304*.png"))

print(f"Found {len(crop_files)} matching crops in crops/Esmeeees:")
for f in crop_files:
    data = f.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    size = len(data)
    img = cv2.imread(str(f))
    h, w, c = img.shape if img is not None else (0, 0, 0)
    print(f"File: {f.name}")
    print(f"  Size: {size} bytes")
    print(f"  MD5:  {md5}")
    print(f"  Shape: {h}x{w}x{c}")
