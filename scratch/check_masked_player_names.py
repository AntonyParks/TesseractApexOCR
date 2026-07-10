import cv2
import numpy as np
from pathlib import Path

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    crop = img[88:133, :]
    
    # 1. Generate HSV color mask (White + Yellow + Red)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red1 = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255]))
    mask_red2 = cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    mask_red = mask_red1 | mask_red2
    
    combined_mask = mask_white | mask_yellow | mask_red
    
    # Let's save the individual masks to see which one failed
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    cv2.imwrite(str(out_dir / "debug_mask_white.png"), mask_white)
    cv2.imwrite(str(out_dir / "debug_mask_yellow.png"), mask_yellow)
    cv2.imwrite(str(out_dir / "debug_mask_red.png"), mask_red)
    cv2.imwrite(str(out_dir / "debug_mask_combined.png"), combined_mask)
    
    # Let's check how many pixels are in the yellow mask (teammate) and red mask (enemy)
    print(f"White mask active pixels: {np.sum(mask_white > 0)}")
    print(f"Yellow mask active pixels: {np.sum(mask_yellow > 0)}")
    print(f"Red mask active pixels: {np.sum(mask_red > 0)}")

if __name__ == "__main__":
    main()
