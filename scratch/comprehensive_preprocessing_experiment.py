import cv2
import numpy as np
import sys
import math
import os
import time
from pathlib import Path
from collections import defaultdict
import statistics
from difflib import SequenceMatcher

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    from gemini_validator import validate_killfeed_crop
except Exception as e:
    print(f"Imports failed: {e}")

def run_trocr(processed_img: np.ndarray) -> tuple[str, float]:
    """Run local TrOCR model supporting grayscale or color input."""
    try:
        import torch
        from PIL import Image
        import trocr_inference
        
        # Load model using singleton loader
        trocr_inference._load_model(Path(config.TROCR_MODEL_PATH))
        
        if processed_img.ndim == 2:
            rgb = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2RGB)
            pil_img = Image.fromarray(rgb)
        else:
            rgb = cv2.cvtColor(processed_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            
        device = next(trocr_inference._model.parameters()).device
        pixel_values = trocr_inference._processor(images=pil_img, return_tensors="pt").pixel_values.to(device)

        with torch.no_grad():
            output = trocr_inference._model.generate(
                pixel_values,
                max_new_tokens=64,
                return_dict_in_generate=True,
                output_scores=True,
            )

        text = trocr_inference._processor.batch_decode(output.sequences, skip_special_tokens=True)[0].strip()

        # Compute confidence
        scores = output.scores
        if scores:
            log_sum = 0.0
            for step in scores:
                probs = torch.softmax(step, dim=-1)
                max_prob = float(probs.max(dim=-1).values[0])
                log_sum += math.log(max_prob) if max_prob > 0 else -10.0
            confidence = math.exp(log_sum / len(scores))
        else:
            confidence = 1.0

        return text, confidence
    except Exception as e:
        return f"Error: {e}", 0.0

def apply_otsu_threshold(gray_crop: np.ndarray) -> np.ndarray:
    """Otsu threshold binarization filter."""
    _, thresh = cv2.threshold(gray_crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(thresh) < 127:
        thresh = cv2.bitwise_not(thresh)
    return thresh

def apply_adaptive_threshold(gray_crop: np.ndarray) -> np.ndarray:
    """Adaptive Gaussian threshold binarization filter."""
    thresh = cv2.adaptiveThreshold(
        gray_crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
        cv2.THRESH_BINARY_INV, 11, 2
    )
    inverted = cv2.bitwise_not(thresh)
    return inverted

def apply_clahe(crop: np.ndarray) -> np.ndarray:
    """Contrast Limited Adaptive Histogram Equalization filter."""
    yuv = cv2.cvtColor(crop, cv2.COLOR_BGR2YUV)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    yuv[:, :, 0] = clahe.apply(yuv[:, :, 0])
    equalized = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    gray = cv2.cvtColor(equalized, cv2.COLOR_BGR2GRAY)
    return cv2.bitwise_not(gray)

def apply_bilateral_smoothing(crop: np.ndarray) -> np.ndarray:
    """Bilateral filter + grayscale conversion."""
    smoothed = cv2.bilateralFilter(crop, 9, 75, 75)
    gray = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    return cv2.bitwise_not(gray)

def main():
    img_paths = ["debug_sang.png", "debug_frame.png", "debug_ranked.png"]
    slices = []
    
    print("[*] Scanning debug images for text-bearing regions...")
    for path_str in img_paths:
        p = Path(path_str)
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
            
        h, w = img.shape[:2]
        slice_h = 42
        
        # Scan image vertically in overlapping increments
        for y in range(0, h - slice_h, 8):
            crop = img[y : y + slice_h, :]
            
            # Simple baseline OCR check to see if slice has text
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
            mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
            mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255]))
            combined = mask_white | mask_yellow | mask_red
            inverted = cv2.bitwise_not(combined)
            
            upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
            
            # Get a quick TrOCR read to verify if it has readable text
            text, _ = run_trocr(padded)
            density = sum(c.isalnum() for c in text)
            if density >= 6:
                # Deduplicate very close coordinates
                is_dup = False
                for existing in slices:
                    if existing["src"] == p.name and abs(existing["y"] - y) < 20:
                        is_dup = True
                        if density > existing["density"]:
                            existing["y"] = y
                            existing["crop"] = crop
                            existing["density"] = density
                        break
                if not is_dup:
                    slices.append({
                        "src": p.name,
                        "y": y,
                        "crop": crop,
                        "density": density
                    })

    print(f"[+] Found {len(slices)} candidate slices containing killfeed text.")
    if len(slices) == 0:
        print("No text slices detected. Exiting.")
        return

    # Cap at 10 slices for a clean, rate-limit safe sample size
    selected_slices = slices[:10]
    print(f"[*] Proceeding with N={len(selected_slices)} slices for benchmark evaluation.")
    print("[*] Added a 5-second delay between Gemini calls to prevent 429 rate limit errors.\n")
    
    # Preprocessing pipeline definitions
    pipeline_names = [
        "1. Baseline (Inverted Binary Mask)",
        "2. Inverted Grayscale (Dark text)",
        "3. Inverted HSV Value (Dark text)",
        "4. Inverted Full Color BGR (Dark text)",
        "5. Otsu Threshold Binarization",
        "6. Adaptive Gaussian Thresholding",
        "7. CLAHE Contrast Grayscale",
        "8. Bilateral Smooth Grayscale"
    ]
    
    # Store aggregate scores
    avg_similarities = defaultdict(list)
    avg_confidences = defaultdict(list)
    
    for idx, sl in enumerate(selected_slices, 1):
        crop = sl["crop"]
        print(f"\n--- Slice #{idx} (Source: {sl['src']} at y={sl['y']}) ---")
        
        # Sleep for 5 seconds to ensure we do not hit the Gemini 429 rate limit
        time.sleep(5)
        
        # 1. Establish Gemini ground truth
        try:
            gemini_in = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            ground_truth = validate_killfeed_crop(gemini_in)
            if not ground_truth:
                raise ValueError("Gemini returned empty read.")
            ground_truth = ground_truth.strip()
        except Exception as e:
            # Fallback consensus using baseline TrOCR if Gemini still fails
            print(f"  [!] Gemini Ground Truth extraction failed ({e}). Using fallback baseline read.")
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            combined = cv2.inRange(hsv, np.array([0,0,160]), np.array([180,45,255])) | cv2.inRange(hsv, np.array([15,60,140]), np.array([35,255,255]))
            inv = cv2.bitwise_not(combined)
            up = cv2.resize(inv, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            pad = cv2.copyMakeBorder(up, 15,15,15,15, cv2.BORDER_CONSTANT, value=255)
            gt_text, _ = run_trocr(pad)
            ground_truth = gt_text if gt_text else "Unknown"

        print(f"  [GROUND TRUTH]: {ground_truth!r}")
        
        # Generate the 8 variations
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        
        # V1: Baseline
        mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
        mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
        mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
        combined_mask = mask_white | mask_yellow | mask_red
        inv_bin = cv2.bitwise_not(combined_mask)
        v1_img = cv2.copyMakeBorder(cv2.resize(inv_bin, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V2: Grayscale
        inv_gray = cv2.bitwise_not(gray)
        v2_img = cv2.copyMakeBorder(cv2.resize(inv_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V3: Value
        _, _, v_chan = cv2.split(hsv)
        inv_v = cv2.bitwise_not(v_chan)
        v3_img = cv2.copyMakeBorder(cv2.resize(inv_v, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V4: Color BGR
        inv_color = cv2.bitwise_not(crop)
        v4_img = cv2.copyMakeBorder(cv2.resize(inv_color, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        
        # V5: Otsu
        otsu = apply_otsu_threshold(gray)
        v5_img = cv2.copyMakeBorder(cv2.resize(otsu, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V6: Adaptive
        adapt = apply_adaptive_threshold(gray)
        v6_img = cv2.copyMakeBorder(cv2.resize(adapt, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V7: CLAHE
        clahe = apply_clahe(crop)
        v7_img = cv2.copyMakeBorder(cv2.resize(clahe, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # V8: Bilateral
        bilat = apply_bilateral_smoothing(crop)
        v8_img = cv2.copyMakeBorder(cv2.resize(bilat, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        pipeline_images = [v1_img, v2_img, v3_img, v4_img, v5_img, v6_img, v7_img, v8_img]
        
        # Run TrOCR-only evaluation for this slice
        for name_p, img_p in zip(pipeline_names, pipeline_images):
            ocr_text, conf = run_trocr(img_p)
            similarity = SequenceMatcher(None, ground_truth.lower(), ocr_text.lower()).ratio()
            
            avg_similarities[name_p].append(similarity)
            avg_confidences[name_p].append(conf)
            
            print(f"    - {name_p[:38]:<38}: {ocr_text!r} (Sim={similarity:.2f}, Conf={conf:.2f})")
            
    # Print Final Summary Table
    print("\n" + "=" * 80)
    print("FINAL CUMULATIVE TrOCR PERFORMANCE MATRIX (N={})".format(len(selected_slices)))
    print("=" * 80)
    print(f"{'Preprocessing Filter':<44} | {'Avg Similarity':<15} | {'Avg TrOCR Conf':<15}")
    print("-" * 80)
    for name_p in pipeline_names:
        mean_sim = statistics.mean(avg_similarities[name_p]) if avg_similarities[name_p] else 0.0
        mean_conf = statistics.mean(avg_confidences[name_p]) if avg_confidences[name_p] else 0.0
        print(f"{name_p:<44} | {mean_sim:<15.2%} | {mean_conf:<15.4f}")
    print("=" * 80)

if __name__ == "__main__":
    main()
