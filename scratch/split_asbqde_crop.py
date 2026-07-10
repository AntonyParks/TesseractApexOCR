import sys
import cv2
import numpy as np
from pathlib import Path

# Add root directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from config import TROCR_MODEL_PATH
from trocr_inference import ocr_with_trocr
from detect_killfeed import _force_split_tall_region, _MAX_SINGLE_LINE_HEIGHT

crop_path = root_dir / "crops" / "Esmeeees" / "20260612_130445_line1_384a.png"
img = cv2.imread(str(crop_path))
print(f"Original image shape: {img.shape}")

# Convert to grayscale
if img.shape[2] == 4:
    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
else:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

# Since this crop is already preprocessed/padded, let's see:
# Let's revert the padding and scaling first
# The padding added in ocr.py preprocess() is 15px all around after 2x upscaling.
# Let's check if the crop file is the raw crop or the preprocessed crop.
# Let's run preprocess from ocr.py
from ocr import preprocess
padded, icons_removed, gun_icon_positions = preprocess(img)
print(f"Padded shape: {padded.shape}")

# Revert to binary map for splitting
# (In ocr.py, it's 2x upscaled and padded)
# Let's do the same revert as fix_what_crops.py
unpadded = gray[15:-15, 15:-15]
h_un, w_un = unpadded.shape
orig_h = h_un // 2
orig_w = w_un // 2
orig_img = cv2.resize(unpadded, (orig_w, orig_h), interpolation=cv2.INTER_AREA)

bmap = cv2.bitwise_not(orig_img)
bmap = (bmap > 127).astype(np.float32)

initial_region = {"left": 0, "top": 0, "width": orig_w, "height": orig_h}
split_regions = _force_split_tall_region(initial_region, bmap, 0, 0, _MAX_SINGLE_LINE_HEIGHT)
print(f"Split into {len(split_regions)} regions:")

for i, reg in enumerate(split_regions):
    l, t, w, h = reg["left"], reg["top"], reg["width"], reg["height"]
    sub_crop = orig_img[t:t+h, l:l+w]
    sub_upscaled = cv2.resize(sub_crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    sub_padded = cv2.copyMakeBorder(sub_upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    text, conf = ocr_with_trocr(sub_padded, [], TROCR_MODEL_PATH)
    print(f"  Region {i} ({reg}):")
    print(f"    OCR:  {text!r}")
    print(f"    Conf: {conf:.4f}")
