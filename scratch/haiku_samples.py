import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from haiku_validator import validate_killfeed_crop
from trocr_inference import ocr_with_trocr
import anthropic

def main():
    crops_dir = Path("crops")
    model_path = Path("models/trocr_apex")
    
    # The 10 sample crops we used previously
    sample_crops = [
        Path("crops/Breonnaaspen/20260610_221609_line0_8000.png"),
        Path("crops/Breonnaaspen/20260610_221624_line0_6293.png"),
        Path("crops/Burteer/20260610_221741_line0_276f.png"),
        Path("crops/Burteer/20260610_221814_line0_274f.png"),
        Path("crops/Denpride/20260610_221611_line0_1e9c.png"),
        Path("crops/Denpride/20260610_221611_line1_4612.png"),
        Path("crops/Enemyapex/20260610_221621_line0_7270.png"),
        Path("crops/Enemyapex/20260610_221635_line0_72b9.png"),
        Path("crops/Hisandherslive/20260610_221657_line0_03ff.png"),
        Path("crops/Hisandherslive/20260610_221704_line0_03fe.png"),
    ]
    
    print("Running Haiku comparison on the 10 sample crops...")
    print(f"{'No.':<4} {'Filename':<35} {'TrOCR Prediction':<35} {'Haiku Transcription':<35}")
    print("-" * 115)
    
    missing_key = False
    
    for idx, crop_path in enumerate(sample_crops, 1):
        if not crop_path.exists():
            print(f"Crop {crop_path} not found.")
            continue
            
        img = cv2.imread(str(crop_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"{idx:<4} {crop_path.name:<35} [ERROR: Could not load image]")
            continue
            
        # Get TrOCR prediction first
        try:
            trocr_text, _ = ocr_with_trocr(img, [], model_path)
        except Exception as e:
            trocr_text = f"TrOCR Error: {e}"
            
        # Get Haiku prediction
        try:
            haiku_text = validate_killfeed_crop(img)
            if haiku_text is None:
                haiku_text = "EMPTY / REJECTED"
        except anthropic.AnthropicError as ae:
            haiku_text = "API KEY ERROR"
            missing_key = True
        except Exception as e:
            haiku_text = f"Error: {e}"
            
        print(f"{idx:<4} {crop_path.name:<35} {trocr_text:<35} {haiku_text:<35}")
        
    if missing_key:
        print("\n[WARNING] Anthropic API Key is not set or invalid in the environment.")
        print("Please configure your ANTHROPIC_API_KEY in the project's .env file or environment variables.")

if __name__ == "__main__":
    main()
