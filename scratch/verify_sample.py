import sys
import csv
import cv2
import random
import numpy as np
from pathlib import Path
from difflib import SequenceMatcher

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trocr_inference import ocr_with_trocr

TROCR_MODEL_PATH = Path("models/trocr_apex")

def similarity(a: str, b: str) -> float:
    a_norm = " ".join(a.lower().split())
    b_norm = " ".join(b.lower().split())
    a_norm = a_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    b_norm = b_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    return SequenceMatcher(None, a_norm, b_norm).ratio()

def preprocess_hsv(img):
    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    else:
        bgr = img.copy()
        
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    
    # White Mask (high value, low saturation)
    lower_white = np.array([0, 0, 160])
    upper_white = np.array([180, 45, 255])
    
    # Yellow Mask (teammate names)
    lower_yellow = np.array([15, 60, 140])
    upper_yellow = np.array([35, 255, 255])
    
    # Red Mask (enemy names)
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
    return padded

def preprocess_legacy(img):
    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    else:
        bgr = img.copy()
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
    return padded

def main():
    csv_path = Path("labels/labels_clean.csv")
    if not csv_path.exists():
        print("CSV not found.", flush=True)
        return
        
    records = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filepath = Path(row["filepath"])
            if filepath.exists() and row["label"].strip():
                if not any(ord(c) > 127 for c in row["label"] if c not in "✔️"):
                    records.append(row)
                    
    print(f"Found {len(records)} matching records in labeled dataset.", flush=True)
    if not records:
        return
        
    # Sample 20 random crops
    random.seed(42)
    sample_size = min(20, len(records))
    sampled_records = random.sample(records, sample_size)
    print(f"Sampled {sample_size} crops for quick verification.", flush=True)
    
    legacy_scores = []
    hsv_scores = []
    
    legacy_exact = 0
    hsv_exact = 0
    
    print("Running TrOCR inference on sample crops...\n", flush=True)
    
    for idx, row in enumerate(sampled_records):
        img = cv2.imread(row["filepath"], cv2.IMREAD_COLOR)
        if img is None:
            continue
            
        gt = row["label"].strip()
        
        legacy_img = preprocess_legacy(img)
        hsv_img = preprocess_hsv(img)
        
        text_legacy, conf_legacy = ocr_with_trocr(legacy_img, [], TROCR_MODEL_PATH)
        text_hsv, conf_hsv = ocr_with_trocr(hsv_img, [], TROCR_MODEL_PATH)
        
        sim_legacy = similarity(text_legacy, gt)
        sim_hsv = similarity(text_hsv, gt)
        
        legacy_scores.append(sim_legacy)
        hsv_scores.append(sim_hsv)
        
        if sim_legacy >= 0.95:
            legacy_exact += 1
        if sim_hsv >= 0.95:
            hsv_exact += 1
            
        print(f"Crop {idx+1}/{sample_size} ({Path(row['filepath']).name}):", flush=True)
        print(f"  GT:     '{gt}'", flush=True)
        print(f"  Legacy: '{text_legacy}' (similarity: {sim_legacy:.2%}, conf: {conf_legacy:.2%})", flush=True)
        print(f"  HSV:    '{text_hsv}' (similarity: {sim_hsv:.2%}, conf: {conf_hsv:.2%})", flush=True)
        print("", flush=True)

    print("=" * 60, flush=True)
    print("COMPARATIVE TROCR ACCURACY RESULTS (SAMPLE)", flush=True)
    print("=" * 60, flush=True)
    print(f"Average Similarity (Legacy): {np.mean(legacy_scores):.2%}", flush=True)
    print(f"Average Similarity (HSV):    {np.mean(hsv_scores):.2%}", flush=True)
    print("-" * 60, flush=True)
    print(f"High-Accuracy matches (>= 95% similarity) under Legacy: {legacy_exact} / {sample_size} ({legacy_exact/sample_size:.2%})", flush=True)
    print(f"High-Accuracy matches (>= 95% similarity) under HSV:    {hsv_exact} / {sample_size} ({hsv_exact/sample_size:.2%})", flush=True)
    print("=" * 60, flush=True)

if __name__ == "__main__":
    main()
