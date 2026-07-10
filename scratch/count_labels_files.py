from pathlib import Path

def main():
    labels_dir = Path("labels")
    if not labels_dir.exists():
        print("labels directory not found.")
        return
        
    pngs = list(labels_dir.glob("**/*.png"))
    print(f"Total PNG files on disk in 'labels/': {len(pngs)}")
    
if __name__ == "__main__":
    main()
