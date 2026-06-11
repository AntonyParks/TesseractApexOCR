"""Test script to validate gun icon removal on actual killfeed images."""

import cv2
import numpy as np
import pytesseract
from pathlib import Path
import sys

# Same function from ocr.py
def remove_gun_icons(img):
    """Remove gun icon sprites that appear between player names before OCR."""
    cleaned = img.copy()
    _, bright_mask = cv2.threshold(img, 200, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    removed_count = 0
    removed_regions = []

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)

        if w < 15 or h < 10:
            continue

        aspect_ratio = w / h if h > 0 else 0
        area = w * h
        contour_area = cv2.contourArea(contour)
        solidity = contour_area / area if area > 0 else 0

        is_gun_icon = (
            aspect_ratio > 1.5 and aspect_ratio < 5.5 and
            h >= 12 and h <= 40 and
            w >= 18 and w <= 85 and
            area < 2500 and
            solidity > 0.6
        )

        if is_gun_icon:
            margin = 2
            x1 = max(0, x - margin)
            y1 = max(0, y - margin)
            x2 = min(img.shape[1], x + w + margin)
            y2 = min(img.shape[0], y + h + margin)
            cv2.rectangle(cleaned, (x1, y1), (x2, y2), (0, 0, 0), -1)
            removed_count += 1
            removed_regions.append((x, y, w, h))

    return cleaned, removed_count, removed_regions


def test_single_image(img_path):
    """Test gun icon removal on a single image."""
    print(f"\n{'='*70}")
    print(f"Testing: {img_path.name}")
    print('='*70)

    # Read image
    img = cv2.imread(str(img_path))
    if img is None:
        print(f"ERROR: Could not read {img_path}")
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # OCR BEFORE gun icon removal
    tesseract_config = "--oem 3 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_[]:-@() "
    text_before = pytesseract.image_to_string(gray, config=tesseract_config).strip()

    # Remove gun icons
    cleaned, icons_removed, regions = remove_gun_icons(gray)

    # OCR AFTER gun icon removal
    text_after = pytesseract.image_to_string(cleaned, config=tesseract_config).strip()

    # Display results
    print(f"\nBEFORE: {text_before}")
    print(f"AFTER:  {text_after}")
    print(f"\nGun icons detected and removed: {icons_removed}")

    if regions:
        print("\nRemoved regions (x, y, width, height):")
        for x, y, w, h in regions:
            aspect = w/h if h > 0 else 0
            print(f"  - ({x:3d}, {y:2d}, {w:2d}px × {h:2d}px) aspect={aspect:.2f}")

    # Create visualization
    vis_before = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    vis_after = cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)

    # Draw rectangles around removed gun icons
    for x, y, w, h in regions:
        cv2.rectangle(vis_before, (x, y), (x+w, y+h), (0, 0, 255), 2)  # Red boxes

    # Save comparison images
    output_dir = Path("test_results")
    output_dir.mkdir(exist_ok=True)

    comparison = np.hstack([vis_before, vis_after])
    output_path = output_dir / f"comparison_{img_path.name}"
    cv2.imwrite(str(output_path), comparison)
    print(f"\nSaved comparison to: {output_path}")

    return {
        'filename': img_path.name,
        'before': text_before,
        'after': text_after,
        'icons_removed': icons_removed,
        'improved': len(text_after) >= len(text_before) and icons_removed > 0
    }


def find_test_images():
    """Search for test images in common locations."""
    print("\n🔍 Searching for test images...")

    # Check current directory
    current_dir = Path(".")

    # Check common subdirectories
    search_paths = [
        current_dir,
        current_dir / "images",
        current_dir / "test_images",
        current_dir / "apex_training_data" / "images",
        current_dir / "data",
    ]

    # Also check parent directories
    parent_dir = current_dir.parent
    search_paths.extend([
        parent_dir,
        parent_dir / "images",
        parent_dir / "apex_training_data" / "images",
    ])

    found_images = []

    for search_path in search_paths:
        if not search_path.exists():
            continue

        # Look for apex_gent_*.jpg files
        for img_file in search_path.glob("apex_gent_*.jpg"):
            found_images.append(img_file)

        # Also look for any .jpg files if none found
        if not found_images:
            for img_file in search_path.glob("*.jpg"):
                if img_file.stat().st_size < 50000:  # Small files likely killfeed
                    found_images.append(img_file)

    return found_images


def main():
    """Test all killfeed images."""
    print("\n" + "="*70)
    print("GUN ICON REMOVAL TEST SUITE")
    print("="*70)

    # Find test images automatically
    found_images = find_test_images()

    if not found_images:
        print("\n❌ ERROR: No test images found!")
        print("\nPlease do one of the following:")
        print("1. Place your apex_gent_*.jpg files in the current directory")
        print("2. Create an 'images/' folder and put them there")
        print("3. Provide the path as a command line argument:")
        print("   python test_gun_icon_removal.py path/to/images/")

        # Check if user provided a path
        if len(sys.argv) > 1:
            custom_path = Path(sys.argv[1])
            if custom_path.exists():
                print(f"\n📁 Using custom path: {custom_path}")
                found_images = list(custom_path.glob("*.jpg"))

        if not found_images:
            return

    print(f"\n✅ Found {len(found_images)} test images:")
    for img in sorted(found_images)[:5]:
        print(f"   - {img.name}")
    if len(found_images) > 5:
        print(f"   ... and {len(found_images) - 5} more")

    # Test images
    results = []
    for img_path in sorted(found_images):
        result = test_single_image(img_path)
        if result:
            results.append(result)

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"\nTotal images tested: {len(results)}")
    print(f"Total gun icons removed: {sum(r['icons_removed'] for r in results)}")
    print(f"Images improved: {sum(r['improved'] for r in results)}/{len(results)}")

    if results:
        print("\n\nDetailed Results:")
        print(f"{'Filename':<30} {'Icons Removed':<15} {'Improved'}")
        print("-"*70)
        for r in results:
            improved = "✓" if r['improved'] else "✗"
            print(f"{r['filename']:<30} {r['icons_removed']:<15} {improved}")

        print("\n✓ Test complete! Check 'test_results/' folder for comparison images.")
    else:
        print("\n❌ No images could be processed.")


if __name__ == "__main__":
    main()
