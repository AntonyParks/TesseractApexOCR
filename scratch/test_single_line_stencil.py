import cv2
import numpy as np
import sys
from pathlib import Path

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    from comprehensive_preprocessing_experiment import run_trocr
except Exception as e:
    print(f"Imports failed: {e}")

def get_preprocessed(crop: np.ndarray, mode: str) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    
    # Generate combined HSV mask
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    combined_mask = mask_white | mask_yellow | mask_red
    
    if mode == "baseline":
        inv = cv2.bitwise_not(combined_mask)
    elif mode == "bilateral":
        smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
        g = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
        inv = cv2.bitwise_not(g)
    elif mode == "stencil_gray":
        kernel = np.ones((3, 3), np.uint8)
        dilated = cv2.dilate(combined_mask, kernel, iterations=1)
        inv_gray = cv2.bitwise_not(gray)
        inv = np.where(dilated > 0, inv_gray, 255)
    elif mode == "stencil_value":
        kernel = np.ones((3, 3), np.uint8)
        dilated = cv2.dilate(combined_mask, kernel, iterations=1)
        _, _, v_chan = cv2.split(hsv)
        inv_v = cv2.bitwise_not(v_chan)
        inv = np.where(dilated > 0, inv_v, 255)
    else:
        inv = cv2.bitwise_not(gray)
        
    upscaled = cv2.resize(inv, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    return padded

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    # Single-line crops (exactly 22 pixels tall)
    lines = {
        "1. Player Name Row (y=90, h=22)": img[90:112, :],
        "2. Shield Broken Row (y=112, h=22)": img[112:134, :]
    }
    
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    for name, crop in lines.items():
        print(f"\n=======================================================")
        print(f"Evaluating: {name}")
        print(f"=======================================================")
        
        modes = ["baseline", "bilateral", "stencil_gray", "stencil_value"]
        for mode in modes:
            prep_img = get_preprocessed(crop, mode)
            
            # Save images for visual comparison
            safe_name = name.split(" ")[1] # Player / Shield
            cv2.imwrite(str(out_dir / f"single_{safe_name}_{mode}.png"), prep_img)
            
            text, conf = run_trocr(prep_img)
            print(f"  {mode:<15}: {text!r} (Conf: {conf:.4f})")

if __name__ == "__main__":
    main()
