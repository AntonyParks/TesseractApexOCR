"""Build an LMDB dataset from labels/labels_clean.csv for EasyOCR_Apex_Finetune.ipynb.

Converts the flat (filename, streamer, filepath, label, quality) CSV into the
train/val split + gt.txt format clovaai/deep-text-recognition-benchmark expects,
then runs its own create_lmdb_dataset.py to build train_lmdb/ and val_lmdb/, and
zips both into easyocr_lmdb_dataset_ready.zip -- the exact file
EasyOCR_Apex_Finetune.ipynb's cell 2 asks you to upload.

Usage:
    python prepare_easyocr_lmdb.py
    python prepare_easyocr_lmdb.py --wordlevel   # build from labels_wordlevel.csv / wordlevel_crops/ instead
"""

import argparse
import csv
import random
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

LABELS_CSV = Path("labels/labels_clean.csv")
WORDLEVEL_CSV = Path("labels/labels_wordlevel.csv")
WORDLEVEL_CROPS_DIR = Path("wordlevel_crops")
STAGING_DIR = Path("easyocr_dataset_staging")
TRAIN_DIR = STAGING_DIR / "train"
VAL_DIR = STAGING_DIR / "val"
LMDB_OUT_DIR = Path("easyocr_lmdb_dataset_ready")
OUTPUT_ZIP = Path("easyocr_lmdb_dataset_ready.zip")
WORDLEVEL_LMDB_OUT_DIR = Path("easyocr_lmdb_wordlevel_ready")
WORDLEVEL_OUTPUT_ZIP = Path("easyocr_lmdb_wordlevel_ready.zip")
CREATE_LMDB_SCRIPT = Path("models/deep-text-recognition-benchmark/create_lmdb_dataset.py")

VAL_FRACTION = 0.2
SEED = 20260702


def _load_rows() -> list[tuple[Path, str]]:
    """Return [(image_path, label), ...] for every row whose image still exists on disk."""
    rows = []
    missing = 0
    with LABELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = Path(row["filepath"])
            if not p.exists():
                missing += 1
                continue
            label = row["label"].replace("\n", " ").replace("\t", " ").strip()
            rows.append((p, label))
    print(f"Labels: {len(rows)} found, {missing} missing on disk (skipped).")
    return rows


def _load_wordlevel_rows() -> list[tuple[Path, str]]:
    """Return [(image_path, label), ...] from labels_wordlevel.csv / wordlevel_crops/."""
    rows = []
    missing = 0
    with WORDLEVEL_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = WORDLEVEL_CROPS_DIR / row["filename"]
            if not p.exists():
                missing += 1
                continue
            rows.append((p, row["label"]))
    print(f"Word-level labels: {len(rows)} found, {missing} missing on disk (skipped).")
    return rows


def _write_split(name: str, split_rows: list[tuple[Path, str]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    gt_path = out_dir / "gt.txt"
    with gt_path.open("w", encoding="utf-8") as gt:
        for i, (src, label) in enumerate(split_rows):
            img_name = f"image_{i:05d}.png"
            shutil.copy(src, out_dir / img_name)
            gt.write(f"{img_name}\t{label}\n")
    print(f"  {name}: {len(split_rows)} images -> {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wordlevel", action="store_true",
                     help="Build from labels_wordlevel.csv / wordlevel_crops/ instead of labels_clean.csv")
    args = ap.parse_args()

    lmdb_out_dir = WORDLEVEL_LMDB_OUT_DIR if args.wordlevel else LMDB_OUT_DIR
    output_zip = WORDLEVEL_OUTPUT_ZIP if args.wordlevel else OUTPUT_ZIP

    if args.wordlevel:
        if not WORDLEVEL_CSV.exists():
            print(f"Missing {WORDLEVEL_CSV}. Run prepare_wordlevel_dataset.py first.")
            sys.exit(1)
    elif not LABELS_CSV.exists():
        print(f"Missing {LABELS_CSV}. Run label_crops.py first.")
        sys.exit(1)
    if not CREATE_LMDB_SCRIPT.exists():
        print(f"Missing {CREATE_LMDB_SCRIPT} (vendored deep-text-recognition-benchmark).")
        sys.exit(1)

    rows = _load_wordlevel_rows() if args.wordlevel else _load_rows()
    if not rows:
        print("No labeled rows with existing images. Nothing to build.")
        sys.exit(1)

    random.seed(SEED)
    random.shuffle(rows)
    split_idx = int(len(rows) * (1 - VAL_FRACTION))
    train_rows, val_rows = rows[:split_idx], rows[split_idx:]

    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    if lmdb_out_dir.exists():
        shutil.rmtree(lmdb_out_dir)

    print("Staging train/val image folders + gt.txt...")
    _write_split("train", train_rows, TRAIN_DIR)
    _write_split("val", val_rows, VAL_DIR)

    print("\nBuilding LMDB (train)...")
    subprocess.run(
        [sys.executable, str(CREATE_LMDB_SCRIPT.resolve()),
         "--inputPath", str(TRAIN_DIR.resolve()),
         "--gtFile", str((TRAIN_DIR / "gt.txt").resolve()),
         "--outputPath", str((lmdb_out_dir / "train_lmdb").resolve())],
        check=True,
    )

    print("\nBuilding LMDB (val)...")
    subprocess.run(
        [sys.executable, str(CREATE_LMDB_SCRIPT.resolve()),
         "--inputPath", str(VAL_DIR.resolve()),
         "--gtFile", str((VAL_DIR / "gt.txt").resolve()),
         "--outputPath", str((lmdb_out_dir / "val_lmdb").resolve())],
        check=True,
    )

    print(f"\nZipping -> {output_zip} ...")
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in lmdb_out_dir.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(lmdb_out_dir))

    size_mb = output_zip.stat().st_size / 1_048_576
    print(f"\nDone: {output_zip} ({size_mb:.1f} MB) -- {len(train_rows)} train / {len(val_rows)} val samples.")
    print("Upload this file to the Colab notebook when prompted in cell 2.")

    shutil.rmtree(STAGING_DIR)


if __name__ == "__main__":
    main()
