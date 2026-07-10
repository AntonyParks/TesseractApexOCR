import cv2
from pathlib import Path

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    out_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\24423f36-1859-44be-91f7-1e8f09ba7cf4")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Save slices
    cv2.imwrite(str(out_dir / "sang_slice_y16.png"), img[16:16+42, :])
    cv2.imwrite(str(out_dir / "sang_slice_y40.png"), img[40:40+42, :])
    cv2.imwrite(str(out_dir / "sang_slice_y72.png"), img[72:72+42, :])
    cv2.imwrite(str(out_dir / "sang_slice_y88.png"), img[88:88+42, :])
    cv2.imwrite(str(out_dir / "sang_slice_y96.png"), img[96:96+42, :])
    
    print("Saved slices y16, y40, y72, y88, y96.")

if __name__ == "__main__":
    main()
