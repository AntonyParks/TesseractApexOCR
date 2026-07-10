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
    crop = img[90:112, :] # Exact single player line
    
    # 1. Pure Otsu Binarization
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    otsu = apply_otsu_threshold(gray)
    upscaled_otsu = cv2.resize(otsu, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded_otsu = cv2.copyMakeBorder(upscaled_otsu, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # 2. Bilateral Smooth + Otsu Binarization (smoothes noise before binarizing)
    smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
    gray_smoothed = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    otsu_smoothed = apply_otsu_threshold(gray_smoothed)
    upscaled_otsu_smooth = cv2.resize(otsu_smoothed, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded_otsu_smooth = cv2.copyMakeBorder(upscaled_otsu_smooth, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # Save the debug images
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / "single_Player_otsu.png"), padded_otsu)
    cv2.imwrite(str(out_dir / "single_Player_otsu_smooth.png"), padded_otsu_smooth)
    
    print("=" * 80)
    print("OTSU OTSU-SMOOTH TRANSCRIPTION EVALUATION")
    print("=" * 80)
    
    for name, img_data in [
        ("1. Pure Otsu Binarization", padded_otsu),
        ("2. Bilateral Smooth + Otsu", padded_otsu_smooth)
    ]:
        text, conf = run_trocr(img_data)
        print(f"  {name}:")
        print(f"    - Text: {text!r}")
        print(f"    - Conf: {conf:.4f}")

if __name__ == "__main__":
    main()
