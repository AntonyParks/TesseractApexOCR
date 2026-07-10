import sys
import os
from pathlib import Path

# Add root directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

import cv2
from ocr import preprocess
from trocr_inference import ocr_with_trocr
from config import TROCR_MODEL_PATH

crops = [
    "crops/Esmeeees/20260612_130415_line1_384a.png",
    "crops/Esmeeees/20260612_130429_line0_9c47.png",
    "crops/Esmeeees/20260612_130445_line1_384a.png"
]

for c_path in crops:
    crop_path = root_dir / c_path
    if not crop_path.exists():
        print(f"Error: Crop not found at {c_path}")
        continue
    img = cv2.imread(str(crop_path))
    padded, icons_removed, gun_icon_positions = preprocess(img)
    text, conf = ocr_with_trocr(padded, gun_icon_positions, TROCR_MODEL_PATH)
    print(f"\nFile: {c_path}")
    print(f"  OCR:  {text!r}")
    print(f"  Conf: {conf:.4f}")
