import cv2
import numpy as np
import sys
import math
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

def run_trocr(processed_img: np.ndarray) -> tuple[str, float]:
    """Run local TrOCR model on grayscale or 3-channel color image."""
    try:
        import torch
        from PIL import Image
        from trocr_inference import _load_model, _model, _processor
        
        # Load model using the existing singleton loader
        _load_model(Path(config.TROCR_MODEL_PATH))
        
        # Handle grayscale vs color
        if processed_img.ndim == 2:
            rgb = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2RGB)
            pil_img = Image.fromarray(rgb)
        else:
            # OpenCV loads as BGR, PIL/transformers expects RGB
            rgb = cv2.cvtColor(processed_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            
        device = next(_model.parameters()).device
        pixel_values = _processor(images=pil_img, return_tensors="pt").pixel_values.to(device)

        with torch.no_grad():
            output = _model.generate(
                pixel_values,
                max_new_tokens=64,
                return_dict_in_generate=True,
                output_scores=True,
            )

        text = _processor.batch_decode(output.sequences, skip_special_tokens=True)[0].strip()

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

def run_tesseract(processed_img: np.ndarray) -> str:
    try:
        text = ocr_with_positions(processed_img, config.TESSERACT_CONFIG)
        return text.strip()
    except Exception as e:
        return f"Error: {e}"

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    h, w = img.shape[:2]
    
    # Slices with known text
    slices = {
        "Teammate / Player Line (y=75)": img[75:120, :],
        "Attacker / Weapon / Victim Line (y=105)": img[105:150, :]
    }
    
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 80)
    print("PREPROCESSING OCR COMPARISON BENCHMARK (WITH COLOR)")
    print("=" * 80)
    
    for name, crop in slices.items():
        print(f"\nEvaluating: {name}")
        print("-" * 50)
        
        # 1. BASELINE: Inverted Binary Mask
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask_white = cv2.inRange(hsv, np.array([0, 0, 160]), np.array([180, 45, 255]))
        mask_yellow = cv2.inRange(hsv, np.array([15, 60, 140]), np.array([35, 255, 255]))
        mask_red = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([12, 255, 255])) | cv2.inRange(hsv, np.array([168, 60, 120]), np.array([180, 255, 255]))
        combined_mask = mask_white | mask_yellow | mask_red
        inverted = cv2.bitwise_not(combined_mask)
        upscaled_bin = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded_bin = cv2.copyMakeBorder(upscaled_bin, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        
        # 2. RAW GRAYSCALE (Inverted)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        inverted_gray = cv2.bitwise_not(gray)
        upscaled_inv_gray = cv2.resize(inverted_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded_inv_gray = cv2.copyMakeBorder(upscaled_inv_gray, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)

        # 3. HSV VALUE CHANNEL (Inverted)
        _, _, v_chan = cv2.split(hsv)
        inverted_v = cv2.bitwise_not(v_chan)
        upscaled_inv_v = cv2.resize(inverted_v, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded_inv_v = cv2.copyMakeBorder(upscaled_inv_v, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)

        # 4. FULL COLOR BGR (Original Light text)
        upscaled_color = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded_color = cv2.copyMakeBorder(upscaled_color, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        
        # 5. FULL COLOR BGR (Inverted Dark text)
        inverted_color = cv2.bitwise_not(crop)
        upscaled_inv_color = cv2.resize(inverted_color, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded_inv_color = cv2.copyMakeBorder(upscaled_inv_color, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=(255, 255, 255))

        # Save color crops for visual inspection
        safe_name = name.replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "").replace("=", "")
        cv2.imwrite(str(out_dir / f"{safe_name}_color.png"), padded_color)
        cv2.imwrite(str(out_dir / f"{safe_name}_inv_color.png"), padded_inv_color)
        
        # Run OCR Evaluators
        pipelines = {
            "1. Baseline (Inverted Binary Mask)": padded_bin,
            "2. Inverted Grayscale (Dark text)": padded_inv_gray,
            "3. Inverted HSV Value Channel (Dark text)": padded_inv_v,
            "4. Full Color BGR (Light text)": padded_color,
            "5. Full Color BGR (Inverted - Dark text)": padded_inv_color
        }
        
        for p_name, img_data in pipelines.items():
            t_text = run_tesseract(img_data)
            tr_text, tr_conf = run_trocr(img_data)
            
            print(f"  {p_name}:")
            print(f"    - Tesseract: {t_text!r}")
            print(f"    - TrOCR:     {tr_text!r} (Conf: {tr_conf:.4f})")
            
    print("\nBenchmark completed. Images saved to conversation artifacts directory.")

if __name__ == "__main__":
    main()
