import os
import json
import base64
import random
import shutil
import sys
import cv2
import numpy as np
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from ocr import preprocess_for_easyocr

random.seed(42)

DATASET_DIR = Path("easyocr_dataset_v3")
TRAIN_DIR = DATASET_DIR / "train"
VAL_DIR = DATASET_DIR / "val"
INPUT_JSONL = Path("labels/batch_input.jsonl")
OUTPUT_JSONL = Path("labels/batch_results.jsonl")

def main():
    if not INPUT_JSONL.exists() or not OUTPUT_JSONL.exists():
        print("Missing JSONL batch files.")
        return

    # Create directories
    if DATASET_DIR.exists():
        shutil.rmtree(DATASET_DIR)
    
    TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    VAL_DIR.mkdir(parents=True, exist_ok=True)

    print("Parsing batch results to find valid labels...")
    valid_labels = {}
    with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            cid = data.get("custom_id")
            if "result" in data and "result" in data["result"] and "message" in data["result"]["result"]:
                text = data["result"]["result"]["message"]["content"][0]["text"].strip()
                if text not in ('EMPTY', 'NOISE', 'INVALID') and len(text) > 3:
                    valid_labels[cid] = text

    print(f"Found {len(valid_labels)} valid labels from Claude.")
    
    # We don't need 21k. Let's sample 5000 for fast training.
    # Actually, let's take 2000 for training and 500 for validation.
    # To do this, we need to find those specific IDs in batch_input.jsonl.
    
    sample_size = 2500
    if len(valid_labels) > sample_size:
        sampled_keys = set(random.sample(list(valid_labels.keys()), sample_size))
        valid_labels = {k: v for k, v in valid_labels.items() if k in sampled_keys}

    print(f"Extracting {len(valid_labels)} sampled crops from batch_input.jsonl...")
    
    rows = []
    with open(INPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            if '"custom_id":"crop_' not in line[:100]:
                data = json.loads(line)
            else:
                data = json.loads(line)
            
            cid = data.get("custom_id")
            if cid in valid_labels:
                b64 = data["params"]["messages"][0]["content"][0]["source"]["data"]
                img_data = base64.b64decode(b64)
                
                img_np = np.frombuffer(img_data, np.uint8)
                img_cv = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
                processed, _, _ = preprocess_for_easyocr(img_cv)
                if isinstance(processed, list):
                    processed = processed[0]
                _, buffer = cv2.imencode('.png', processed)
                img_data = buffer.tobytes()
                
                label = valid_labels[cid]
                # Filter out line breaks that Claude might have accidentally added
                label = label.replace("\n", " ").replace("\t", " ")
                rows.append((cid, img_data, label))
                
                if len(rows) >= len(valid_labels):
                    break

    print(f"Successfully extracted {len(rows)} images.")
    random.shuffle(rows)
    
    split_idx = int(len(rows) * 0.8)
    train_rows = rows[:split_idx]
    val_rows = rows[split_idx:]
    
    def write_split(split_name, split_rows, out_dir):
        gt_path = out_dir / "gt.txt"
        copied = 0
        with open(gt_path, "w", encoding="utf-8") as f:
            for idx, (cid, img_data, label) in enumerate(split_rows):
                img_name = f"image_{idx:05d}.png"
                img_path = out_dir / img_name
                
                with open(img_path, "wb") as img_f:
                    img_f.write(img_data)
                    
                f.write(f"{img_name}\t{label}\n")
                copied += 1
        print(f"Wrote {copied} items to {split_name} split.")

    write_split("train", train_rows, TRAIN_DIR)
    write_split("val", val_rows, VAL_DIR)
    print("Done formatting data for LMDB generation!")

if __name__ == "__main__":
    main()
