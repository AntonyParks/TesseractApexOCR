import cv2
import sys
from pathlib import Path

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import config
    from ocr import preprocess_for_trocr
    from trocr_inference import ocr_with_trocr
except Exception as e:
    print(f"Imports failed: {e}")

def main():
    p = Path("debug_sang.png")
    if not p.exists():
        print("debug_sang.png not found")
        return
        
    img = cv2.imread(str(p))
    
    # Crop Slice y=87, h=28 (aligned row containing player names)
    crop = img[87:115, :]
    
    # Generate both preprocessed crops using the updated production function
    processed_list, _, _ = preprocess_for_trocr(crop)
    
    print("=" * 80)
    print("TESTING PARALLEL BATCH TrOCR OCR INFERENCE")
    print("=" * 80)
    print(f"Input list size: {len(processed_list)}")
    print(f"Image shapes: {[img.shape for img in processed_list]}")
    
    # Run batch ocr
    text, conf = ocr_with_trocr(processed_list, [], Path(config.TROCR_MODEL_PATH))
    
    print("\n[+] Winning Parallel Batch Transcription:")
    print(f"    - Text: {text!r}")
    print(f"    - Conf: {conf:.4f}")
    
    # Verify the contents of the read
    if "Keon_XXL" in text:
        print("\n🟢 SUCCESS: Player name 'Keon_XXL' transcribed perfectly!")
    else:
        print("\n🔴 FAILURE: Player name 'Keon_XXL' was not found in the output.")

if __name__ == "__main__":
    main()
