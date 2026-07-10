import cv2
import numpy as np
import sys
from pathlib import Path

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    import pytesseract
    from detect_killfeed import detect_killfeed_from_frame, detect_content_x_bounds
    from ocr import ocr_with_positions
    if hasattr(config, "TESSERACT_CMD") and config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
except Exception as e:
    print(f"Imports failed: {e}")

def get_text_density(text: str) -> int:
    """Return count of alphanumeric characters to find text density."""
    return sum(c.isalnum() for c in text)

def main():
    img_paths = ["debug_frame.png", "debug_sang.png", "debug_ranked.png"]
    best_crop = None
    best_text = ""
    best_reg = None
    best_img_name = ""
    
    for path_str in img_paths:
        p = Path(path_str)
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
            
        h, w = img.shape[:2]
        
        # Detect regions
        try:
            x0, x1 = detect_content_x_bounds(img)
            regions = detect_killfeed_from_frame(img, w, h, content_x0=x0, content_x1=x1)
        except Exception:
            # Fallback segmenting into horizontal stripes in typical area
            regions = []
            for y in range(250, 550, 30):
                regions.append({"left": int(w * 0.75), "top": y, "width": int(w * 0.23), "height": 28})
                
        print(f"Scanned {p.name} ({w}x{h}), found {len(regions)} candidate regions.")
        
        for reg in regions:
            rx, ry, rw, rh = reg["left"], reg["top"], reg["width"], reg["height"]
            
            # Ensure coordinates are within image bounds
            if rx < 0 or ry < 0 or rx + rw > w or ry + rh > h or rw <= 0 or rh <= 0:
                continue
                
            crop = img[ry:ry+rh, rx:rx+rw]
            
            # Run basic preprocessing and Tesseract to verify if it has text
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
                
            density = get_text_density(text)
            print(f"  Region x={rx}, y={ry} -> Text: {text!r} (Density: {density})")
            
            if density > get_text_density(best_text):
                best_text = text
                best_crop = crop
                best_reg = reg
                best_img_name = p.name

    if best_crop is None:
        print("Could not find any region with valid text.")
        return
        
    print(f"\n[+] Best active text found in {best_img_name} at x={best_reg['left']}, y={best_reg['top']}")
    print(f"    OCR Text: {best_text!r}")
    
    # Save the selected BGR crop and intermediate stages to artifacts folder
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    cv2.imwrite(str(out_dir / "sample_raw_crop.png"), best_crop)
    
    # Convert to HSV
    hsv = cv2.cvtColor(best_crop, cv2.COLOR_BGR2HSV)
    h_chan, s_chan, v_chan = cv2.split(hsv)
    cv2.imwrite(str(out_dir / "sample_hsv_h.png"), h_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_s.png"), s_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_v.png"), v_chan)
    
    # Generate Masks
    mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
    mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
    mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
    
    combined_mask = mask_white | mask_yellow | mask_red
    inverted = cv2.bitwise_not(combined_mask)
    
    # Resize and Pad like ocr.py
    upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    cv2.imwrite(str(out_dir / "sample_combined_mask.png"), combined_mask)
    cv2.imwrite(str(out_dir / "sample_preprocessed_crop.png"), padded)
    print("Done. Saved BGR, HSV, and processed outputs.")

if __name__ == "__main__":
    main()
