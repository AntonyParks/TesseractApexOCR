"""synth_wordlevel.py -- generate WORD-LEVEL synthetic training crops for the EasyOCR recognizer
fine-tune (bead gc3), matching prepare_wordlevel_dataset.py's format exactly.

Why word-level: inference runs reader.detect() (CRAFT) then recognizes each box; the word-level
retrain beat the line-level one 89.4% on eval_holdout (KNOWN_ISSUES). So we train on the same unit.

Pipeline per synthetic sample (mirrors prepare_wordlevel_dataset.py):
  render light-on-dark text -> augment (Twitch-transcode degradation) -> preprocess_for_easyocr
  (grayscale/invert/2x/pad) -> reader.detect() CRAFT boxes -> if box count == word count, crop each
  box and label it with the corresponding word. Crops are the exact pixels + polarity the recognizer
  sees at inference. PERFECT labels (no Gemini/relabel needed -> not blocked by dead credits).

Content is weighted toward the documented failure modes: 'SQUADS'/'LEFT', 1-20 counts (tens-digit
mangles), digit-confusion glyphs, clan [TAG]s, and realistic player-name shapes.

Output: wordlevel_crops_synth/word_synth_NNNNNN.png + labels/labels_wordlevel_synth.csv
Only in-charset tokens are emitted (apex.yaml 94-char set) -- respects the dataset.py truncation bug.
"""
import argparse, csv, random
from pathlib import Path
import numpy as np
import cv2

import ocr as ocr_mod
from synth_killfeed import render_raw, augment, FONTS

# apex.yaml character_list (must match; anything outside is silently truncated by dataset.py)
CHARSET = set("0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ °"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

OUT_DIR = Path("wordlevel_crops_synth")
OUT_CSV = Path("labels/labels_wordlevel_synth.csv")

LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = LOWER.upper()
DIGIT = "0123456789"

# Fixed HUD / event vocabulary + the exact failure targets (oversampled via the builders below).
HUD_WORDS = ["SQUADS", "LEFT", "[Bleed", "Out]", "EneMy", "EnemY", "Shield", "Broken",
             "AWAITING", "RESPAWN", "spotted", "revealed", "enemies", "on", "the", "map",
             "KNOCKED", "DOWN", "[Bleed", "Out]", "Assist", "Squad", "Wipe"]


def in_charset(s: str) -> bool:
    return all(c in CHARSET for c in s)


def rand_name() -> str:
    """Realistic Apex IGN: lowercase-heavy alnum, optional caps/underscore/apostrophe, trailing digits."""
    n = random.randint(3, 14)
    chars = []
    for i in range(n):
        r = random.random()
        if r < 0.62:
            chars.append(random.choice(LOWER))
        elif r < 0.78:
            chars.append(random.choice(DIGIT))
        elif r < 0.90:
            chars.append(random.choice(UPPER))
        elif r < 0.955:
            chars.append("_")
        else:
            chars.append(random.choice("'.-x"))
    # frequently append 1-4 trailing digits (very common in IGNs)
    if random.random() < 0.5:
        chars += list("".join(random.choice(DIGIT) for _ in range(random.randint(1, 4))))
    s = "".join(chars).strip("_.-")
    return s if s and in_charset(s) else rand_name()


def rand_clan() -> str:
    n = random.randint(2, 5)
    body = "".join(random.choice(LOWER + UPPER + DIGIT) for _ in range(n))
    return f"[{body}]"


def sample_text() -> str:
    """Return a space-delimited unit to render (its words become individual box labels).

    Heavily SINGLE-token: at inference CRAFT splits 'N SQUADS LEFT' into separate boxes anyway, so
    single tokens both MATCH inference and give ~100% box/word yield (1 word -> 1 box). A minority of
    tight multi-word units (clan+name) keeps robustness for the tight-group boxes CRAFT sometimes
    emits. Failure targets (SQUADS/LEFT, 1-20 counts) are oversampled as single tokens."""
    r = random.random()
    # --- single-token failure targets (~48%) ---
    if r < 0.16:
        return "SQUADS"
    if r < 0.22:
        return "LEFT"
    if r < 0.42:                        # counts incl. tens-digit mangle stress (11-20)
        return str(random.randint(1, 20) if random.random() < 0.6 else random.randint(1, 60))
    if r < 0.48:
        return random.choice(["Broken", "Shield", "EneMy", "EnemY", "RESPAWN", "AWAITING",
                              "spotted", "revealed", "enemies", "KNOCKED", "DOWN", "Assist", "Out]", "[Bleed"])
    # --- single-token names / clans (~37%): bulk glyph diversity ---
    if r < 0.80:
        return rand_name()
    if r < 0.85:
        return rand_clan()
    # --- tight multi-word units (~15%): clan+name, bleed marker ---
    if r < 0.96:
        return f"{rand_clan()} {rand_name()}"
    return "[Bleed Out]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4000, help="number of source samples to render")
    ap.add_argument("--min-box-width", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed); np.random.seed(args.seed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ocr_mod._easyocr_reader = None
    reader = ocr_mod._get_easyocr_reader()   # CRAFT detection is model-independent

    out_rows, idx = [], 0
    kept_lines = skipped_mismatch = skipped_no_boxes = skipped_charset = 0
    fonts = list(FONTS.values())

    for i in range(args.n):
        text = sample_text()
        words = text.split()
        if not all(in_charset(w) for w in words):
            skipped_charset += 1
            continue
        raw = augment(render_raw(text, random.choice(fonts), fontsize=random.randint(22, 32)))
        processed, _, _ = ocr_mod.preprocess_for_easyocr(cv2.cvtColor(raw, cv2.COLOR_BGR2BGRA))

        hlist, _ = reader.detect(processed)
        raw_boxes = hlist[0] if hlist else []
        boxes = [b for b in raw_boxes if (b[1] - b[0]) >= args.min_box_width]
        boxes.sort(key=lambda b: b[0])

        if not boxes:
            skipped_no_boxes += 1
            continue
        if len(boxes) != len(words):
            skipped_mismatch += 1
            continue

        h, w = processed.shape[:2]
        for box, word in zip(boxes, words):
            x0, x1, y0, y1 = box
            x0, x1 = max(0, x0), min(w, x1)
            y0, y1 = max(0, y0), min(h, y1)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = processed[y0:y1, x0:x1]
            fname = f"word_synth_{idx:06d}.png"
            cv2.imwrite(str(OUT_DIR / fname), crop)
            out_rows.append((fname, word))
            idx += 1
        kept_lines += 1

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["filename", "label"])
        wtr.writerows(out_rows)

    print(f"source samples={args.n}  kept_lines={kept_lines}  "
          f"skipped(mismatch={skipped_mismatch}, no_boxes={skipped_no_boxes}, charset={skipped_charset})")
    print(f"word-level synthetic crops: {len(out_rows)} -> {OUT_DIR}/  |  labels -> {OUT_CSV}")
    # tiny spot-check manifest
    for i in range(0, len(out_rows), max(1, len(out_rows) // 10)):
        print("  ", out_rows[i])


if __name__ == "__main__":
    main()
