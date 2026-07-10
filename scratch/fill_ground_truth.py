"""fill_ground_truth.py — Label any test_crops/raw crops missing from ground_truth.json via Gemini.

Extends the existing offline ground_truth.json in place (does not overwrite existing labels).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmark_easyocr_filters import get_gemini_ground_truth, RAW_DIR, GT_FILE  # noqa: E402


def main():
    gt = json.loads(GT_FILE.read_text(encoding="utf-8")) if GT_FILE.exists() else {}
    crops = sorted(RAW_DIR.glob("*.png"))
    missing = [c for c in crops if c.name not in gt]
    print(f"{len(crops)} total crops, {len(missing)} missing labels")

    for i, crop in enumerate(missing, 1):
        label = get_gemini_ground_truth(crop)
        if label is None:
            print(f"[{i}/{len(missing)}] {crop.name}: FAILED, skipping")
            continue
        gt[crop.name] = label
        print(f"[{i}/{len(missing)}] {crop.name}: {label!r}")
        GT_FILE.write_text(json.dumps(gt, indent=2, ensure_ascii=False), encoding="utf-8")
        time.sleep(4.5)

    print(f"\nDone. {len(gt)}/{len(crops)} crops now labeled.")


if __name__ == "__main__":
    main()
