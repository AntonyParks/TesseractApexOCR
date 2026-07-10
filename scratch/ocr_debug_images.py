import cv2
import sys
from pathlib import Path

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    import pytesseract
    from ocr import ocr_with_positions
    if hasattr(config, "TESSERACT_CMD") and config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
except Exception as e:
    print(f"Imports failed: {e}")

def main():
    img_paths = ["debug_sang.png", "debug_ranked.png"]
    for path_str in img_paths:
        p = Path(path_str)
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
            
        print(f"Image: {p.name}, size: {img.shape}")
        
        # Test direct OCR
        try:
            text = pytesseract.image_to_string(img).strip()
            print(f"  Direct OCR: {text!r}")
        except Exception as e:
            print(f"  Direct OCR failed: {e}")

if __name__ == "__main__":
    main()
