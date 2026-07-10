import cv2
import numpy as np
from pathlib import Path

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    crop = img[96:138, :] # Slice y=96
    
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Baseline
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    combined_mask = mask_white | mask_yellow | mask_red
    inv_bin = cv2.bitwise_not(combined_mask)
    padded_bin = cv2.copyMakeBorder(cv2.resize(inv_bin, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    cv2.imwrite(str(out_dir / "comparison_y96_baseline.png"), padded_bin)
    
    # 2. Bilateral
    smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
    gray = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    inv_gray = cv2.bitwise_not(gray)
    padded_bilat = cv2.copyMakeBorder(cv2.resize(inv_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    cv2.imwrite(str(out_dir / "comparison_y96_bilat.png"), padded_bilat)
    
    # 3. HSV-Masked Grayscale Stencil (with 1px Dilation)
    kernel = np.ones((3, 3), np.uint8)
    dilated_mask = cv2.dilate(combined_mask, kernel, iterations=1)
    raw_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    inv_raw_gray = cv2.bitwise_not(raw_gray)
    stencil_gray = np.where(dilated_mask > 0, inv_raw_gray, 255)
    padded_stencil = cv2.copyMakeBorder(cv2.resize(stencil_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    cv2.imwrite(str(out_dir / "comparison_y96_stencil.png"), padded_stencil)
    
    # 4. HSV-Masked Value Channel Stencil (with 1px Dilation)
    # Let's also try using the HSV Value channel instead of Grayscale
    _, _, v_chan = cv2.split(hsv)
    inv_v = cv2.bitwise_not(v_chan)
    stencil_v = np.where(dilated_mask > 0, inv_v, 255)
    padded_stencil_v = cv2.copyMakeBorder(cv2.resize(stencil_v, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    cv2.imwrite(str(out_dir / "comparison_y96_stencil_value.png"), padded_stencil_v)
    
    print("Visual comparison crops saved successfully.")

if __name__ == "__main__":
    main()
