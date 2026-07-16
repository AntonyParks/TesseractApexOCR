"""Prototype synthetic killfeed-crop generator for EasyOCR recognizer fine-tuning (bead gc3).

Renders Apex-style killfeed / HUD text (light-on-dark, like the real strip), applies
stream-compression-style augmentation, and runs the SAME preprocess_for_easyocr the live
pipeline uses -- so synthetic crops enter training identically to real ones. PERFECT labels.

This is the visual-validation prototype: it renders a few fixed strings in several candidate
fonts and stacks them next to a real processed crop so we can confirm the match before scaling
up to a full labelled dataset.
"""
import os, sys, random
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, r"g:\PycharmProjects\TesseractApexOCR")
from ocr import preprocess_for_easyocr

# The REAL Apex HUD fonts (TT Squares / TT Lakes Condensed, TypeType) extracted from the game
# (ruphin/apex-fonts) — the killfeed matched the BOLD condensed variants on direct pixel comparison.
# The v1 Windows fonts (Arial/Segoe) were the wrong glyph shapes and the model didn't transfer past
# the parsed gate (89.4%); this swap is the fix. Regular kept for weight diversity, bold weighted 2x.
_AF = r"g:\PycharmProjects\TesseractApexOCR\fonts\apex"
FONTS = {
    "ttlakes_bold":    _AF + r"\TTLakesCondensedBold.otf",
    "ttsquares_bold":  _AF + r"\TTSquaresCondensedBold.otf",
    "ttsquares_bold2": _AF + r"\TTSquaresCondensedBold.otf",   # 2x weight toward the matched bolds
    "ttsquares_reg":   _AF + r"\TTSquaresCondensedRegular.otf",
}

def render_raw(text, font_path, fontsize=26, pad=8):
    """Render light text on a dark translucent-strip-like background (BGR uint8)."""
    font = ImageFont.truetype(font_path, fontsize)
    # measure
    tmp = Image.new("L", (10, 10))
    d = ImageDraw.Draw(tmp)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    W, H = tw + 2 * pad, th + 2 * pad
    # dark bluish background with slight vertical gradient + noise, like the killfeed strip
    bg = np.zeros((H, W, 3), np.uint8)
    base = random.randint(18, 42)
    for y in range(H):
        bg[y, :] = (base + int(6 * y / H), base + int(4 * y / H), base + 2)
    bg += np.random.randint(0, 10, bg.shape, dtype=np.uint8)
    img = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    # near-white text, very slight off-white to mimic HUD
    fill = random.choice([(240, 240, 240), (255, 255, 255), (230, 234, 238)])
    d.text((pad - l, pad - t), text, font=font, fill=fill)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

def augment(bgr):
    """Match the REAL crop degradation: low-res source (Twitch transcode), soft focus, low
    contrast, JPEG mush. Real killfeed crops are small + blurry + grey-on-grey, NOT crisp."""
    out = bgr
    # 1) low-res source: shrink to a small height then back up (the strip is ~15-22px tall live)
    h, w = out.shape[:2]
    small_h = random.randint(14, 20)
    out = cv2.resize(out, (max(1, int(w * small_h / h)), small_h), interpolation=cv2.INTER_AREA)
    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    # 2) soft focus
    if random.random() < 0.85:
        out = cv2.GaussianBlur(out, (3, 3), random.uniform(0.5, 1.2))
    # 3) contrast compression toward grey + brightness jitter (text is grey, not black)
    a = random.uniform(0.55, 0.85); b = random.uniform(-8, 20)
    out = np.clip(a * out.astype(np.float32) + b, 0, 255).astype(np.uint8)
    # 4) sensor/transcode noise
    if random.random() < 0.6:
        out = np.clip(out.astype(np.int16) + np.random.randint(-6, 7, out.shape), 0, 255).astype(np.uint8)
    # 5) JPEG artifacts
    q = random.randint(22, 55)
    _, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out

SAMPLES = [
    "daniel949d [Bleed Out] WetEs'haal",
    "15 SQUADS LEFT",
    "8 SQUADS LEFT",
    "pentaxnvk [Bleed Out] supravb42o",
]

if __name__ == "__main__":
    out = r"C:\Users\anton\AppData\Local\Temp\claude\g--PycharmProjects-TesseractApexOCR\22c2f3c4-1b27-4bb2-a4c2-5c77691fa78e\scratchpad"
    random.seed(1); np.random.seed(1)
    rows = []
    for text in SAMPLES:
        for fname, fpath in FONTS.items():
            raw = render_raw(text, fpath)
            raw = augment(raw)
            proc, _, _ = preprocess_for_easyocr(cv2.cvtColor(raw, cv2.COLOR_BGR2BGRA))
            # normalize height for the comparison strip
            h = 60
            scale = h / proc.shape[0]
            proc = cv2.resize(proc, (int(proc.shape[1] * scale), h))
            label = np.full((h, 130), 255, np.uint8)
            cv2.putText(label, fname, (4, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
            cv2.putText(label, text[:14], (4, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.4, 0, 1)
            rows.append(np.hstack([label, proc]) if False else (fname, text, proc, label))
    # build a stacked comparison image (pad to common width)
    maxw = max(r[2].shape[1] + r[3].shape[1] for r in rows)
    strips = []
    for fname, text, proc, label in rows:
        strip = np.hstack([label, proc])
        if strip.shape[1] < maxw:
            strip = np.hstack([strip, np.full((strip.shape[0], maxw - strip.shape[1]), 255, np.uint8)])
        strips.append(strip)
        strips.append(np.full((3, maxw), 200, np.uint8))
    grid = np.vstack(strips)
    cv2.imwrite(os.path.join(out, "synth_grid.png"), grid)
    print("wrote synth_grid.png", grid.shape)
