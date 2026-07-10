import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import shutil
from trocr_inference import ocr_with_trocr

def main():
    crops_dir = Path("crops")
    dest_dir = Path(r"C:\Users\anton\.gemini\antigravity-ide\brain\a52ac73c-e640-491c-b4ec-ba79ac991851")
    model_path = Path("models/trocr_apex")
    
    if not crops_dir.exists():
        print("Crops directory not found.")
        return
        
    # Get all PNG crops
    all_crops = []
    for p in crops_dir.glob("**/*.png"):
        all_crops.append(p)
        
    print(f"Found {len(all_crops)} crops total.")
    
    # Let's take a sample (e.g. 8 different images from a few different folders)
    # We want a mix of streamers if possible
    by_streamer = {}
    for p in all_crops:
        streamer = p.parent.name
        if streamer not in by_streamer:
            by_streamer[streamer] = []
        by_streamer[streamer].append(p)
        
    sample_crops = []
    # Take up to 2 crops per streamer to get variety
    for streamer, paths in by_streamer.items():
        sample_crops.extend(paths[:2])
        if len(sample_crops) >= 10:
            break
            
    # If we still have less than 10, fill up
    if len(sample_crops) < 10 and all_crops:
        for p in all_crops:
            if p not in sample_crops:
                sample_crops.append(p)
            if len(sample_crops) >= 10:
                break
                
    sample_crops = sample_crops[:10]
    print(f"Selected {len(sample_crops)} crops for sample.")
    
    markdown_lines = [
        "# TrOCR Sample Output Audit",
        "",
        "This document lists a sample of 10 crop images with their raw image representation and the OCR result produced by the fine-tuned TrOCR model.",
        "",
        "| No. | Streamer | Filename | Crop Image | TrOCR Predicted Text | Confidence |",
        "| :--- | :--- | :--- | :---: | :--- | :--- |"
    ]
    
    for idx, crop_path in enumerate(sample_crops, 1):
        streamer = crop_path.parent.name
        filename = crop_path.name
        
        # Copy to destination
        dest_path = dest_dir / filename
        shutil.copy(crop_path, dest_path)
        
        # Run OCR
        img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"Could not read image: {crop_path}")
            text, conf = "ERROR: Failed to read image", 0.0
        else:
            try:
                text, conf = ocr_with_trocr(img, [], model_path)
            except Exception as e:
                text, conf = f"ERROR: {str(e)}", 0.0
                
        # Markdown row format
        # IMPORTANT: embed images with ![caption](/absolute/path/to/file.png)
        # Note: the local system absolute path to brain dir must have forward slashes for markdown compliance
        img_url = f"file:///{str(dest_path).replace('\\', '/')}"
        img_embed = f"![Crop {idx}]({img_url})"
        
        markdown_lines.append(
            f"| {idx} | `{streamer}` | `{filename}` | {img_embed} | `{text}` | {conf:.2%} |"
        )
        print(f"Processed crop {idx}: {streamer}/{filename} -> OCR: '{text}' (conf: {conf:.2%})")
        
    artifact_path = dest_dir / "crop_ocr_samples.md"
    with artifact_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(markdown_lines))
        
    print(f"\nWritten markdown artifact to: {artifact_path}")

if __name__ == "__main__":
    main()
