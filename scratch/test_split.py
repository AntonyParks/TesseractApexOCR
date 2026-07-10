import cv2
import numpy as np
from pathlib import Path

def test_split():
    crop_path = Path("crops/Wavybenji_/20260611_201928_line0_28fd.png")
    if not crop_path.exists():
        print("Crop image not found")
        return
        
    img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
    print(f"Loaded crop shape: {img.shape}")
    
    # The crop is upscaled 2x and has 15px padding.
    # Let's remove the padding and downscale it back to the original size.
    # original_w = (padded_w - 30) // 2
    # original_h = (padded_h - 30) // 2
    h, w = img.shape
    unpadded = img[15:h-15, 15:w-15]
    orig_h = unpadded.shape[0] // 2
    orig_w = unpadded.shape[1] // 2
    orig_img = cv2.resize(unpadded, (orig_w, orig_h), interpolation=cv2.INTER_AREA)
    print(f"Original shape: {orig_img.shape}")
    
    # We simulate the brightness map by thresholding the original image.
    # (Since the crop is already preprocessed/binary, we can just invert it back to white text on black background)
    # The crop was saved as inverted (black text on white), so we invert it to get white text on black.
    bmap = cv2.bitwise_not(orig_img)
    # Normalize to 0 and 1
    bmap = (bmap > 127).astype(np.float32)
    
    row_proj = bmap.sum(axis=1)
    print("\nRow projection:")
    for i, val in enumerate(row_proj):
        print(f"Row {i:2d}: {val:.1f}")
        
    # Let's run the split logic
    h_orig = bmap.shape[0]
    if h_orig > 32:
        print(f"\nHeight {h_orig} > 32. Attempting split...")
        min_y = int(0.25 * h_orig)
        max_y = int(0.75 * h_orig)
        print(f"Search range for split: {min_y} to {max_y}")
        
        # Find minimum in this range
        sub_proj = row_proj[min_y:max_y+1]
        min_val = sub_proj.min()
        # Find all indices with the minimum value and pick the one closest to the center
        min_indices = np.where(sub_proj == min_val)[0] + min_y
        center = h_orig / 2.0
        split_idx = min(min_indices, key=lambda idx: abs(idx - center))
        
        print(f"Minimum value: {min_val} at row {split_idx}")
        print(f"Split lines: 0 to {split_idx - 1} (height {split_idx}) and {split_idx + 1} to {h_orig - 1} (height {h_orig - split_idx - 1})")

if __name__ == "__main__":
    test_split()
