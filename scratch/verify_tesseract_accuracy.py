import csv
import cv2
import numpy as np
import pytesseract
from pathlib import Path
from difflib import SequenceMatcher

TESS_CONFIG = "--oem 3 --psm 7 -l eng"

def similarity(a: str, b: str) -> float:
    # Normalize spaces and lower case
    a_norm = " ".join(a.lower().split())
    b_norm = " ".join(b.lower().split())
    # Standardize <GUN_ICON> representations
    a_norm = a_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    b_norm = b_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    return SequenceMatcher(None, a_norm, b_norm).ratio()

def ocr_with_positions(processed_img, config):
    try:
        data = pytesseract.image_to_data(processed_img, config=config, output_type=pytesseract.Output.DICT)
        text_parts = []
        prev_right = 0
        for i, word in enumerate(data['text']):
            if not word.strip():
                continue
            left = data['left'][i]
            if prev_right > 0:
                gap_size = left - prev_right
                if gap_size > 80:
                    text_parts.append(" <GUN_ICON> ")
                elif gap_size > 10:
                    text_parts.append(" ")
            text_parts.append(word)
            prev_right = left + data['width'][i]
        return ''.join(text_parts)
    except Exception:
        return pytesseract.image_to_string(processed_img, config=config).strip()

def main():
    csv_path = Path(__file__).resolve().parent.parent / "labels" / "labels_clean.csv"
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return

    # Gather English high-quality crops
    records = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["streamer"] == "Faide" and row["quality"] == "high" and row["label"].strip():
                # Make sure label is primarily english/symbols
                label = row["label"]
                if not any(ord(c) > 127 for c in label if c not in "✔️"):
                    records.append(row)

    print(f"Total English high-quality Faide records found: {len(records)}")
    if not records:
        return

    # Sample 100 records for validation
    import random
    random.seed(42)
    sample_records = random.sample(records, min(100, len(records)))
    print(f"Evaluating {len(sample_records)} random crops...\n")

    no_pad_scores = []
    pad_scores = []
    
    no_pad_above_80 = 0
    pad_above_80 = 0

    for idx, row in enumerate(sample_records):
        filepath = Path(__file__).resolve().parent.parent / row["filepath"]
        if not filepath.exists():
            continue
            
        img = cv2.imread(str(filepath), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
            
        ground_truth = row["label"].strip()
        
        # 1. OCR without adding padding
        text_no_pad = ocr_with_positions(img, TESS_CONFIG).strip()
        sim_no_pad = similarity(text_no_pad, ground_truth)
        no_pad_scores.append(sim_no_pad)
        if sim_no_pad >= 0.80:
            no_pad_above_80 += 1
            
        # 2. OCR with 15px white padding added
        padded = cv2.copyMakeBorder(
            img, 15, 15, 15, 15,
            cv2.BORDER_CONSTANT, value=255
        )
        text_pad = ocr_with_positions(padded, TESS_CONFIG).strip()
        sim_pad = similarity(text_pad, ground_truth)
        pad_scores.append(sim_pad)
        if sim_pad >= 0.80:
            pad_above_80 += 1
            
        if idx < 5:  # print a few examples
            print(f"Example {idx+1}:")
            print(f"  GT:      '{ground_truth}'")
            print(f"  No Pad:  '{text_no_pad}' (sim: {sim_no_pad:.2%})")
            print(f"  Padded:  '{text_pad}' (sim: {sim_pad:.2%})")
            print()

    print("=" * 50)
    print("RESULTS SUMMARY")
    print("=" * 50)
    print(f"Average Similarity without padding: {np.mean(no_pad_scores):.2%}")
    print(f"Average Similarity with 15px padding: {np.mean(pad_scores):.2%}")
    print("-" * 50)
    print(f"Crops matching >= 80% without padding: {no_pad_above_80} / {len(no_pad_scores)} ({no_pad_above_80 / len(no_pad_scores):.2%})")
    print(f"Crops matching >= 80% with 15px padding: {pad_above_80} / {len(pad_scores)} ({pad_above_80 / len(pad_scores):.2%})")
    print("=" * 50)

if __name__ == "__main__":
    main()
