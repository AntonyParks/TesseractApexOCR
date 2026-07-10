import sys
import cv2
import numpy as np
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from detect_killfeed import _refine_regions, detect_killfeed_regions

def test_detect_split():
    crop_path = Path("crops/Wavybenji_/20260611_201928_line0_28fd.png")
    if not crop_path.exists():
        print("Crop image not found")
        return
        
    img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
    print(f"Loaded crop shape: {img.shape}")
    
    # Revert padding and scaling
    h, w = img.shape
    unpadded = img[15:h-15, 15:w-15]
    orig_h = unpadded.shape[0] // 2
    orig_w = unpadded.shape[1] // 2
    orig_img = cv2.resize(unpadded, (orig_w, orig_h), interpolation=cv2.INTER_AREA)
    
    # Invert to white text on black background for brightness map simulation
    crop_bmap = cv2.bitwise_not(orig_img)
    crop_bmap = (crop_bmap > 127).astype(np.float32)
    
    # Pad width to 800 to simulate a realistic search box size and avoid the 95% full-width banner filter
    bmap = np.zeros((orig_h, 800), dtype=np.float32)
    bmap[:, :orig_w] = crop_bmap
    
    print(f"Simulated brightness map shape: {bmap.shape}")
    
    # We call detect_killfeed_regions directly with origin_x=0, origin_y=0
    regions = detect_killfeed_regions(bmap, 0, 0, frame_w=1920)
    print(f"Detected regions before refinement: {len(regions)}")
    for i, r in enumerate(regions):
        print(f"  [{i}]: {r}")
        
    # Standard refinement (calls our new _force_split_tall_region inside)
    refined = _refine_regions(regions, bmap, 0, 0, verbose=True)
    print(f"Refined regions: {len(refined)}")
    for i, r in enumerate(refined):
        print(f"  [{i}]: {r}")
        
    # Verify split happened
    if len(refined) >= 2:
        print("SUCCESS: Split double-line crop into separate single-line regions!")
    else:
        print("FAILURE: Double-line crop was not split.")

if __name__ == "__main__":
    test_detect_split()
