"""build_wordlevel_lmdb.py -- assemble train/val LMDB for the EasyOCR recognizer fine-tune (bead gc3).

Combines the SYNTHETIC word crops (labels/labels_wordlevel_synth.csv, perfect labels) with the
existing REAL clean word crops (labels/labels_wordlevel.csv, Gemini-verified) into a training set,
and holds out a slice of the REAL crops as validation so val accuracy reflects the real distribution
(the final real metric is still eval_holdout.csv via the pipeline's parsed-correct check).

Writes:
    easyocr_dataset_synth/train_gt.txt , val_gt.txt   (tab-separated: relpath<TAB>label)
    easyocr_dataset_synth/train_lmdb  , val_lmdb

Mirrors models/deep-text-recognition-benchmark/create_lmdb_dataset.py, but with a generous map_size
(the hardcoded 100MB there is too small once synthetic scales up).
"""
import argparse, csv, random
from pathlib import Path
import numpy as np
import cv2
import lmdb

ROOT = Path(r"g:\PycharmProjects\TesseractApexOCR")
SYNTH_CSV = ROOT / "labels/labels_wordlevel_synth.csv"
SYNTH_DIR = "wordlevel_crops_synth"
REAL_CSV = ROOT / "labels/labels_wordlevel.csv"
REAL_DIR = "wordlevel_crops"
OUT = ROOT / "easyocr_dataset_synth"
VAL_FRACTION_REAL = 0.10
MAP_SIZE = 4 * 1024 ** 3   # 4 GB ceiling (LMDB is sparse; not preallocated on most FS)

# Drift-resistant balance (advisor: 96% Windows-font synthetic risks drifting apex.pth away from the
# real Apex font). Cap synthetic and oversample the real (Apex-font, Gemini-verified) crops so REAL
# carries real gradient weight and anchors the font. Oversampling duplicates the SAME real crops
# (no new font diversity) -- the point is loss WEIGHT, not diversity; eval_holdout (unseen) is the
# overfit guard. Defaults ~= 55% synth / 45% real.
DEFAULT_SYNTH_CAP = 8000
DEFAULT_REAL_OVERSAMPLE = 6

CHARSET = set("0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ °"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def load(csv_path, subdir):
    rows = []
    if not csv_path.exists():
        return rows
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fn, lab = r["filename"], r["label"]
            if not lab or "\t" in lab or "\n" in lab:
                continue
            if not all(c in CHARSET for c in lab):   # respect dataset.py out-of-charset truncation
                continue
            p = f"{subdir}/{fn}"
            if (ROOT / p).exists():
                rows.append((p, lab))
    return rows


def write_lmdb(pairs, out_path):
    out_path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(out_path), map_size=MAP_SIZE)
    cache, cnt = {}, 1
    for relpath, label in pairs:
        with open(ROOT / relpath, "rb") as f:
            img_bin = f.read()
        arr = cv2.imdecode(np.frombuffer(img_bin, np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None or arr.shape[0] * arr.shape[1] == 0:
            continue
        cache[f"image-{cnt:09d}".encode()] = img_bin
        cache[f"label-{cnt:09d}".encode()] = label.encode()
        if cnt % 2000 == 0:
            with env.begin(write=True) as txn:
                for k, v in cache.items():
                    txn.put(k, v)
            cache = {}
            print(f"  written {cnt}/{len(pairs)}")
        cnt += 1
    cache["num-samples".encode()] = str(cnt - 1).encode()
    with env.begin(write=True) as txn:
        for k, v in cache.items():
            txn.put(k, v)
    env.close()
    return cnt - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth-cap", type=int, default=DEFAULT_SYNTH_CAP,
                    help="max synthetic crops in train (0 = all)")
    ap.add_argument("--real-oversample", type=int, default=DEFAULT_REAL_OVERSAMPLE,
                    help="duplication factor for real train crops (font-anchor weight)")
    args = ap.parse_args()

    random.seed(0)
    synth = load(SYNTH_CSV, SYNTH_DIR)
    real = load(REAL_CSV, REAL_DIR)
    random.shuffle(synth)
    random.shuffle(real)

    n_val = int(len(real) * VAL_FRACTION_REAL)
    val = real[:n_val]
    real_train = real[n_val:]

    if args.synth_cap and len(synth) > args.synth_cap:
        synth = synth[:args.synth_cap]
    real_train_os = real_train * args.real_oversample     # duplicate for gradient weight
    train = synth + real_train_os
    random.shuffle(train)
    print(f"balance: synth={len(synth)} + real={len(real_train)}x{args.real_oversample}"
          f"={len(real_train_os)}  -> {100*len(real_train_os)//max(1,len(train))}% real")

    OUT.mkdir(parents=True, exist_ok=True)
    for name, pairs in [("train", train), ("val", val)]:
        with open(OUT / f"{name}_gt.txt", "w", encoding="utf-8", newline="\n") as f:
            for relpath, label in pairs:
                f.write(f"{relpath}\t{label}\n")

    print(f"synthetic={len(synth)}  real={len(real)}  -> train={len(train)}  val={len(val)}")
    print("writing train_lmdb ..."); nt = write_lmdb(train, OUT / "train_lmdb")
    print("writing val_lmdb ...");   nv = write_lmdb(val, OUT / "val_lmdb")
    print(f"DONE: train_lmdb={nt} samples, val_lmdb={nv} samples -> {OUT}")


if __name__ == "__main__":
    main()
