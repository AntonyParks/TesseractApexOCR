from pathlib import Path
import time

def main():
    crops_dir = Path("crops")
    if not crops_dir.exists():
        print("Crops folder not found.")
        return
        
    pngs = list(crops_dir.glob("**/*.png"))
    if not pngs:
        print("No crops found yet.")
        return
        
    # Sort by modification time
    pngs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    
    print(f"Total crops: {len(pngs)}")
    print("Top 10 newest crops:")
    for p in pngs[:10]:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
        print(f" - {p.relative_to(crops_dir)} ({mtime})")

if __name__ == "__main__":
    main()
