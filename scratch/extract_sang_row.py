import cv2
import numpy as np
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
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    h, w = img.shape[:2]
    
    # We will segment debug_sang.png horizontally into slices.
    # The image is 309 pixels tall, let's try 10 different slices of height ~40
    # and find the one that yields the best text.
    best_text = ""
    best_crop = None
    best_slice_idx = -1
    
    slice_height = 45
    for i in range(0, h - slice_height, 15):
        crop = img[i : i + slice_height, :]
        
        # Preprocess
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        lower_white = np.array([0, 0, 160])
        upper_white = np.array([180, 45, 255])
        mask_white = cv2.inRange(hsv, lower_white, upper_white)
        
        lower_yellow = np.array([15, 60, 140])
        upper_yellow = np.array([35, 255, 255])
        mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)
        
        lower_red1 = np.array([0, 60, 120])
        upper_red1 = np.array([12, 255, 255])
        lower_red2 = np.array([168, 60, 120])
        upper_red2 = np.array([180, 255, 255])
        mask_red1 = cv2.inRange(hsv, lower_red1, upper_red1)
        mask_red2 = cv2.inRange(hsv, lower_red2, upper_red2)
        
        combined_mask = mask_white | mask_yellow | mask_red1 | mask_red2
        inverted = cv2.bitwise_not(combined_mask)
        
        upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        try:
            text = ocr_with_positions(padded, config.TESSERACT_CONFIG).strip()
        except Exception:
            text = pytesseract.image_to_string(padded, config=config.TESSERACT_CONFIG).strip()
            
        density = sum(c.isalnum() for c in text)
        print(f"Slice at y={i} -> Text: {text!r} (Density: {density})")
        
        if density > sum(c.isalnum() for c in best_text):
            best_text = text
            best_crop = crop
            best_slice_idx = i

    if best_crop is None:
        print("Could not find any slice with text.")
        return
        
    print(f"\n[+] Best slice found at y={best_slice_idx}")
    print(f"    OCR Text: {best_text!r}")
    
    # Save BGR and intermediate stages
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    cv2.imwrite(str(out_dir / "sample_raw_crop.png"), best_crop)
    
    # Convert to HSV
    hsv = cv2.cvtColor(best_crop, cv2.COLOR_BGR2HSV)
    h_chan, s_chan, v_chan = cv2.split(hsv)
    cv2.imwrite(str(out_dir / "sample_hsv_h.png"), h_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_s.png"), s_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_v.png"), v_chan)
    
    # Reconstruct combined mask and processed image
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    
    combined_mask = mask_white | mask_yellow | mask_red
    inverted = cv2.bitwise_not(combined_mask)
    
    upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    cv2.imwrite(str(out_dir / "sample_combined_mask.png"), combined_mask)
    cv2.imwrite(str(out_dir / "sample_preprocessed_crop.png"), padded)
    print("Saved BGR, HSV, and processed outputs successfully.")

if __name__ == "__main__":
    main()
