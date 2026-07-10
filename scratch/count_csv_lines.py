from pathlib import Path

def main():
    csv_path = Path("labels/labels_clean.csv")
    if not csv_path.exists():
        print("CSV not found.")
        return
        
    with csv_path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    print(f"Total lines in labels_clean.csv: {len(lines)}")

if __name__ == "__main__":
    main()
