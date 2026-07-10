import cv2
import numpy as np
import sys
from pathlib import Path

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    from comprehensive_preprocessing_experiment import run_trocr, apply_otsu_threshold
except Exception as e:
    print(f"Imports failed: {e}")

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    # We will test different vertical pad offsets around the y=90 line
    # y=90, h=22 means y range 90:112. Let's try expanding t0 and t1.
    tests = {
        "Pad +1 (y=89:113, h=24)": img[89:113, :],
        "Pad +2 (y=88:114, h=26)": img[88:114, :],
        "Pad +3 (y=87:115, h=28)": img[87:115, :],
        "Pad +4 (y=86:116, h=30)": img[86:116, :]
    }
    
    print("=" * 80)
    print("EVALUATING VERTICAL CROP PADDING ON PLAYER NAME")
    print("=" * 80)
    
    for name, crop in tests.items():
        print(f"\n--- {name} ---")
        
        # Test 1: Bilateral Smooth + Otsu
        smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
        gray_smooth = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
        otsu_smooth = apply_otsu_threshold(gray_smooth)
        prep_otsu = cv2.copyMakeBorder(cv2.resize(otsu_smooth, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # Test 2: Inverted HSV Value
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        _, _, v_chan = cv2.split(hsv)
        inv_v = cv2.bitwise_not(v_chan)
        prep_v = cv2.copyMakeBorder(cv2.resize(inv_v, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        text_otsu, conf_otsu = run_trocr(prep_otsu)
        text_v, conf_v = run_trocr(prep_v)
        
        print(f"  Bilateral + Otsu: {text_otsu!r} (Conf: {conf_otsu:.4f})")
        print(f"  HSV Value       : {text_v!r} (Conf: {conf_v:.4f})")

if __name__ == "__main__":
    main()
