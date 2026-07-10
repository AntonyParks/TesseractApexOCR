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
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    
    # Load the generated images
    img_bin = cv2.imread(str(out_dir / "comparison_y96_baseline.png"))
    img_bilat = cv2.imread(str(out_dir / "comparison_y96_bilat.png"))
    img_stencil_gray = cv2.imread(str(out_dir / "comparison_y96_stencil.png"))
    img_stencil_v = cv2.imread(str(out_dir / "comparison_y96_stencil_value.png"))
    
    print("=" * 80)
    print("TrOCR TRANSCRIPTION COMPARISON ON Y=96")
    print("=" * 80)
    
    for name, img in [
        ("1. Baseline (Inverted Binary Mask)", img_bin),
        ("2. Bilateral Smooth Grayscale", img_bilat),
        ("3. HSV-Masked Grayscale Stencil", img_stencil_gray),
        ("4. HSV-Masked HSV Value Stencil", img_stencil_v)
    ]:
        if img is None:
            print(f"Error loading {name}")
            continue
        text, conf = run_trocr(img)
        print(f"  {name}:")
        print(f"    - Text: {text!r}")
        print(f"    - Conf: {conf:.4f}")

if __name__ == "__main__":
    main()
