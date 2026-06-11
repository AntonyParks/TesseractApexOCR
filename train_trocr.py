"""Fine-tune microsoft/trocr-small-printed on labeled Apex killfeed crops.

Usage:
    python train_trocr.py
    python train_trocr.py --epochs 15 --batch_size 8 --lr 5e-5 --quality high,medium

Output: models/trocr_apex/  (model + processor saved with save_pretrained)
"""

import argparse
import csv
import random
import shutil
import tempfile
from pathlib import Path

import torch
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from torch.optim import AdamW
from transformers import (
    TrOCRProcessor,
    VisionEncoderDecoderModel,
    get_linear_schedule_with_warmup,
)
LABELS_CSV = Path("labels/labels_clean.csv")
MODEL_OUTPUT = Path("models/trocr_apex")
BASE_MODEL = "microsoft/trocr-small-printed"
DEFAULT_BASE_MODEL = BASE_MODEL


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class KillfeedDataset(Dataset):
    def __init__(self, rows: list[dict], processor: TrOCRProcessor):
        self.rows = rows
        self.processor = processor

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        row = self.rows[idx]
        img = Image.open(row["filepath"]).convert("RGB")
        label = row["label"]

        pixel_values = self.processor(images=img, return_tensors="pt").pixel_values.squeeze(0)
        labels = self.processor.tokenizer(
            label,
            padding="max_length",
            max_length=64,
            truncation=True,
            return_tensors="pt",
        ).input_ids.squeeze(0)

        # Replace padding token id with -100 so loss ignores it
        labels[labels == self.processor.tokenizer.pad_token_id] = -100

        return {"pixel_values": pixel_values, "labels": labels}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_rows(quality_filter: set[str]) -> list[dict]:
    rows = []
    with LABELS_CSV.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["quality"] in quality_filter and Path(row["filepath"]).exists():
                rows.append(row)
    return rows


def stratified_split(rows: list[dict], val_ratio: float = 0.1, seed: int = 42) -> tuple:
    """Stratified 90/10 split by quality tier."""
    rng = random.Random(seed)
    by_tier: dict[str, list] = {}
    for row in rows:
        by_tier.setdefault(row["quality"], []).append(row)

    train, val = [], []
    for tier_rows in by_tier.values():
        rng.shuffle(tier_rows)
        n_val = max(1, int(len(tier_rows) * val_ratio))
        val.extend(tier_rows[:n_val])
        train.extend(tier_rows[n_val:])

    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def compute_cer(predictions: list[str], references: list[str], _unused=None) -> float:
    """Character Error Rate computed locally — no network call required."""
    total_edits = 0
    total_chars = 0
    for pred, ref in zip(predictions, references):
        # Levenshtein distance at character level
        n, m = len(ref), len(pred)
        dp = list(range(m + 1))
        for i in range(1, n + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, m + 1):
                prev, dp[j] = dp[j], prev if ref[i-1] == pred[j-1] else 1 + min(prev, dp[j], dp[j-1])
        total_edits += dp[m]
        total_chars += max(n, 1)
    return total_edits / total_chars if total_chars else 0.0


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train(args):
    quality_filter = set(q.strip() for q in args.quality.split(","))
    print(f"Loading labels from {LABELS_CSV} (quality: {quality_filter})...")

    rows = load_rows(quality_filter)
    if not rows:
        print("No labeled data found. Run label_crops.py first.")
        return

    train_rows, val_rows = stratified_split(rows, val_ratio=0.1)
    print(f"Dataset: {len(rows)} total | {len(train_rows)} train | {len(val_rows)} val")

    base = args.base_model
    print(f"Loading processor and model from {base}...")
    processor = TrOCRProcessor.from_pretrained(base)
    model = VisionEncoderDecoderModel.from_pretrained(base)

    # Configure decoder
    model.config.decoder_start_token_id = processor.tokenizer.cls_token_id
    model.config.pad_token_id = processor.tokenizer.pad_token_id
    model.config.vocab_size = model.config.decoder.vocab_size

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")
    model.to(device)

    train_dataset = KillfeedDataset(train_rows, processor)
    val_dataset = KillfeedDataset(val_rows, processor)

    train_loader = DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0
    )
    val_loader = DataLoader(
        val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=0
    )

    optimizer = AdamW(model.parameters(), lr=args.lr)
    total_steps = len(train_loader) * args.epochs
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=total_steps // 10, num_training_steps=total_steps
    )

    best_cer = float("inf")
    n_train_batches = len(train_loader)
    log_every = max(1, n_train_batches // 20)  # print ~20 updates per epoch

    for epoch in range(1, args.epochs + 1):
        # --- Train ---
        model.train()
        train_loss = 0.0
        for batch_idx, batch in enumerate(train_loader, 1):
            pixel_values = batch["pixel_values"].to(device)
            labels = batch["labels"].to(device)

            outputs = model(pixel_values=pixel_values, labels=labels)
            loss = outputs.loss
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()

            train_loss += loss.item()

            if batch_idx % log_every == 0 or batch_idx == n_train_batches:
                print(f"  Epoch {epoch}/{args.epochs} | batch {batch_idx}/{n_train_batches} "
                      f"| loss {train_loss / batch_idx:.4f}", flush=True)

        train_loss /= n_train_batches

        # --- Validate ---
        model.eval()
        all_preds, all_refs = [], []
        with torch.no_grad():
            for batch in val_loader:
                pixel_values = batch["pixel_values"].to(device)
                labels = batch["labels"].to(device)

                generated = model.generate(pixel_values, max_new_tokens=64)
                preds = processor.batch_decode(generated, skip_special_tokens=True)
                # Decode labels (replace -100 with pad_token_id)
                label_ids = labels.clone()
                label_ids[label_ids == -100] = processor.tokenizer.pad_token_id
                refs = processor.batch_decode(label_ids, skip_special_tokens=True)

                all_preds.extend(preds)
                all_refs.extend(refs)

        cer = compute_cer(all_preds, all_refs)
        print(f"Epoch {epoch:02d}/{args.epochs} | loss={train_loss:.4f} | CER={cer:.4f}")

        if cer < best_cer:
            best_cer = cer
            # Save to a temp dir first, then atomically replace MODEL_OUTPUT.
            # This avoids Windows os error 1224 (memory-mapped file still open).
            MODEL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir = Path(tempfile.mkdtemp(dir=MODEL_OUTPUT.parent, prefix=".trocr_tmp_"))
            try:
                model.save_pretrained(tmp_dir)
                processor.save_pretrained(tmp_dir)
                if MODEL_OUTPUT.exists():
                    shutil.rmtree(MODEL_OUTPUT)
                tmp_dir.rename(MODEL_OUTPUT)
            except Exception:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
            print(f"  [BEST] New best CER={best_cer:.4f} - model saved to {MODEL_OUTPUT}")

    print(f"\nTraining complete. Best CER: {best_cer:.4f}")
    if best_cer < 0.10:
        print("[OK] Target CER < 0.10 achieved.")
    else:
        print("[WARN] CER >= 0.10 - consider more data or more epochs.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fine-tune TrOCR on Apex killfeed crops")
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--batch_size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument(
        "--quality", type=str, default="high,medium,noise",
        help="Comma-separated quality tiers to include (high, medium, low, noise)"
    )
    parser.add_argument(
        "--base_model", type=str, default=DEFAULT_BASE_MODEL,
        help="Base model or checkpoint path to fine-tune from (default: microsoft/trocr-small-printed)"
    )
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
