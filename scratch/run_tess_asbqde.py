import sys
import cv2
from pathlib import Path

# Add root directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from ocr import preprocess, ocr_with_positions
from config import TESSERACT_CONFIG

crop_path = root_dir / "crops" / "Esmeeees" / "20260612_130445_line1_384a.png"
img = cv2.imread(str(crop_path))
padded, icons_removed, gun_icon_positions = preprocess(img)

text = ocr_with_positions(padded, TESSERACT_CONFIG)
print("=== Tesseract OCR ===")
print(f"Text: {text!r}")
