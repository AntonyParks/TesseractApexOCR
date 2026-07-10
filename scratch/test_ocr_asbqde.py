import cv2
import pandas as pd
from pathlib import Path
from ocr import parse_crop_image # Check if this function exists or see what parser is used in ocr.py/parsers.py

# Let's inspect labels_clean.csv first if it exists
labels_csv = Path("labels_clean.csv")
if labels_csv.exists():
    df = pd.read_csv(labels_csv)
    # Search for this filename
    match = df[df['filename'].str.contains('20260612_130445_line1_384a', na=False)]
    if not match.empty:
        print("Found matching label in labels_clean.csv:")
        print(match.to_dict(orient='records'))
    else:
        print("No match found in labels_clean.csv.")
else:
    print("labels_clean.csv not found.")
