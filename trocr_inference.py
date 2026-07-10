"""Drop-in replacement for ocr_with_positions() using fine-tuned TrOCR.

The model is loaded once (lazy singleton) on first call, thread-safe.
"""

import math
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_model = None
_processor = None
_loaded_path: str | None = None
_load_lock = threading.Lock()


def _load_model(model_path: Path):
    global _model, _processor, _loaded_path
    path_str = str(model_path)
    with _load_lock:
        if _loaded_path == path_str:
            return

        import torch
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel

        print(f"[TrOCR] Loading model from {model_path}...")
        _processor = TrOCRProcessor.from_pretrained(path_str)
        _model = VisionEncoderDecoderModel.from_pretrained(path_str)
        _model.eval()
        if torch.cuda.is_available():
            _model.to("cuda")
        _loaded_path = path_str
        print("[TrOCR] Model loaded.")


def ocr_with_trocr(processed_img: np.ndarray | list[np.ndarray], gun_icon_positions: list[int], model_path: Path) -> tuple[str, float]:
    """Run TrOCR on a preprocessed killfeed strip (or list of strips for parallel batching).

    The model is trained to output <GUN_ICON> tokens directly — no heuristic
    reinsertion needed. gun_icon_positions is accepted for API compatibility
    but not used.

    Args:
        processed_img:      Preprocessed image (uint8), or list of preprocessed images.
        gun_icon_positions: Unused — kept for drop-in compatibility with ocr_with_positions().
        model_path:         Path to saved TrOCR model directory.

    Returns:
        (text, confidence) where confidence is the geometric mean of per-token
        max-softmax probability (0–1). Low confidence indicates uncertain/noisy output.
    """
    import torch

    _load_model(model_path)

    # Convert single image to a list of one image for uniform processing
    is_list = isinstance(processed_img, list)
    images_list = processed_img if is_list else [processed_img]

    pil_imgs = []
    for img in images_list:
        if img.ndim == 2:
            rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            pil_imgs.append(Image.fromarray(rgb))
        else:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            pil_imgs.append(Image.fromarray(rgb))

    device = next(_model.parameters()).device
    # Batch process PIL images in parallel
    pixel_values = _processor(images=pil_imgs, return_tensors="pt").pixel_values.to(device)

    with torch.no_grad():
        output = _model.generate(
            pixel_values,
            max_new_tokens=64,
            return_dict_in_generate=True,
            output_scores=True,
        )

    # Decode all transcriptions in parallel
    decoded_texts = _processor.batch_decode(output.sequences, skip_special_tokens=True)
    decoded_texts = [text.strip() for text in decoded_texts]

    # Compute confidence for each batch item
    batch_size = len(images_list)
    confidences = [0.0] * batch_size
    log_sums = [0.0] * batch_size

    # output.scores is a tuple of shape (num_tokens) where each element is a tensor (batch_size, vocab)
    if output.scores:
        num_steps = len(output.scores)
        for step in output.scores:
            probs = torch.softmax(step, dim=-1) # (batch_size, vocab_size)
            max_probs = probs.max(dim=-1).values # (batch_size,)
            for idx in range(batch_size):
                prob_val = float(max_probs[idx])
                log_sums[idx] += math.log(prob_val) if prob_val > 0 else -10.0
        
        for idx in range(batch_size):
            confidences[idx] = math.exp(log_sums[idx] / num_steps)
    else:
        confidences = [1.0] * batch_size

    # Filter outputs (reject garbage)
    candidates = []
    for text, conf in zip(decoded_texts, confidences):
        # Reject obviously garbage output early (high special-char density).
        gun_icon_stripped = text.replace("<GUN_ICON>", "")
        if gun_icon_stripped:
            alpha_ratio = sum(c.isalnum() or c in " -_." for c in gun_icon_stripped) / len(gun_icon_stripped)
            if alpha_ratio < 0.5:
                candidates.append(("", 0.0))
                continue
        candidates.append((text, conf))

    # Select the candidate with the highest confidence
    # (defaulting to the first one if confidence is equal)
    best_text, best_conf = candidates[0]
    for text, conf in candidates[1:]:
        if conf > best_conf:
            best_text = text
            best_conf = conf

    return best_text, best_conf
