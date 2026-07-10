import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np
import pytesseract
from detect_killfeed import detect_killfeed_from_frame

TESS_CONFIG = "--oem 3 --psm 7 -l eng"

def preprocess_hsv(img):
    # img is BGRA
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    
    # White Mask (high value, low saturation)
    lower_white = np.array([0, 0, 160])
    upper_white = np.array([180, 45, 255])
    
    # Yellow Mask (teammate names)
    lower_yellow = np.array([15, 60, 140])
    upper_yellow = np.array([35, 255, 255])
    
    # Red Mask (enemy names, wraps around 0 and 180)
    lower_red1 = np.array([0, 60, 120])
    upper_red1 = np.array([12, 255, 255])
    lower_red2 = np.array([168, 60, 120])
    upper_red2 = np.array([180, 255, 255])
    
    mask_white = cv2.inRange(hsv, lower_white, upper_white)
    mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)
    mask_red1 = cv2.inRange(hsv, lower_red1, upper_red1)
    mask_red2 = cv2.inRange(hsv, lower_red2, upper_red2)
    
    combined_mask = mask_white | mask_yellow | mask_red1 | mask_red2
    inverted = cv2.bitwise_not(combined_mask)
    
    # Existing gun icon removal logic
    dark_mask = cv2.bitwise_not(inverted)
    contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    gun_icon_positions = []
    icons_removed = 0
    temp_inverted = inverted.copy()
    
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        aspect_ratio = w / h if h > 0 else 0
        if 2.0 < aspect_ratio < 6.0 and 15 < h < 40 and 40 < w < 100:
            cv2.rectangle(temp_inverted, (x, y), (x + w, y + h), (255, 255, 255), -1)
            gun_icon_positions.append(x + w // 2)
            icons_removed += 1
            
    upscaled = cv2.resize(temp_inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    return padded, icons_removed

def preprocess_legacy(img):
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, inverted = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
    
    _, dark_mask = cv2.threshold(inverted, 50, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    gun_icon_positions = []
    icons_removed = 0
    temp_inverted = inverted.copy()
    
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        aspect_ratio = w / h if h > 0 else 0
        if 2.0 < aspect_ratio < 6.0 and 15 < h < 40 and 40 < w < 100:
            cv2.rectangle(temp_inverted, (x, y), (x + w, y + h), (255, 255, 255), -1)
            gun_icon_positions.append(x + w // 2)
            icons_removed += 1
            
    upscaled = cv2.resize(temp_inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    return padded, icons_removed

def main():
    frame_path = Path("debug_frame.png")
    if not frame_path.exists():
        print("debug_frame.png not found.")
        return
        
    frame = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
    if frame is None:
        print("Failed to read debug_frame.png")
        return
        
    fh, fw = frame.shape[:2]
    # If image does not have an alpha channel, convert to BGRA
    if frame.shape[2] == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
        
    regions = detect_killfeed_from_frame(frame, fw, fh)
    print(f"Detected {len(regions)} regions in debug_frame.png")
    
    dest_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\a52ac73c-e640-491c-b4ec-ba79ac991851")
    
    for idx, r in enumerate(regions):
        left, top, w, h = r["left"], r["top"], r["width"], r["height"]
        print(f"\n--- Line {idx}: x={left}, y={top}, w={w}, h={h} ---")
        
        # Crop region with 2px vertical padding
        _PAD = 2
        t0 = max(0, top - _PAD)
        t1 = min(fh, top + h + _PAD)
        crop = frame[t0:t1, left:left + w]
        
        legacy_res, _ = preprocess_legacy(crop)
        hsv_res, _ = preprocess_hsv(crop)
        
        # Save output images
        cv2.imwrite(str(dest_dir / f"line_{idx}_legacy.png"), legacy_res)
        cv2.imwrite(str(dest_dir / f"line_{idx}_hsv.png"), hsv_res)
        
        # Run OCR
        text_legacy = pytesseract.image_to_string(legacy_res, config=TESS_CONFIG).strip()
        text_hsv = pytesseract.image_to_string(hsv_res, config=TESS_CONFIG).strip()
        
        print("Legacy OCR:", repr(text_legacy))
        print("HSV OCR:   ", repr(text_hsv))

if __name__ == "__main__":
    main()
