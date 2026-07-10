import cv2
import pytesseract
from pathlib import Path

TESS_CONFIG = "--oem 3 --psm 6 -l eng"

def main():
    brain_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\a52ac73c-e640-491c-b4ec-ba79ac991851")
    legacy_path = brain_dir / "debug_preprocess_legacy.png"
    hsv_path = brain_dir / "debug_preprocess_hsv.png"
    
    if not legacy_path.exists() or not hsv_path.exists():
        print("Preprocessed images not found.")
        return
        
    img_legacy = cv2.imread(str(legacy_path), cv2.IMREAD_GRAYSCALE)
    img_hsv = cv2.imread(str(hsv_path), cv2.IMREAD_GRAYSCALE)
    
    text_legacy = pytesseract.image_to_string(img_legacy, config=TESS_CONFIG).strip()
    text_hsv = pytesseract.image_to_string(img_hsv, config=TESS_CONFIG).strip()
    
    print("=== TESSERACT OCR OUTPUT ===")
    print("Legacy preprocessing text:")
    print(repr(text_legacy))
    print("\nHSV preprocessing text:")
    print(repr(text_hsv))

if __name__ == "__main__":
    main()
