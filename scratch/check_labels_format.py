import csv
import cv2
from pathlib import Path

def main():
    csv_path = Path("labels/labels_clean.csv")
    if not csv_path.exists():
        print("CSV not found.")
        return
        
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    print(f"Total rows in CSV: {len(rows)}")
    
    # Check first 5 existing files
    checked = 0
    for r in rows:
        filepath = Path(r["filepath"])
        if filepath.exists():
            img = cv2.imread(str(filepath), cv2.IMREAD_UNCHANGED)
            if img is not None:
                print(f" - {filepath.name}: channels={img.shape[2] if len(img.shape) > 2 else 1}, shape={img.shape}")
                checked += 1
                if checked >= 5:
                    break
    if checked == 0:
        print("No files found under filepath references in CSV.")

if __name__ == "__main__":
    main()
