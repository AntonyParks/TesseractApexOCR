"""Fast OCR-fix validation loop: re-run the read pipeline on the SAVED color crops (no VOD
re-decode) so an OCR/skull/marker change can be re-scored in minutes. Overwrites vod_capture/
reads.jsonl (original backed up to reads.jsonl.orig once). Then run _vod_parse.py + _score_ocr.py.
"""
import os, glob, json, re, shutil
import cv2
from ocr import preprocess_for_easyocr, ocr_with_easyocr, is_empty_line, looks_like_noise

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CROPS = os.path.join(SP, "vod_capture", "crops")
READS = os.path.join(SP, "vod_capture", "reads.jsonl")
SX, SY = 1.0, 936 / 1080.0

if not os.path.exists(READS + ".orig"):
    shutil.copy(READS, READS + ".orig")

crops = sorted(glob.glob(os.path.join(CROPS, "*.png")),
               key=lambda p: float(re.match(r"(\d+\.\d+)_r(\d+)", os.path.basename(p)).group(1)))
out = open(READS, "w", encoding="utf-8")
n = 0
for p in crops:
    m = re.match(r"(\d+\.\d+)_r(\d+)", os.path.basename(p))
    t, ri = float(m.group(1)), int(m.group(2))
    color = cv2.imread(p)
    if color is None:
        continue
    try:
        processed = preprocess_for_easyocr(color, stretch_x=SX, stretch_y=SY)[0]
    except Exception:
        continue
    saving = processed[0] if isinstance(processed, list) else processed
    if is_empty_line(saving):
        continue
    text = ocr_with_easyocr(saving, color_img=color)
    if looks_like_noise(text):
        continue
    out.write(json.dumps({"t": round(t, 2), "ri": ri, "crop": os.path.basename(p), "text": text}) + "\n")
    n += 1
    if n % 200 == 0:
        print(f"  {n} crops re-OCR'd", flush=True)
out.close()
print(f"DONE {n} reads -> {READS}", flush=True)
