"""Prepare a zip bundle for Google Colab training.

Packs only the labeled crops (referenced in labels_clean.csv) + the CSV
into  colab_bundle.zip  in the project root.

Usage:
    python prepare_colab_bundle.py
    python prepare_colab_bundle.py --output my_bundle.zip
"""

import argparse
import csv
import zipfile
from pathlib import Path

LABELS_CSV = Path("labels/labels_clean.csv")
DEFAULT_OUTPUT = Path("colab_bundle.zip")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = []
    missing = 0
    with LABELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = Path(row["filepath"])
            if p.exists():
                rows.append((p, row))
            else:
                missing += 1

    print(f"Labels: {len(rows)} found, {missing} missing")

    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write the CSV (with forward-slash paths for cross-platform compat)
        csv_lines = ["filename,streamer,filepath,label,quality\n"]
        for p, row in rows:
            fixed = row["filepath"].replace("\\", "/")
            csv_lines.append(
                f'{row["filename"]},{row["streamer"]},{fixed},{row["label"]},{row["quality"]}\n'
            )
        zf.writestr("labels/labels_clean.csv", "".join(csv_lines))

        # Write each crop image
        for i, (p, _) in enumerate(rows):
            arcname = str(p).replace("\\", "/")
            zf.write(p, arcname)
            if (i + 1) % 2000 == 0:
                print(f"  Zipped {i + 1}/{len(rows)} images...")

    size_mb = args.output.stat().st_size / 1_048_576
    print(f"\nBundle written: {args.output} ({size_mb:.1f} MB)")
    print("Next: upload this file to Google Drive, then open train_trocr_colab.ipynb in Colab.")


if __name__ == "__main__":
    main()
