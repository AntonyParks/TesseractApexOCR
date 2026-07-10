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

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    # Extract Slice 2 (y=88) which contains player names in yellow/red against background
    crop = img[88:133, :]
    
    # 1. Generate HSV color mask (White + Yellow + Red)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    combined_mask = mask_white | mask_yellow | mask_red
    
    # 2. Dilate the mask slightly (1 pixel) to capture anti-aliasing edges
    kernel = np.ones((3, 3), np.uint8)
    dilated_mask = cv2.dilate(combined_mask, kernel, iterations=1)
    
    # 3. Create Inverted Grayscale
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    inverted_gray = cv2.bitwise_not(gray)
    
    # 4. Apply stencil: Keep gray details inside mask, force white (255) outside mask
    stencil_gray = np.where(dilated_mask > 0, inverted_gray, 255)
    
    # Process like standard pipeline (Upscale 2x and Pad 15px)
    upscaled = cv2.resize(stencil_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # Save the output
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / "sample_stencil_grayscale.png"), padded)
    print("Saved stencil grayscale crop.")
    
    # Run TrOCR evaluation
    ocr_text, conf = run_trocr(padded)
    print(f"\n[+] TrOCR Results on Dilated Stencil Grayscale:")
    print(f"    - Text: {ocr_text!r}")
    print(f"    - Conf: {conf:.4f}")

if __name__ == "__main__":
    main()
