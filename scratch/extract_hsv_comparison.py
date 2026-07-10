import cv2
import numpy as np
from pathlib import Path

def main():
    # Prioritize full-size debug_frame.png
    img_paths = ["debug_frame.png", "debug_sang.png", "debug_ranked.png"]
    src_img = None
    src_path = None
    
    for path_str in img_paths:
        p = Path(path_str)
        if p.exists():
            img = cv2.imread(str(p))
            if img is not None:
                src_img = img
                src_path = p
                break
                
    if src_img is None:
        print("No valid debug images found in the workspace root.")
        return
        
    print(f"Loaded {src_path.name} with shape {src_img.shape}")
    h, w = src_img.shape[:2]
    
    # If the image is large, it's a full screenshot, so we crop the killfeed area
    if w >= 1280 and h >= 720:
        print("Image looks like a full frame. Extracting killfeed region...")
        # We will try to use the actual detector or fall back
        try:
            # Add workspace root to sys.path so we can import local modules from scratch directory
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from detect_killfeed import detect_killfeed_from_frame, detect_content_x_bounds
            x0, x1 = detect_content_x_bounds(src_img)
            regions = detect_killfeed_from_frame(src_img, w, h, content_x0=x0, content_x1=x1)
            if regions:
                reg = regions[0]
                rx, ry, rw, rh = reg["left"], reg["top"], reg["width"], reg["height"]
            else:
                rx, ry, rw, rh = int(w * 0.78), int(h * 0.32), int(w * 0.20), int(h * 0.04)
        except Exception as e:
            print(f"Detector failed ({e}), using fallback crop parameters.")
            rx, ry, rw, rh = int(w * 0.78), int(h * 0.32), int(w * 0.20), int(h * 0.04)
            
        print(f"Selected crop region: x={rx}, y={ry}, w={rw}, h={rh}")
        raw_crop = src_img[ry:ry+rh, rx:rx+rw]
    else:
        # The image is already a crop/slice
        print("Image is already crop-sized. Using full image.")
        raw_crop = src_img

    if raw_crop is None or raw_crop.size == 0:
        print("Error: Extracted crop is empty.")
        return

    # Create the output directory in artifacts
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    raw_path = out_dir / "sample_raw_crop.png"
    cv2.imwrite(str(raw_path), raw_crop)
    print(f"Saved raw crop to: {raw_path}")
    
    # Convert to HSV
    hsv = cv2.cvtColor(raw_crop, cv2.COLOR_BGR2HSV)
    
    # Save the HSV channels as grayscale images
    h_chan, s_chan, v_chan = cv2.split(hsv)
    cv2.imwrite(str(out_dir / "sample_hsv_h.png"), h_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_s.png"), s_chan)
    cv2.imwrite(str(out_dir / "sample_hsv_v.png"), v_chan)
    
    # Replicate the ocr.py masks:
    # White Mask (high value, low saturation)
    lower_white = np.array([0, 0, 160])
    upper_white = np.array([180, 45, 255])
    mask_white = cv2.inRange(hsv, lower_white, upper_white)
    
    # Yellow Mask (teammate names)
    lower_yellow = np.array([15, 60, 140])
    upper_yellow = np.array([35, 255, 255])
    mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)
    
    # Red Mask (enemy names)
    lower_red1 = np.array([0, 60, 120])
    upper_red1 = np.array([12, 255, 255])
    lower_red2 = np.array([168, 60, 120])
    upper_red2 = np.array([180, 255, 255])
    mask_red1 = cv2.inRange(hsv, lower_red1, upper_red1)
    mask_red2 = cv2.inRange(hsv, lower_red2, upper_red2)
    
    combined_mask = mask_white | mask_yellow | mask_red1 | mask_red2
    inverted = cv2.bitwise_not(combined_mask)
    
    # Save the combined mask and the inverted (final) crop
    cv2.imwrite(str(out_dir / "sample_combined_mask.png"), combined_mask)
    cv2.imwrite(str(out_dir / "sample_preprocessed_crop.png"), inverted)
    print("Saved HSV channels, combined mask, and preprocessed crops successfully.")

if __name__ == "__main__":
    main()
