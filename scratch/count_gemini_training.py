from pathlib import Path

def main():
    training_dir = Path("labels/gemini_training")
    corrections_dir = Path("labels/gemini_corrections")
    confirmed_dir = Path("labels/gemini_confirmed")
    
    for d, name in [(training_dir, "gemini_training"), (corrections_dir, "gemini_corrections"), (confirmed_dir, "gemini_confirmed")]:
        if d.exists():
            pngs = list(d.glob("**/*.png"))
            print(f"{name}: {len(pngs)} PNGs on disk")
        else:
            print(f"{name} directory does not exist yet.")

if __name__ == "__main__":
    main()
