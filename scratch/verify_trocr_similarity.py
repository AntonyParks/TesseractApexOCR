import csv
import random
from pathlib import Path
from difflib import SequenceMatcher
import numpy as np

import torch
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

LABELS_CSV = Path("labels/labels_clean.csv")
MODEL_PATH = Path("models/trocr_apex")

def similarity(a: str, b: str) -> float:
    # Normalize spaces and lower case
    a_norm = " ".join(a.lower().split())
    b_norm = " ".join(b.lower().split())
    # Standardize <GUN_ICON> representations
    a_norm = a_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    b_norm = b_norm.replace("<gun_icon>", "<gun_icon>").replace("gunicon", "<gun_icon>").replace("gun_icon", "<gun_icon>")
    return SequenceMatcher(None, a_norm, b_norm).ratio()

def load_model():
    processor = TrOCRProcessor.from_pretrained(str(MODEL_PATH))
    model = VisionEncoderDecoderModel.from_pretrained(str(MODEL_PATH))
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    return processor, model, device

def predict(processor, model, device, image_path: str) -> str:
    img = Image.open(image_path).convert("RGB")
    pixel_values = processor(images=img, return_tensors="pt").pixel_values.to(device)
    with torch.no_grad():
        generated_ids = model.generate(pixel_values, max_new_tokens=64)
    return processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()

def main():
    if not MODEL_PATH.exists() or not LABELS_CSV.exists():
        print("Required files not found.")
        return

    # Load rows for high-quality
    rows = []
    with LABELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["quality"] == "high":
                if Path(row["filepath"]).exists():
                    rows.append(row)

    if not rows:
        print("No high-quality rows found.")
        return

    # Sample 100 random crops
    rng = random.Random(42)
    sample = rng.sample(rows, min(100, len(rows)))

    print("Loading TrOCR model...")
    processor, model, device = load_model()
    print("Evaluating similarity on 100 crops...")

    scores = []
    above_80_count = 0

    for idx, row in enumerate(sample):
        label = row["label"].strip()
        pred = predict(processor, model, device, row["filepath"]).strip()
        
        sim = similarity(pred, label)
        scores.append(sim)
        if sim >= 0.80:
            above_80_count += 1
            
        if idx < 5:
            print(f"Example {idx+1}:")
            print(f"  GT:   '{label}'")
            print(f"  Pred: '{pred}'")
            print(f"  Sim:  {sim:.2%}")
            print()

    print("=" * 50)
    print("TrOCR SIMILARITY SUMMARY")
    print("=" * 50)
    print(f"Average Similarity: {np.mean(scores):.2%}")
    print(f"Crops matching >= 80% similarity: {above_80_count} / {len(scores)} ({above_80_count / len(scores):.2%})")
    print("=" * 50)

if __name__ == "__main__":
    main()
