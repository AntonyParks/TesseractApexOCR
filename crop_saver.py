"""CropSaver — saves deduplicated preprocessed killfeed crops to disk during live capture."""

import time
import cv2
import numpy as np
from pathlib import Path

from config import CROP_OUTPUT_DIR, CROP_DEDUP_WINDOW, CROP_PHASH_THRESHOLD

_DEFAULT_OUTPUT_DIR = CROP_OUTPUT_DIR


def _compute_phash(img: np.ndarray, hash_size: int = 8) -> int:
    """Compute 64-bit perceptual hash of a grayscale image using DCT."""
    dct_size = hash_size * 4
    small = cv2.resize(img, (dct_size, dct_size), interpolation=cv2.INTER_AREA)
    small_f = np.float32(small)
    dct = cv2.dct(small_f)
    dct_low = dct[:hash_size, :hash_size]
    # Mean excluding DC component at [0,0]
    mean = (dct_low.sum() - dct_low[0, 0]) / (hash_size * hash_size - 1)
    bits = (dct_low > mean).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _hamming(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count('1')


class CropSaver:
    """Saves preprocessed killfeed line crops, deduplicating via perceptual hash.

    Output layout:
        crops/<streamer>/YYYYMMDD_HHMMSS_line{N}_{hex4}.png
    """

    def __init__(self, streamer: str, base_dir: Path | None = None):
        self._streamer = streamer
        self._base_dir = base_dir or _DEFAULT_OUTPUT_DIR
        self._out_dir: Path = self._base_dir / streamer
        self._out_dir.mkdir(parents=True, exist_ok=True)

        # Recent hashes: list of (timestamp, phash_int)
        self._recent: list[tuple[float, int]] = []

        self._saved = 0
        self._skipped = 0

    # ------------------------------------------------------------------
    def update_streamer(self, new_streamer: str) -> None:
        """Switch output directory when the active streamer changes."""
        self._streamer = new_streamer
        self._out_dir = self._base_dir / new_streamer
        self._out_dir.mkdir(parents=True, exist_ok=True)
        # Clear history — new streamer, new context
        self._recent.clear()

    # ------------------------------------------------------------------
    def maybe_save(self, processed_img: np.ndarray, line_index: int, now: float) -> bool:
        """Save *processed_img* if it is not a duplicate of a recent crop.

        Args:
            processed_img: Preprocessed (inverted, upscaled) line image (uint8 grayscale).
            line_index:     0-based index of the killfeed line.
            now:            Current timestamp (time.time()).

        Returns:
            True if the crop was saved, False if skipped as duplicate.
        """
        # Evict stale entries outside the dedup window
        self._recent = [(t, h) for t, h in self._recent if now - t <= CROP_DEDUP_WINDOW]

        h = _compute_phash(processed_img)

        for _, old_h in self._recent:
            if _hamming(h, old_h) <= CROP_PHASH_THRESHOLD:
                self._skipped += 1
                return False

        # Not a duplicate — save
        self._recent.append((now, h))

        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        hex4 = f"{h & 0xFFFF:04x}"
        filename = f"{ts}_line{line_index}_{hex4}.png"
        path = self._out_dir / filename

        cv2.imwrite(str(path), processed_img)
        self._saved += 1
        return True

    # ------------------------------------------------------------------
    @property
    def saved(self) -> int:
        return self._saved

    @property
    def skipped(self) -> int:
        return self._skipped

    def print_stats(self) -> None:
        print(f"[CropSaver] saved={self._saved}  skipped={self._skipped}  dir={self._out_dir}")
