"""Spot-check the fine-tuned TrOCR model against Claude-labeled crops.

Usage:
    python test_trocr.py                    # 10 random high-quality crops
    python test_trocr.py --n 20             # 20 crops
    python test_trocr.py --quality high     # high-quality only
    python test_trocr.py --seed 0           # reproducible sample
"""

import argparse
import csv
import random
from pathlib import Path

import torch
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

LABELS_CSV = Path("labels/labels_clean.csv")
MODEL_PATH = Path("models/trocr_apex")


def load_model():
    print(f"Loading model from {MODEL_PATH}...")
    processor = TrOCRProcessor.from_pretrained(str(MODEL_PATH))
    model = VisionEncoderDecoderModel.from_pretrained(str(MODEL_PATH))
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    print(f"Model loaded on {device}.\n")
    return processor, model, device


def predict(processor, model, device, image_path: str) -> str:
    img = Image.open(image_path).convert("RGB")
    pixel_values = processor(images=img, return_tensors="pt").pixel_values.to(device)
    with torch.no_grad():
        generated_ids = model.generate(pixel_values, max_new_tokens=64)
    return processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()


def main():
    parser = argparse.ArgumentParser(description="Spot-check TrOCR model against Claude labels")
    parser.add_argument("--n", type=int, default=10, help="Number of crops to test")
    parser.add_argument("--quality", type=str, default="high", help="Quality tier filter (high/medium/low/all)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    if not MODEL_PATH.exists():
        print(f"ERROR: Model not found at {MODEL_PATH}. Run train_trocr.py first.")
        return

    if not LABELS_CSV.exists():
        print(f"ERROR: Labels CSV not found at {LABELS_CSV}. Run label_crops.py first.")
        return

    # Load rows
    rows = []
    with LABELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if args.quality == "all" or row["quality"] == args.quality:
                if Path(row["filepath"]).exists():
                    rows.append(row)

    if not rows:
        print(f"No rows found for quality={args.quality!r}.")
        return

    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))

    processor, model, device = load_model()

    # Stats
    exact_matches = 0
    gun_icon_in_label = 0
    gun_icon_in_pred = 0
    gun_icon_both = 0

    print(f"{'#':<4} {'MATCH':<6} {'LABEL':<45} {'PREDICTION'}")
    print("-" * 110)

    for i, row in enumerate(sample, 1):
        label = row["label"]
        pred = predict(processor, model, device, row["filepath"])

        match = label.strip() == pred.strip()
        if match:
            exact_matches += 1

        has_gun_label = "<GUN_ICON>" in label
        has_gun_pred = "<GUN_ICON>" in pred
        if has_gun_label:
            gun_icon_in_label += 1
        if has_gun_pred:
            gun_icon_in_pred += 1
        if has_gun_label and has_gun_pred:
            gun_icon_both += 1

        match_str = "OK" if match else "DIFF"
        label_disp = label[:43] + ".." if len(label) > 45 else label
        pred_disp = pred[:60] + ".." if len(pred) > 62 else pred
        print(f"{i:<4} {match_str:<6} {label_disp:<45} {pred_disp}")

    n = len(sample)
    print("-" * 110)
    print(f"\nResults ({n} crops, quality={args.quality!r}):")
    print(f"  Exact match:       {exact_matches}/{n} ({exact_matches/n*100:.1f}%)")
    print(f"  <GUN_ICON> in label:  {gun_icon_in_label}/{n}")
    print(f"  <GUN_ICON> in pred:   {gun_icon_in_pred}/{n}")
    if gun_icon_in_label:
        print(f"  Gun icon recall:   {gun_icon_both}/{gun_icon_in_label} ({gun_icon_both/gun_icon_in_label*100:.1f}%)")


if __name__ == "__main__":
    main()
