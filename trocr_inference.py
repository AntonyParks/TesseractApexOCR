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


def ocr_with_trocr(processed_img: np.ndarray, gun_icon_positions: list[int], model_path: Path) -> tuple[str, float]:
    """Run TrOCR on a preprocessed killfeed strip.

    The model is trained to output <GUN_ICON> tokens directly — no heuristic
    reinsertion needed. gun_icon_positions is accepted for API compatibility
    but not used.

    Args:
        processed_img:      Preprocessed grayscale image (uint8).
        gun_icon_positions: Unused — kept for drop-in compatibility with ocr_with_positions().
        model_path:         Path to saved TrOCR model directory.

    Returns:
        (text, confidence) where confidence is the geometric mean of per-token
        max-softmax probability (0–1). Low confidence indicates uncertain/noisy output.
    """
    import torch

    _load_model(model_path)

    rgb = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2RGB)
    pil_img = Image.fromarray(rgb)

    device = next(_model.parameters()).device
    pixel_values = _processor(images=pil_img, return_tensors="pt").pixel_values.to(device)

    with torch.no_grad():
        output = _model.generate(
            pixel_values,
            max_new_tokens=64,
            return_dict_in_generate=True,
            output_scores=True,
        )

    text = _processor.batch_decode(output.sequences, skip_special_tokens=True)[0].strip()

    # Compute geometric mean of per-token max-softmax probability as confidence.
    scores = output.scores  # tuple of (batch, vocab_size) per generated token
    if scores:
        log_sum = 0.0
        for step in scores:
            probs = torch.softmax(step, dim=-1)
            max_prob = float(probs.max(dim=-1).values[0])
            log_sum += math.log(max_prob) if max_prob > 0 else -10.0
        confidence = math.exp(log_sum / len(scores))
    else:
        confidence = 1.0

    # Reject obviously garbage output early (high special-char density).
    gun_icon_stripped = text.replace("<GUN_ICON>", "")
    if gun_icon_stripped:
        alpha_ratio = sum(c.isalnum() or c in " -_." for c in gun_icon_stripped) / len(gun_icon_stripped)
        if alpha_ratio < 0.5:
            return "", 0.0

    return text, confidence
