import cv2
import numpy as np
from pathlib import Path

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    # Extract Slice 4 (y=96, height=42) which contains the complex player/shield-break line
    crop = img[96:138, :]
    
    # Apply Bilateral Smoothing + Grayscale Inversion
    smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
    gray = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    
    # Upscale 2x and Pad 15px like the production preprocessing pipeline
    upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # Save the output to artifacts
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    cv2.imwrite(str(out_dir / "sample_bilateral_smooth.png"), padded)
    print("Bilateral smooth sample saved successfully.")

if __name__ == "__main__":
    main()
