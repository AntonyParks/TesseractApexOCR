"""Apex Legends killfeed OCR — multi-channel Twitch stream capture."""

import argparse
import csv
import os
import re
import sys
import threading
import time
from collections import defaultdict, Counter, deque
from difflib import SequenceMatcher
from pathlib import Path

# Windows console defaults to cp1252; OCR output can contain arbitrary Unicode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

import av
import cv2
import numpy as np
import pytesseract

from config import *
from calibrate_zone import (
    resolve_static_zone, attempt_calibration_from_frame,
    zones_overlap, commit_calibrated_zone, BYPASS,
)
from crop_saver import CropSaver
from database import PlayerDatabase
from detect_killfeed import detect_for_stream, detect_killfeed_from_frame, detect_content_x_bounds, open_stream, _get_frame_dimensions
from detect_ranked import is_ranked_game
from detect_squads import read_squads_left
import rank_gate
from parsers import parse_killfeed_line

# ---------------------------------------------------------------------------
# EasyOCR lazy initialisation  (loaded once on first use, not at import time)
# ---------------------------------------------------------------------------
_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _get_easyocr_reader():
    """Return the shared EasyOCR Reader, creating it on first call."""
    global _easyocr_reader
    if _easyocr_reader is None:
        with _easyocr_lock:
            if _easyocr_reader is None:               # double-check under lock
                import easyocr
                import torch
                # Cap torch CPU intra-op threads to avoid N_workers*default oversubscription of cores,
                # which crushes multi-stream OCR throughput (config comment; measured ~2x). Process-wide.
                if EASYOCR_TORCH_THREADS:
                    try:
                        torch.set_num_threads(EASYOCR_TORCH_THREADS)
                    except Exception as e:
                        print(f"[EasyOCR] set_num_threads({EASYOCR_TORCH_THREADS}) failed: {e}")
                gpu = EASYOCR_GPU and torch.cuda.is_available()
                
                custom_model = None
                if EASYOCR_CUSTOM_MODEL_DIR.exists() and (EASYOCR_CUSTOM_MODEL_DIR / "apex.pth").exists():
                    custom_model = "apex"
                
                print(f"[EasyOCR] Initialising reader (languages={EASYOCR_LANGUAGES}, gpu={gpu}, custom={custom_model})…")
                if custom_model:
                    _easyocr_reader = easyocr.Reader(
                        EASYOCR_LANGUAGES,
                        gpu=gpu,
                        quantize=EASYOCR_RECOGNIZER_QUANT,
                        recog_network=custom_model,
                        user_network_directory=str(EASYOCR_CUSTOM_MODEL_DIR.absolute()),
                        model_storage_directory=str(EASYOCR_CUSTOM_MODEL_DIR.absolute())
                    )
                else:
                    _easyocr_reader = easyocr.Reader(EASYOCR_LANGUAGES, gpu=gpu,
                                                     quantize=EASYOCR_RECOGNIZER_QUANT)
                # Move the CRAFT detector to the GPU via onnxruntime-directml (bead hy2). Drop-in swap
                # of reader.detector; auto-falls back to torch CRAFT on any failure, so OCR never breaks.
                if EASYOCR_DETECTOR_DML:
                    try:
                        import craft_onnx
                        craft_onnx.install_dml_detector(_easyocr_reader, str(EASYOCR_CRAFT_ONNX))
                    except Exception as e:
                        print(f"[EasyOCR] DirectML detector unavailable, using torch CRAFT: {e}")
                print(f"[EasyOCR] Reader ready.")
    return _easyocr_reader


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def preprocess_for_easyocr(img, stretch_x: float = 1.0, stretch_y: float = 1.0):
    """Simple inverted grayscale preprocessing optimized for EasyOCR."""
    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    else:
        bgr = img.copy()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    return padded, 0, []


def detect_kill_skull(color_img, x0: int, x1: int) -> bool:
    """Check an icon-gap region of the ORIGINAL color crop for the red elimination skull.

    The Apex killfeed marks an actual elimination with a small pure-red skull glyph next to
    the victim's name; knockdown lines show only the weapon icon. Two look-alikes must be
    rejected: the orange circular kill-leader badge (mostly-orange disc whose dark-red rim
    can cross into a red mask; appears on both knocks and kills, means nothing about the
    kill) and saturated game-world background bleeding through the semi-transparent strip.

    Args:
        color_img: original (un-preprocessed) BGR/BGRA crop of the killfeed line.
        x0, x1:    horizontal bounds of the gap in *original* crop coordinates.
    """
    if color_img is None:
        return False
    if color_img.ndim == 3 and color_img.shape[2] == 4:
        color_img = cv2.cvtColor(color_img, cv2.COLOR_BGRA2BGR)
    H, W = color_img.shape[:2]
    x0 = max(0, int(x0) - 3)
    x1 = min(W, int(x1) + 3)
    if x1 - x0 < 6:
        return False
    region = color_img[:, x0:x1]
    regW = region.shape[1]
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 120, 80), (9, 255, 255)) | \
          cv2.inRange(hsv, (171, 120, 80), (180, 255, 255))
    orange = cv2.inRange(hsv, (10, 100, 80), (26, 255, 255))
    # A transient red flash behind the translucent strip (damage flash, red banner) floods
    # the whole gap red and makes the skull unrecoverable by color in THIS frame. Bail out
    # (treat as knock for this read): the same line is OCR'd many times over its ~10s
    # visibility and the event tracker votes across reads, so unflashed frames decide.
    if cv2.countNonZero(red) > 0.45 * red.size:
        return False
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    n, _, stats, cents = cv2.connectedComponentsWithStats(red, 8)
    # Thresholds calibrated on labeled crops (2026-07-04): true skulls measured
    # area 47-180, aspect 0.50-1.00, fill 0.60-0.81, center at 0.84-0.88 of gap width.
    # Rejected look-alikes: kill-leader badge rim arc (posx 0.96, aspect 0.41), solid
    # red UI bar (fill 0.88), weapon-icon red tints (positioned left/middle of gap).
    for i in range(1, n):
        cx, cy, cw, ch, area = stats[i]
        if not (40 <= area <= 450):
            continue
        if not (0.45 <= cw / max(ch, 1) <= 1.8):
            continue
        if not (7 <= ch <= 30):
            continue
        if not (0.10 * H <= cents[i][1] <= 0.90 * H):
            continue
        fill = area / (cw * ch)
        if not (0.45 <= fill <= 0.92):
            continue  # scraggly tints too hollow; only a fully-solid bar/disc exceeds this, and
            #           those are already caught by the size/aspect/orange/context gates below.
            #           Upper bound raised 0.84->0.92 (2026-07-14): at 936p the skull renders more
            #           solid (measured fill 0.88 on a confirmed kill incl. the game-winning kill,
            #           validated vs the vision ground truth) than the 1080p 0.60-0.81 calibration.
        posx = (cx + cw / 2) / regW
        if not (0.50 <= posx <= 0.93):
            continue  # skull sits just left of the victim name at the gap's right end;
            #           weapon tints sit left/middle, badge arcs clip at the very edge
        # Kill-leader badge (orange disc): its red rim candidate carries orange inside
        # its own bbox. True skulls measured <= 0.19 interior orange.
        box_orange = int(cv2.countNonZero(orange[cy:cy + ch, cx:cx + cw]))
        if box_orange > 0.45 * (cw * ch):
            continue
        # Background bleed-through: on the dark strip the skull's surroundings are
        # low-saturation; a colorful context means game world showing through.
        pad = 8
        bx0, by0 = max(0, cx - pad), max(0, cy - pad)
        bx1, by1 = min(regW, cx + cw + pad), min(H, cy + ch + pad)
        ctx_hsv = hsv[by0:by1, bx0:bx1]
        n_ctx = (by1 - by0) * (bx1 - bx0)
        sat = cv2.inRange(ctx_hsv, (0, 100, 60), (180, 255, 255))
        other_sat = cv2.countNonZero(sat) - cv2.countNonZero(red[by0:by1, bx0:bx1]) \
            - cv2.countNonZero(orange[by0:by1, bx0:bx1])
        if other_sat > 0.45 * n_ctx:
            continue  # colorful surroundings = game world bleeding through the translucent strip.
            #           Raised 0.25->0.45 (2026-07-14): a real skull on a BRIGHT map (sky behind the
            #           strip) measured ctx_sat 0.42 (the game-winning kill), dropped as a knock. The
            #           skull-specific shape/size/pos/orange gates above carry the precision; validated
            #           recall-up / precision-flat on the vision ground truth via _score_ocr.
        return True
    return False


def preprocess_for_trocr(img, stretch_x: float = 1.0, stretch_y: float = 1.0):
    """HSV Value channel and Bilateral + Otsu binarization preprocessing optimized for parallel TrOCR batching."""
    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    else:
        bgr = img.copy()
        
    # Pipeline 1: HSV Value Channel (Anti-aliased)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    _, _, v_chan = cv2.split(hsv)
    inverted_v = cv2.bitwise_not(v_chan)
    upscaled_v = cv2.resize(inverted_v, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded_v = cv2.copyMakeBorder(upscaled_v, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # Pipeline 2: Bilateral Smooth + Otsu Threshold (High-contrast binary)
    smoothed = cv2.bilateralFilter(bgr, 9, 75, 75)
    gray_smooth = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray_smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(otsu) < 127:
        otsu = cv2.bitwise_not(otsu)
    upscaled_otsu = cv2.resize(otsu, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded_otsu = cv2.copyMakeBorder(upscaled_otsu, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    
    # Return both preprocessed images in a list
    return [padded_v, padded_otsu], 0, []


def preprocess(img, stretch_x: float = 1.0, stretch_y: float = 1.0):
    """Invert colors, remove gun icons, upscale 2x.

    Args:
        stretch_x: Horizontal stretch factor relative to 1080p native (e.g. 1920/1440 = 1.333
                   for a 4:3 game stretched to fill a 16:9 broadcast, or frame_w/content_w for
                   black-bar streams).  Scales gun-icon width and aspect-ratio filters.
        stretch_y: Vertical scale factor relative to 1080p (e.g. 1440/1080 = 1.333 for a 1440p
                   stream).  Scales gun-icon height filter.

    Returns: (processed_image, icons_removed, gun_icon_positions)
    """
    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    else:
        bgr = img.copy()
        
    if USE_EASYOCR:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        inverted = cv2.bitwise_not(gray)
        upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        padded = cv2.copyMakeBorder(upscaled, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
        return padded, 0, []
        
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    
    # White Mask (high value, low saturation)
    lower_white = np.array([0, 0, 160])
    upper_white = np.array([180, 45, 255])
    
    # Yellow Mask (teammate names)
    lower_yellow = np.array([15, 60, 140])
    upper_yellow = np.array([35, 255, 255])
    
    # Red Mask (enemy names)
    lower_red1 = np.array([0, 60, 120])
    upper_red1 = np.array([12, 255, 255])
    lower_red2 = np.array([168, 60, 120])
    upper_red2 = np.array([180, 255, 255])
    
    mask_white = cv2.inRange(hsv, lower_white, upper_white)
    mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)
    mask_red1 = cv2.inRange(hsv, lower_red1, upper_red1)
    mask_red2 = cv2.inRange(hsv, lower_red2, upper_red2)
    
    combined_mask = mask_white | mask_yellow | mask_red1 | mask_red2
    inverted = cv2.bitwise_not(combined_mask)

    # Existing gun icon removal logic
    dark_mask = cv2.bitwise_not(inverted)
    contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    gun_icon_positions = []
    icons_removed = 0
    temp_inverted = inverted.copy()

    _icon_min_h  = int(15  * stretch_y)
    _icon_max_h  = int(40  * stretch_y)
    _icon_max_w  = int(100 * stretch_x)
    _icon_max_ar = 6.0 * stretch_x

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        aspect_ratio = w / h if h > 0 else 0
        if 2.0 < aspect_ratio < _icon_max_ar and _icon_min_h < h < _icon_max_h and 40 < w < _icon_max_w:
            cv2.rectangle(temp_inverted, (x, y), (x + w, y + h), (255, 255, 255), -1)
            gun_icon_positions.append(x + w // 2)
            icons_removed += 1

    upscaled = cv2.resize(temp_inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    
    # Add a 15px white padding all around (quiet zone) so Tesseract doesn't merge text boundaries with image edges
    padded = cv2.copyMakeBorder(
        upscaled, 15, 15, 15, 15,
        cv2.BORDER_CONSTANT, value=255
    )
    
    # Adjust gun icon positions for the 15px left padding
    gun_icon_positions = [pos * 2 + 15 for pos in gun_icon_positions]

    return padded, icons_removed, gun_icon_positions


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def ocr_with_easyocr(processed_img, color_img=None):
    """Run EasyOCR and insert <GUN_ICON>/<KILL_ICON> tokens at large horizontal gaps.

    Args:
        processed_img: Preprocessed greyscale image (already inverted / padded).
        color_img:     Optional ORIGINAL color crop the processed image was derived from.
                       When provided, each icon gap is checked for the red elimination
                       skull (see detect_kill_skull) and emits <KILL_ICON> instead of
                       <GUN_ICON> if found. Callers without color (benchmarks, evaluator)
                       get the legacy <GUN_ICON>-only behavior.

    Returns:
        Transcribed text string with gap markers between word bounding boxes.
    """
    reader = _get_easyocr_reader()
    # detail=1 returns (bbox, text, confidence) per word
    results = reader.readtext(processed_img, detail=1, paragraph=False)
    if not results:
        return ""

    # Sort word boxes by the left edge of the bounding box (top-left x)
    results.sort(key=lambda r: min(pt[0] for pt in r[0]))

    text_parts = []
    prev_right = 0
    for bbox, word, _conf in results:
        if not word.strip():
            continue
        x_left = min(pt[0] for pt in bbox)
        x_right = max(pt[0] for pt in bbox)
        if prev_right > 0:
            gap = x_left - prev_right
            if gap > EASYOCR_GAP_THRESHOLD:
                marker = " <GUN_ICON> "
                if color_img is not None:
                    # Map gap bounds from processed coords back to the original crop:
                    # preprocess_for_easyocr does 2x upscale then a 15px border pad.
                    ox0 = (prev_right - 15) / 2
                    ox1 = (x_left - 15) / 2
                    if detect_kill_skull(color_img, ox0, ox1):
                        marker = " <KILL_ICON> "
                text_parts.append(marker)
            elif gap > 5:
                text_parts.append(" ")
        text_parts.append(word)
        prev_right = x_right
    return ''.join(text_parts)


def ocr_with_positions(processed_img, config):
    """Run Tesseract and insert <GUN_ICON> tokens at detected gaps."""
    try:
        data = pytesseract.image_to_data(processed_img, config=config, output_type=pytesseract.Output.DICT)
        text_parts = []
        prev_right = 0
        for i, word in enumerate(data['text']):
            if not word.strip():
                continue
            left = data['left'][i]
            if prev_right > 0:
                gap_size = left - prev_right
                if gap_size > 80:
                    text_parts.append(" <GUN_ICON> ")
                elif gap_size > 10:
                    text_parts.append(" ")
            text_parts.append(word)
            prev_right = left + data['width'][i]
        return ''.join(text_parts)
    except Exception:
        return pytesseract.image_to_string(processed_img, config=config).strip()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def is_empty_line(line_img) -> bool:
    return line_img.var() < EMPTY_LINE_VARIANCE


def looks_like_noise(text: str) -> bool:
    if len(text) < MIN_TEXT_LENGTH:
        return True
    alpha_ratio = sum(c.isalnum() or c == ' ' for c in text) / len(text)
    if alpha_ratio < MIN_ALPHA_RATIO:
        return True
    if re.match(r'^[0-9\-\[\]\(\)]+$', text):
        return True
    if text.count('—') > 3 or text.count('|') > 3 or text.count('-') > 5:
        return True
    if len(set(text)) < len(text) / 3:
        return True
    if re.match(r'^[a-z]{1,2}\s', text):
        return True
    return False


# ---------------------------------------------------------------------------
# Event deduplication / voting
# ---------------------------------------------------------------------------

def _gap_marker_class(text: str):
    """'kill' / 'gun' / None depending on which icon-gap marker the text carries."""
    tl = text.lower()
    if '<kill_icon>' in tl:
        return 'kill'
    if '<gun_icon>' in tl:
        return 'gun'
    return None


# Matches any icon-gap marker token, including forms mangled by char-level voting
# (<gun_icon>, <kill_icon>, <gil_icon>, gunicon, killicon).
_ICON_TOKEN_RE = re.compile(r'<[^>]*icon[^>]*>|\b(?:gun|kill)icon\b', re.IGNORECASE)


def _neutralize_marker(text: str) -> str:
    """Collapse kill/gun markers to one neutral token so two reads of the same physical line
    match regardless of which icon the noisy skull detector emitted (ICON_VOTE_ENABLED path)."""
    return _ICON_TOKEN_RE.sub('<ICON>', text)


def _icon_vote(variants):
    """Persistence-aware kill/knock decision over one merged line's reads. Returns
    (decision, stats). A true elimination shows a SUSTAINED run of kill-icon reads (the skull is
    present the whole ~6s); a detector false positive is sparse/isolated. So require both a
    contiguous kill-run AND a minimum kill fraction, else default to knock ('gun')."""
    seq = [m for (_, txt) in sorted(variants, key=lambda v: v[0]) if (m := _gap_marker_class(txt))]
    n = len(seq)
    n_kill = seq.count('kill')
    longest = cur = 0
    for m in seq:
        cur = cur + 1 if m == 'kill' else 0
        longest = max(longest, cur)
    is_kill = n > 0 and longest >= ICON_KILL_MIN_RUN and n_kill >= ICON_KILL_MIN_FRAC * n
    return ('kill' if is_kill else 'gun',
            {'reads': len(variants), 'marked': n, 'kill': n_kill, 'gun': seq.count('gun'), 'kill_run': longest})


def _apply_icon_decision(text: str, decision: str) -> str:
    """Force the merged line's marker to match the vote so downstream has_kill_marker() yields
    the right event_type. has_kill_marker only needs one kill token present."""
    out = _ICON_TOKEN_RE.sub('<GUN_ICON>', text)
    if decision == 'kill':
        out = out.replace('<GUN_ICON>', '<KILL_ICON>', 1)
    return out


def find_recent_match(text: str, event_tracker: dict, now: float, threshold: float = 0.75) -> str:
    best_match = None
    best_ratio = 0
    text_marker = _gap_marker_class(text)
    cmp_text = _neutralize_marker(text).lower() if ICON_VOTE_ENABLED else text.lower()
    for canonical_text, variants in event_tracker.items():
        if not variants:
            continue
        last_seen = max(v[0] for v in variants)
        if now - last_seen > EVENT_WINDOW:
            continue
        # A knockdown line and its later elimination line read near-identically except for the
        # gap marker (<GUN_ICON> vs <KILL_ICON>). Legacy: track them separately (DIFFERENT game
        # events). With ICON_VOTE_ENABLED we intentionally MERGE them so a persistence-aware vote
        # (_icon_vote at flush) can reject icon-detector false positives -- the kill/knock label
        # is decided from the full read population, not a single noisy frame.
        if ICON_VOTE_ENABLED:
            ratio = SequenceMatcher(None, cmp_text, _neutralize_marker(canonical_text).lower()).ratio()
        else:
            cand_marker = _gap_marker_class(canonical_text)
            if text_marker and cand_marker and text_marker != cand_marker:
                continue
            ratio = SequenceMatcher(None, text.lower(), canonical_text.lower()).ratio()
        if ratio > best_ratio and ratio >= threshold:
            best_ratio = ratio
            best_match = canonical_text
    return best_match if best_match else text


def find_best_alignment(texts):
    if len(texts) < 2:
        return {i: [c] for i, c in enumerate(texts[0])} if texts else {}
    reference = texts[0]
    alignments = defaultdict(list)
    for text in texts:
        matcher = SequenceMatcher(None, reference, text)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                for k in range(i2 - i1):
                    alignments[i1 + k].append(text[j1 + k])
            elif tag == 'replace':
                for k in range(min(i2 - i1, j2 - j1)):
                    alignments[i1 + k].append(text[j1 + k])
    return alignments


def vote_on_aligned_positions(alignments):
    consensus = []
    for pos in sorted(alignments.keys()):
        chars = alignments[pos]
        valid_chars = [c for c in chars if c.isprintable()]
        if not valid_chars:
            continue
        counter = Counter(valid_chars)
        most_common_char, count = counter.most_common(1)[0]
        if count >= len(chars) * 0.4:
            consensus.append(most_common_char)
    return ''.join(consensus)


def align_and_vote(variants):
    if not variants:
        return None
    if len(variants) == 1:
        return variants[0][1]
    texts = [v[1].lower() for v in variants]
    cleaned_texts = []
    for text in texts:
        # Only strip leading noise like hyphens, underscores, pipes
        cleaned = re.sub(r'^[-_|.=+*]+', '', text).strip()
        if cleaned:
            cleaned_texts.append(cleaned)
    if not cleaned_texts:
        return variants[0][1]
    reference = max(cleaned_texts, key=lambda t: sum(c.isalnum() for c in t))
    alignments = find_best_alignment([reference] + [t for t in cleaned_texts if t != reference])
    consensus = vote_on_aligned_positions(alignments)
    consensus = re.sub(r'\s+', ' ', consensus).strip()
    return consensus


def pick_best_variant(variants):
    if not variants:
        return None
    if len(variants) < 3:
        from parsers import normalize_common_phrases
        normalized_counts = Counter()
        for timestamp, text in variants:
            canonical = normalize_common_phrases(text)
            normalized_counts[canonical] += 1
        if not normalized_counts:
            return variants[0][1]
        best = max(normalized_counts.items(), key=lambda x: (x[1], len(x[0])))
        return best[0]
    consensus = align_and_vote(variants)
    from parsers import normalize_common_phrases
    normalized = normalize_common_phrases(consensus)
    return normalized if normalized else consensus


def flush_old_events(event_tracker, now, event_crops=None, streamer=""):
    to_write = []
    to_delete = []
    for canonical_text, variants in event_tracker.items():
        if not variants:
            continue
        last_seen = max(v[0] for v in variants)
        if now - last_seen > EVENT_WINDOW:
            best_text = pick_best_variant(variants)
            if best_text:
                # Persistence-aware kill/knock vote: decide the merged line's label from the full
                # read population instead of trusting whichever icon the last frame happened to
                # read. ICON_VOTE_LOG instruments the decision even when the vote is disabled, so
                # thresholds can be calibrated before ICON_VOTE_ENABLED is flipped on.
                if (ICON_VOTE_ENABLED or ICON_VOTE_LOG):
                    decision, st = _icon_vote(variants)
                    if ICON_VOTE_LOG and st['kill'] > 0:
                        print(f"[IconVote:{streamer}] '{best_text[:48]}' reads={st['reads']} "
                              f"kill={st['kill']} gun={st['gun']} kill_run={st['kill_run']} "
                              f"-> {decision}" + ("" if ICON_VOTE_ENABLED else " (log-only)"))
                    if ICON_VOTE_ENABLED:
                        best_text = _apply_icon_decision(best_text, decision)
                crop_tuple = event_crops.pop(canonical_text, None) if event_crops is not None else None
                crop = crop_tuple[0] if crop_tuple else None
                crop_filename = crop_tuple[1] if crop_tuple else ""
                to_write.append((last_seen, best_text, crop, crop_filename))
            to_delete.append(canonical_text)
    for canonical_text in to_delete:
        del event_tracker[canonical_text]
        if event_crops is not None:
            event_crops.pop(canonical_text, None)
    return to_write


# ---------------------------------------------------------------------------
# ChannelWorker
# ---------------------------------------------------------------------------

def _parse_channel_arg(raw: str) -> str:
    """Strip URL prefix and return lowercase Twitch username."""
    raw = raw.strip().lower()
    for prefix in ("https://www.twitch.tv/", "https://twitch.tv/",
                   "http://www.twitch.tv/", "http://twitch.tv/", "twitch.tv/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw.rstrip("/")


def _display_name(username: str) -> str:
    """Resolve a Twitch username to a display name via TWITCH_CHANNELS, else title-case it."""
    return TWITCH_CHANNELS.get(username, username.title())


# ---------------------------------------------------------------------------
# Crop-quality helpers
# ---------------------------------------------------------------------------

# Keywords that appear in Apex killfeed lines but NOT in tournament overlays or lobby screens.
_KILLFEED_KEYWORDS = frozenset({
    'gun_icon', 'gunicon', 'reviving', 'bleed', 'spotted',
    'shield', 'pinged', 'scan', 'audio', 'eliminated',
})


def _has_killfeed_content(text: str) -> bool:
    """Return True if OCR text looks like it came from the Apex killfeed.

    Filters out tournament bracket overlays ("TEAM 6 [ALLIANCE]"),
    lobby screens ("YOUR TEAM"), and other non-gameplay content that
    passes is_empty_line() but contains no killfeed markers.
    """
    low = text.lower()
    return any(kw in low for kw in _KILLFEED_KEYWORDS)


def _has_non_latin_script(text: str) -> bool:
    """Return True if text contains CJK, Cyrillic, or other non-Latin characters.

    Used to detect streams where the game UI is in a non-Latin language
    (e.g. Japanese, Korean, Russian).  Romance languages (Italian, French,
    Portuguese) use Latin script and are NOT flagged.
    """
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF    # CJK Unified Ideographs
                or 0x3040 <= cp <= 0x30FF  # Hiragana / Katakana
                or 0x0400 <= cp <= 0x04FF  # Cyrillic
                or 0x0370 <= cp <= 0x03FF  # Greek
                or 0x0600 <= cp <= 0x06FF  # Arabic
                or 0x0E00 <= cp <= 0x0E7F):  # Thai
            return True
    return False


class ChannelWorker(threading.Thread):
    """Watches one Twitch channel: auto-detects killfeed, then OCRs and writes to SQLite."""

    def __init__(self, twitch_username: str,
                 db: PlayerDatabase, db_lock: threading.Lock):
        display = _display_name(twitch_username)
        super().__init__(daemon=True, name=f"worker-{display}")
        self.username = twitch_username
        self.streamer = display          # used for logging
        # Per-streamer killfeed search-zone override (None falls back to the generic
        # global search box in detect_killfeed.py, e.g. for unconfigured/ad-hoc streamers)
        self._search_zone = STREAMER_SEARCH_ZONES.get(display)
        self.db = db
        self.db_lock = db_lock
        self._stop = threading.Event()
        self.crop_saver = CropSaver(display)
        self.noise_crop_saver = CropSaver(display, base_dir=NOISE_CROP_OUTPUT_DIR)
        # Set once per stream connection; used to handle black bars and stretched resolutions
        self._content_x0: int = 0
        self._content_x1: int | None = None
        self._stretch_x: float = 1.0   # horizontal stretch (1920/1440 = 1.333 for 4:3→16:9)
        self._stretch_y: float = 1.0   # vertical scale relative to 1080p baseline
        self._procs: list = []           # [sl_proc, ff_proc]
        self._non_latin_streak: int = 0
        self._non_english_warned: bool = False
        # Live auto-calibration state: while _calib_needed, the OCR loop fires one Claude vision
        # classification roughly every AUTO_CALIBRATE_INTERVAL_SECONDS (on frames that actually
        # have killfeed candidates) until a clean killfeed is captured and a tight zone is locked.
        self._calib_needed: bool = False
        self._last_calib_attempt: float = 0.0
        self._calib_attempts: int = 0
        self._calib_candidate: dict | None = None   # first-strike zone, awaiting confirmation
        # Match-boundary frame capture (data collection for future squad-eliminated/placement/
        # lobby screen detection; see config.SAVE_BOUNDARY_FRAMES). Independent of killfeed
        # detection -- these screens replace the killfeed entirely, so capture unconditionally.
        self._last_boundary_capture: float = 0.0
        # Squads-left observed-placement tracking (bead hmz, log-only). Monotonic non-increasing over
        # a match; a LARGE jump UP (e.g. 3 -> 18) means a new match. Small ups (16->18) are OCR digit
        # misreads (6<->8) -- smoothed out by requiring a value to repeat before committing.
        self._last_squads_read: float = 0.0
        self._squads_left: int | None = None
        self._squads_pending: int | None = None
        self._squads_pending_n: int = 0   # consecutive confirming reads for the pending candidate
        self._squads_pending_ts: float = 0.0  # wall-clock when the current decrease candidate was FIRST
                                               # seen -- the real wipe is ~here, not at (lagged) commit
        # Placement correlation (bead hmz): a squads-left DECREMENT means a squad in this lobby was
        # wiped (placing Nth). Correlate it with the killfeed elimination(s) that caused it. FIRST
        # goal per advisor -- TEST COVERAGE: do decrements coincide with captured elims at all (the
        # feed is comprehensive but our OCR capture of it is not)? Log-only; attribution/ELO later.
        self._recent_elims: deque = deque(maxlen=60)   # (epoch_ts, victim) for Kill/BleedOut
        self._decr_total: int = 0
        self._decr_with_elims: int = 0
        # A committed decrement is correlated LATER, not at commit: an elim only enters _recent_elims
        # when flush_old_events fires (EVENT_WINDOW=6s after its last read), but a decrement can commit
        # ~1-4s after the wipe, BEFORE its causing elims have flushed in. So queue the decrement and
        # evaluate coverage once the elims have matured. Each entry: (anchor_ts, drop, placed_from, to).
        self._pending_placements: list = []
        # Event-triggered squads re-read: an elimination makes a squads-left decrement likely, so read
        # the counter right after one (min 1s apart) instead of only every SQUADS_TRACK_INTERVAL_SECONDS.
        # Cheaper than a uniform fast grid (fires only when kills happen) and pins the read ~1s from the
        # finishing kill, tightening the decrement<->elim correlation window. Killfeed OCR already runs
        # ~2x/s/stream, so one extra squads read per kill-cluster is marginal cost.
        self._squads_read_asap: bool = False
        self._squads_lowres_logged: bool = False   # one-time notice when a sub-900p stream is skipped

    def stop(self):
        self._stop.set()
        for p in self._procs:
            p.terminate()

    # ------------------------------------------------------------------
    def _open_stream(self):
        try:
            container, procs = open_stream(self.username)
            self._procs = procs
            return container
        except Exception as e:
            print(f"[{self.streamer}] ERROR opening stream: {e}")
            return None

    def _sample_is_ranked(self, container: av.container.Container) -> bool:
        """Read a few frames and return True if any shows a ranked badge."""
        checked = 0
        for packet in container.demux(video=0):
            if checked >= RANKED_CHECK_FRAMES:
                break
            try:
                for frame in packet.decode():
                    arr = frame.to_ndarray(format='bgra')
                    if is_ranked_game(arr, arr.shape[1], arr.shape[0]):
                        return True
                    checked += 1
                    break
            except Exception:
                pass
        return False

    def _sample_is_master_pred(self, container: av.container.Container) -> bool:
        """Master/Predator daily gate (bead 2mo). Classify this streamer's rank ONCE per day from
        IN-GAME frames -- the rank badge only renders during a live match; menus/lobbies lack it and
        their red UI would false-trigger Predator. Cache the tier for the day; a below-Master streamer
        is re-checked the next day (they may have ranked up). Colour-only (rank_gate), no OCR of the
        badge -- the only OCR here is the squads-HUD read that confirms the frame is in-game."""
        cached = rank_gate.cached_tier(self.streamer)
        if cached is not None:
            return rank_gate.should_collect(cached)

        def ocr_region(reg):
            proc = preprocess_for_easyocr(reg)[0]
            return ocr_with_easyocr(proc, color_img=reg)

        votes: list[str] = []
        checked = 0
        last_badge = None                           # (crop, red, pur) of the last in-game frame, for sampling
        for packet in container.demux(video=0):
            if checked >= MASTER_PRED_MAX_FRAMES or len(votes) >= MASTER_PRED_MIN_INGAME:
                break
            try:
                for frame in packet.decode():
                    checked += 1
                    arr = frame.to_ndarray(format='bgra')
                    squads, _, _ = read_squads_left(arr, ocr_region)
                    if squads is None:
                        break                       # not in-game -> don't classify off a lobby/menu frame
                    bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
                    badge = rank_gate._crop_badge(bgr)
                    red, pur = rank_gate._band_fracs(badge)
                    votes.append(rank_gate.classify_from_bands(red, pur))
                    last_badge = (badge, red, pur)
                    break                           # one frame per packet
            except Exception:
                pass

        tier = rank_gate.aggregate(votes, min_votes=MASTER_PRED_MIN_INGAME)
        if tier is None:
            # too few in-game frames (streamer still in lobby) -> undecided; do NOT cache, retry later.
            print(f"[{self.streamer}] Master/Pred check: only {len(votes)} in-game frame(s) — retrying.")
            return False
        rank_gate.set_tier(self.streamer, tier)
        if RANK_GATE_SAMPLE_LOG and last_badge is not None:  # passive cross-rank sample capture (incl. Diamond in OTHER)
            rank_gate.record_sample(self.streamer, tier, last_badge[1], last_badge[2], last_badge[0])
        keep = rank_gate.should_collect(tier)
        print(f"[{self.streamer}] Rank classified {tier} (cached today) — "
              f"{'collecting' if keep else 'skipping (re-check tomorrow)'}.")
        return keep

    def _detect_killfeed(self, container: av.container.Container,
                         fw: int, fh: int) -> list[dict]:
        """Keep sampling frames until >= DETECT_MIN_LINES regions are found."""
        attempt = 0
        not_ranked_streak = 0
        wait_cycles = 0
        while not self._stop.is_set():
            attempt += 1
            if attempt > DETECT_MAX_ATTEMPTS:
                print(f"[{self.streamer}] No killfeed found after {DETECT_MAX_ATTEMPTS} attempts — dropping, rotating to next streamer.")
                return []
            regions, n_read = detect_for_stream(
                container, fw, fh,
                content_x0=self._content_x0,
                content_x1=self._content_x1,
                search_zone=self._search_zone,
            )
            if len(regions) >= DETECT_MIN_LINES:
                coords = [(r['left'], r['top'], r['width'], r['height']) for r in regions]
                print(f"[{self.streamer}] Detected {len(regions)} killfeed lines "
                      f"(attempt {attempt}, {n_read} frames): {coords}")
                return regions

            if RANKED_STREAMS_ONLY:
                gate_ok = (self._sample_is_master_pred(container) if MASTER_PRED_ONLY
                           else self._sample_is_ranked(container))
                if gate_ok:
                    not_ranked_streak = 0
                    print(f"[{self.streamer}] Detection attempt {attempt}: "
                          f"only {len(regions)} line(s) found — "
                          f"{'master/pred' if MASTER_PRED_ONLY else 'ranked'} game active, retrying...")
                else:
                    not_ranked_streak += 1
                    if not_ranked_streak >= RANKED_NOT_RANKED_STREAK:
                        wait_cycles += 1
                        if wait_cycles >= RANKED_MAX_WAIT_CYCLES:
                            print(f"[{self.streamer}] Not in ranked after "
                                  f"{wait_cycles} wait cycles — dropping, rotating to next streamer.")
                            return []
                        print(f"[{self.streamer}] Not in ranked game — "
                              f"waiting {RANKED_NOT_RANKED_WAIT}s before retry "
                              f"({wait_cycles}/{RANKED_MAX_WAIT_CYCLES})...")
                        self._stop.wait(RANKED_NOT_RANKED_WAIT)
                        not_ranked_streak = 0
                    else:
                        print(f"[{self.streamer}] Detection attempt {attempt}: "
                              f"only {len(regions)} line(s) found — not ranked ({not_ranked_streak}/{RANKED_NOT_RANKED_STREAK})...")
            else:
                print(f"[{self.streamer}] Detection attempt {attempt}: "
                      f"only {len(regions)} line(s) found — waiting for gameplay...")
        return []

    # ------------------------------------------------------------------
    def run(self):
        print(f"[{self.streamer}] Connecting to twitch.tv/{self.username}...")
        container = self._open_stream()
        if container is None:
            return

        # A stream can open but expose no video track (observed live on 'Notworkinmd', 2026-07-10):
        # container.demux(video=0) then raises IndexError from the for-statement's iterator setup,
        # BEFORE any try/except below, crashing the worker thread ungracefully. Guard it here so a
        # no-video stream degrades exactly like an unopenable one — return and let the parent's
        # worker-died rotation handle it. (bd TesseractApexOCR-rba)
        if not container.streams.video:
            print(f"[{self.streamer}] ERROR: stream has no video track — dropping, rotating.")
            return

        print(f"[{self.streamer}] Stream opened. Detecting killfeed regions...")

        # Get frame dimensions from stream metadata
        fw, fh = _get_frame_dimensions(container)

        # Detect black bars and stretch factors from the first available frame
        for packet in container.demux(video=0):
            try:
                for frame in packet.decode():
                    arr = frame.to_ndarray(format='bgra')
                    self._content_x0, self._content_x1 = detect_content_x_bounds(arr)
                    break
            except Exception:
                pass
            break

        content_w = (self._content_x1 or fw) - self._content_x0
        self._stretch_x = fw / content_w if content_w > 0 else 1.0
        self._stretch_y = fh / 1080.0

        if self._content_x0 > fw * 0.02:
            print(f"[{self.streamer}] Black bars detected: game content "
                  f"x={self._content_x0}–{self._content_x1} ({content_w}px / {fw}px), "
                  f"stretch_x={self._stretch_x:.3f}")
        if abs(self._stretch_y - 1.0) > 0.05:
            print(f"[{self.streamer}] Non-1080p stream: {fw}×{fh}, stretch_y={self._stretch_y:.3f}")

        # Resolve any INSTANT zone (hardcoded STREAMER_SEARCH_ZONES / previously-cached / prior
        # bypass) — no network, no blocking. If none exists, the streamer starts on the generic
        # box and is calibrated LIVE in the OCR loop below: one Claude vision classification
        # roughly once a minute (only on frames that have killfeed candidates) until a clean
        # killfeed is captured, then the tight zone is locked in live and polling stops.
        if self._search_zone is None and AUTO_CALIBRATE_ZONES:
            static = resolve_static_zone(self.streamer)
            if static == BYPASS:
                print(f"[{self.streamer}] Bypassed (cached) — killfeed overlay could not be "
                      f"reliably isolated for this streamer. Not OCR-ing. Rotating to next streamer.")
                container.close()
                return
            elif static is not None:
                self._search_zone = static
                print(f"[{self.streamer}] Using cached calibrated zone: {static}")
            else:
                self._calib_needed = True

        # Phase 1: detect killfeed line positions
        killfeed_lines = self._detect_killfeed(container, fw, fh)
        if not killfeed_lines:
            container.close()
            return

        max_width = max(line["width"] for line in killfeed_lines)
        max_width_upscaled = max_width * 2
        event_tracker = defaultdict(list)
        event_crops: dict[str, np.ndarray] = {}
        last_process    = 0.0
        _frames_active  = 0      # frames where killfeed was visible → OCR ran
        _frames_skipped = 0      # frames where killfeed was blank   → OCR skipped
        _last_stats_ts  = time.time()
        _STATS_INTERVAL = 60     # seconds between stats log lines

        # Phase 2: OCR loop — continues on the same container
        try:
            for packet in container.demux(video=0):
                if self._stop.is_set():
                    break
                try:
                    frames = packet.decode()
                except Exception:
                    continue

                for frame in frames:
                    if self._stop.is_set():
                        break

                    now = time.time()
                    if now - last_process < FRAME_PROCESS_INTERVAL:
                        continue
                    last_process = now

                    # Drain matured placement decrements (bead hmz): evaluate coverage once the causing
                    # elims have had time to flush into _recent_elims (EVENT_WINDOW + margin after the
                    # wipe). The window is time-anchored, so a late evaluation is exact.
                    if self._pending_placements:
                        _mature = EVENT_WINDOW + 3
                        _still = []
                        for anchor, drop, pfrom, pto in self._pending_placements:
                            if now - anchor < _mature:
                                _still.append((anchor, drop, pfrom, pto))
                                continue
                            lead = SQUADS_TRACK_INTERVAL_SECONDS + 2   # wipe precedes the visible drop
                            vics = [v for (t, v) in self._recent_elims if t >= anchor - lead]
                            self._decr_total += 1
                            if vics:
                                self._decr_with_elims += 1
                            cov = f"{self._decr_with_elims}/{self._decr_total}"
                            print(f"[Placement:{self.streamer}] {drop} squad(s) wiped "
                                  f"(placed ~{pfrom}..{pto + 1}); {len(vics)} elim(s) in "
                                  f"~{now - (anchor - lead):.0f}s window: {vics[-4:]}  "
                                  f"[decr-coverage {cov}]")
                        self._pending_placements = _still

                    frame_bgra = frame.to_ndarray(format='bgra')
                    fh_cur, fw_cur = frame_bgra.shape[:2]

                    if SAVE_BOUNDARY_FRAMES and now - self._last_boundary_capture >= BOUNDARY_FRAME_INTERVAL_SECONDS:
                        self._last_boundary_capture = now
                        boundary_dir = BOUNDARY_FRAME_DIR / self.streamer
                        boundary_dir.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(
                            str(boundary_dir / f"{int(now)}.jpg"),
                            cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2BGR),
                        )

                    # Squads-left observed-placement tracking (bead hmz, log-only). Read the top-right
                    # 'N SQUADS LEFT' HUD ~every SQUADS_TRACK_INTERVAL_SECONDS and log transitions.
                    _since_squads = now - self._last_squads_read
                    _squads_due = _since_squads >= SQUADS_TRACK_INTERVAL_SECONDS          # slow baseline
                    _squads_evt = self._squads_read_asap and _since_squads >= 1.0         # after a kill, min 1s
                    _squads_lowres = fh_cur < SQUADS_MIN_FRAME_HEIGHT   # counter OCRs too poorly below ~900p
                    if SQUADS_TRACK_ENABLED and _squads_lowres and not self._squads_lowres_logged:
                        print(f"[Squads:{self.streamer}] skipping squads reads — "
                              f"{fw_cur}x{fh_cur} below {SQUADS_MIN_FRAME_HEIGHT}p (HUD counter unreliable)")
                        self._squads_lowres_logged = True
                    if SQUADS_TRACK_ENABLED and not _squads_lowres and (_squads_due or _squads_evt):
                        self._last_squads_read = now
                        _evt_read = self._squads_read_asap
                        self._squads_read_asap = False
                        sq, pl, _raw = read_squads_left(
                            frame_bgra,
                            lambda reg: ocr_with_easyocr(preprocess_for_easyocr(reg)[0]),
                        )
                        if os.environ.get("SQUADS_DEBUG"):
                            print(f"[SqDbg:{self.streamer}] t={now:.1f} sq={sq} pl={pl} "
                                  f"cur={self._squads_left} pend={self._squads_pending} "
                                  f"evt={int(_evt_read)} raw={_raw!r}")
                        if sq is not None:
                            cur = self._squads_left
                            # Smoothing that survives event-triggered fast reads. squads-left is
                            # monotonically non-increasing within a match, so:
                            #   * DECREASE (sq < cur): accumulate consecutive below-cur, non-increasing
                            #     reads and commit once enough agree. A lone digit misread-down
                            #     (18->16) bounces back up next read (-> 18 == cur), resetting the
                            #     count, so it never commits; a real decline (10->9->8) keeps reading
                            #     lower and commits. GRADUATED confirmation: a small drop needs 2
                            #     reads, a large drop needs 3-4 -- this kills tens-digit misreads
                            #     (11->1, drop 10) without hard-rejecting a genuine big drop after a
                            #     menu/None gap (which would strand cur at a stale high value forever).
                            #   * INCREASE (sq > cur): only a LARGE jump (>=5) is a new match, confirmed
                            #     by two identical reads; small increases are OCR misreads, ignored.
                            committed = False
                            new_match = False
                            if cur is None:
                                committed = True
                            elif sq == cur:
                                self._squads_pending = None
                                self._squads_pending_n = 0
                            elif sq < cur:
                                if self._squads_pending is not None and sq <= self._squads_pending:
                                    self._squads_pending_n += 1
                                else:
                                    self._squads_pending_n = 1
                                    self._squads_pending_ts = now   # first sight of THIS drop ~= wipe time
                                self._squads_pending = sq       # track the latest (lowest) candidate
                                drop = cur - self._squads_pending
                                need = 2 if drop <= 4 else (3 if drop <= 7 else 4)
                                if self._squads_pending_n >= need:
                                    committed = True
                            else:  # sq > cur
                                if sq - cur >= 5:
                                    if self._squads_pending == sq:
                                        committed = True
                                        new_match = True
                                    else:
                                        self._squads_pending = sq
                                        self._squads_pending_n = 1
                                # small increase -> OCR misread, ignore
                            if committed:
                                print(f"[Squads:{self.streamer}] {cur} -> {sq} squads left"
                                      f"{f', {pl} players' if pl else ''}"
                                      f"{' (new match)' if new_match else ''}")
                                # Placement-correlation (bead hmz): a DECREASE means cur-sq squad(s)
                                # were wiped, placing ~cur..sq+1. Queue it; coverage is evaluated later
                                # (see the drain above) once the causing elims have flushed into
                                # _recent_elims. Anchor at when the drop was FIRST seen (~the wipe), not
                                # this (lagged) commit.
                                if cur is not None and sq < cur:
                                    anchor = self._squads_pending_ts or now
                                    self._pending_placements.append((anchor, cur - sq, cur, sq))
                                self._squads_left = sq
                                self._squads_pending = None
                                self._squads_pending_n = 0
                                self._squads_pending_ts = 0.0

                    # Re-detect killfeed position on this frame
                    new_regions = detect_killfeed_from_frame(
                        frame_bgra, fw_cur, fh_cur,
                        content_x0=self._content_x0,
                        content_x1=self._content_x1,
                        search_zone=self._search_zone,
                    )
                    if not new_regions:
                        # Killfeed region is blank — nothing to OCR this frame
                        _frames_skipped += 1
                    else:
                        killfeed_lines     = new_regions
                        max_width_upscaled = max(r["width"] for r in killfeed_lines) * 2
                        _frames_active    += 1

                    # Live auto-calibration: while this streamer has no zone yet, fire one Claude
                    # classification on the current candidate regions ~once a minute (only when
                    # regions are actually present). A single frame can mislead (a facecam on a
                    # sparse feed can be mislabeled killfeed), so a zone is only LOCKED once TWO
                    # independent attempts agree (zones_overlap); until then the streamer stays on
                    # the generic box. The ~2-3s vision call briefly stalls this worker's demux
                    # once a minute — a negligible, self-correcting buffer — and stops once locked.
                    if (self._calib_needed and new_regions
                            and now - self._last_calib_attempt >= AUTO_CALIBRATE_INTERVAL_SECONDS):
                        self._last_calib_attempt = now
                        self._calib_attempts += 1
                        zone = attempt_calibration_from_frame(
                            self.streamer, frame_bgra, new_regions,
                            self._content_x0, self._content_x1 or fw_cur, fh_cur,
                        )
                        if zone is not None:
                            if self._calib_candidate is None:
                                self._calib_candidate = zone
                                print(f"[{self.streamer}] Calibration candidate (awaiting a "
                                      f"second agreeing frame to confirm): {zone}")
                            elif zones_overlap(self._calib_candidate, zone):
                                self._search_zone = zone
                                self._calib_needed = False
                                commit_calibrated_zone(self.streamer, zone, fw_cur, fh_cur)
                                print(f"[{self.streamer}] Live-calibrated killfeed zone: {zone}")
                            else:
                                # Disagreement — the earlier candidate was likely a one-off
                                # mislabel; replace it and keep looking.
                                self._calib_candidate = zone
                                print(f"[{self.streamer}] Calibration candidate replaced "
                                      f"(previous did not reproduce): {zone}")
                        if self._calib_needed and self._calib_attempts >= AUTO_CALIBRATE_MAX_ATTEMPTS:
                            self._calib_needed = False
                            print(f"[{self.streamer}] Auto-calibration gave up after "
                                  f"{self._calib_attempts} attempts — staying on generic box.")

                    if new_regions:
                        is_ranked = (not RANKED_ONLY_CROPS) or is_ranked_game(
                            frame_bgra, fw_cur, fh_cur
                        )

                    for line_idx, line_crop in enumerate(killfeed_lines if new_regions else []):
                        l = line_crop["left"]
                        t = line_crop["top"]
                        w = line_crop["width"]
                        h = line_crop["height"]

                        if t + h > fh_cur or l + w > fw_cur:
                            continue

                        # Small vertical padding for descenders (g, p, y tails).
                        # Kept at 2px to avoid bleeding into adjacent killfeed lines.
                        _PAD = 2
                        t0 = max(0, t - _PAD)
                        t1 = min(fh_cur, t + h + _PAD)
                        # Horizontal padding: killfeed_lines here comes from a single-frame
                        # snapshot (detect_killfeed_from_frame), not the multi-frame average
                        # used at initial calibration. A trailing name/word can still be
                        # rendering in and not yet bright enough to clear the column-projection
                        # threshold when this exact frame was sampled, truncating the crop with
                        # no margin (confirmed: "Enemy Shield Broken -" captured with the legend
                        # name entirely missing, 2026-07-01). Pad both sides defensively.
                        _PAD_X = 25
                        l0 = max(0, l - _PAD_X)
                        l1 = min(fw_cur, l + w + _PAD_X)
                        img = frame_bgra[t0:t1, l0:l1]
                        if USE_EASYOCR:
                            processed, _, gun_positions = preprocess_for_easyocr(
                                img,
                                stretch_x=self._stretch_x,
                                stretch_y=self._stretch_y,
                            )
                        elif USE_TROCR:
                            processed, _, gun_positions = preprocess_for_trocr(
                                img,
                                stretch_x=self._stretch_x,
                                stretch_y=self._stretch_y,
                            )
                        else:
                            processed, _, gun_positions = preprocess(
                                img,
                                stretch_x=self._stretch_x,
                                stretch_y=self._stretch_y,
                            )

                        if isinstance(processed, list):
                            for i in range(len(processed)):
                                if processed[i].shape[1] < max_width_upscaled:
                                    pad = max_width_upscaled - processed[i].shape[1]
                                    processed[i] = cv2.copyMakeBorder(
                                        processed[i], 0, 0, 0, pad,
                                        cv2.BORDER_CONSTANT, value=255
                                    )
                            if is_empty_line(processed[0]):
                                continue
                        else:
                            if processed.shape[1] < max_width_upscaled:
                                pad = max_width_upscaled - processed.shape[1]
                                processed = cv2.copyMakeBorder(
                                    processed, 0, 0, 0, pad,
                                    cv2.BORDER_CONSTANT, value=255
                                )
                            if is_empty_line(processed):
                                continue

                        saving_img = processed[0] if isinstance(processed, list) else processed

                        # --- OCR inference: EasyOCR → TrOCR → Tesseract ---
                        if USE_EASYOCR:
                            # img = original color crop; enables red-skull knock/kill
                            # classification of icon gaps (see detect_kill_skull).
                            text = ocr_with_easyocr(saving_img, color_img=img)
                        elif USE_TROCR:
                            try:
                                from trocr_inference import ocr_with_trocr
                                text, trocr_conf = ocr_with_trocr(processed, gun_positions, TROCR_MODEL_PATH)
                                if trocr_conf < TROCR_CONF_THRESHOLD:
                                    if SAVE_NOISE_CROPS and is_ranked:
                                        self.noise_crop_saver.maybe_save(saving_img, line_idx, now)
                                    continue
                            except Exception as e:
                                print(f"[{self.streamer}] TrOCR error, falling back to Tesseract: {e}")
                                text = ocr_with_positions(saving_img, TESSERACT_CONFIG)
                        else:
                            text = ocr_with_positions(processed, TESSERACT_CONFIG)

                        if looks_like_noise(text):
                            if SAVE_NOISE_CROPS and is_ranked:
                                self.noise_crop_saver.maybe_save(saving_img, line_idx, now)
                            continue

                        # Non-Latin script detection (CJK, Cyrillic, etc.)
                        if NON_ENGLISH_DETECTION:
                            if _has_non_latin_script(text):
                                self._non_latin_streak += 1
                                if (self._non_latin_streak >= NON_ENGLISH_THRESHOLD
                                        and not self._non_english_warned):
                                    print(f"[{self.streamer}] WARNING: Non-Latin script "
                                          f"detected — suppressing crop saves for this channel")
                                    self._non_english_warned = True
                            else:
                                if (self._non_english_warned
                                        and self._non_latin_streak >= NON_ENGLISH_THRESHOLD):
                                    print(f"[{self.streamer}] Latin text resumed "
                                          f"— re-enabling crop saves")
                                self._non_latin_streak = 0
                                self._non_english_warned = False

                        crop_filename = ""
                        # Save crop only after OCR confirms killfeed content
                        if (SAVE_CROPS and is_ranked
                                and _has_killfeed_content(text)
                                and not (SKIP_NON_ENGLISH_CROPS and self._non_english_warned)):
                            res = self.crop_saver.maybe_save(saving_img, line_idx, now, raw_img=img)
                            if res:
                                crop_filename = res

                        canonical_text = find_recent_match(text, event_tracker, now)
                        event_tracker[canonical_text].append((now, text))
                        
                        existing_crop, existing_fn = event_crops.get(canonical_text, (None, ""))
                        event_crops[canonical_text] = (saving_img, crop_filename or existing_fn)

                    completed = flush_old_events(event_tracker, now, event_crops, self.streamer)
                    if completed:
                        self._write_events(completed)

                    if now - _last_stats_ts >= _STATS_INTERVAL:
                        total = _frames_active + _frames_skipped
                        pct   = int(100 * _frames_skipped / total) if total else 0
                        print(f"[{self.streamer}] Frame stats: "
                              f"{_frames_active} active, {_frames_skipped} skipped "
                              f"({pct}% blank)")
                        _frames_active = _frames_skipped = 0
                        _last_stats_ts = now

        except Exception as e:
            if not self._stop.is_set():
                print(f"[{self.streamer}] Stream error: {e}")
        finally:
            try:
                container.close()
            except Exception:
                pass
            for p in self._procs:
                p.terminate()
            print(f"[{self.streamer}] Worker stopped. "
                  f"Crops saved: {self.crop_saver.saved}, skipped: {self.crop_saver.skipped} | Noise crops saved: {self.noise_crop_saver.saved}")

    def _write_events(self, completed_events: list):
        import db_log
        for event_time, best_text, crop, crop_filename in completed_events:
            with self.db_lock:
                parsed = parse_killfeed_line(best_text, self.db, event_time)
            if not parsed['event_type']:
                continue

            # Async Gemini validation — enqueued non-blocking, processed in background
            if GEMINI_VALIDATE and crop is not None and parsed.get("event_type") == "Kill":
                try:
                    from gemini_queue import get_queue
                    get_queue().enqueue(crop, best_text, self.streamer, event_time)
                except Exception as e:
                    print(f"[{self.streamer}] Gemini enqueue error: {e}")

            # Buffer eliminations for placement correlation (bead hmz). A squad wipe -> squads-left
            # decrement is triggered by an elimination; keep a short rolling window to match against.
            if parsed["event_type"] in ("Kill", "BleedOut") and (parsed["victim"] or "").strip():
                self._recent_elims.append((event_time, parsed["victim"].strip()))
                self._squads_read_asap = True   # re-read squads-left soon (a decrement is likely)

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_time))
            print(
                f"[{self.streamer}] [{timestamp}] {parsed['canonical']}  "
                f"(event={parsed['event_type']}, atk={parsed['attacker']}, vic={parsed['victim']})"
            )
            try:
                db_log.insert_event(
                    streamer=self.streamer,
                    timestamp=timestamp,
                    raw_text=parsed["raw_line"],
                    canonical=parsed["canonical"],
                    event_type=parsed["event_type"],
                    attacker=parsed["attacker"] or "",
                    victim=parsed["victim"] or "",
                    attacker_conf=parsed.get("attacker_conf", 1.0),
                    victim_conf=parsed.get("victim_conf", 1.0),
                    source="easyocr" if USE_EASYOCR else ("trocr" if USE_TROCR else "tesseract"),
                    gemini_corrected=0,
                    crop_filename=crop_filename
                )
            except Exception as e:
                print(f"[{self.streamer}] DB write error: {e}")



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Apex killfeed OCR — watch any live Twitch stream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ocr.py --channel faide\n"
            "  python ocr.py --channel https://twitch.tv/faide\n"
            "  python ocr.py --channel faide,sang4tw,apryze\n"
            "  python ocr.py                    # uses TWITCH_CHANNELS from config.py"
        ),
    )
    parser.add_argument(
        "--channel",
        help="Twitch username(s) or URL(s), comma-separated. "
             "Accepts any Apex stream — no pre-configuration needed.",
    )
    parser.add_argument(
        "--top", type=int, default=None, metavar="N",
        help="Auto-watch top N Apex streams by viewer count "
             "(requires TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET env vars).",
    )
    args = parser.parse_args()

    # Resolve usernames
    if args.top:
        from twitch_api import get_top_apex_streams  # noqa: F401 (used in loop too)
        top_n_resolved = args.top
        print(f"Fetching top {top_n_resolved} Apex streams from Twitch API"
              f"{' (ranked titles only)' if RANKED_STREAMS_ONLY else ''}...")
        usernames = get_top_apex_streams(top_n_resolved, ranked_only=RANKED_STREAMS_ONLY)
        print(f"  -> {usernames}")
    elif args.channel:
        top_n_resolved = None
        usernames = [_parse_channel_arg(c) for c in args.channel.split(",") if c.strip()]
    else:
        # Default: auto-watch top streamers via Twitch API
        from twitch_api import get_top_apex_streams  # noqa: F401 (used in loop too)
        top_n_resolved = TOP_STREAMS_COUNT
        print(f"No channels specified — auto-watching top {top_n_resolved} Apex streams"
              f"{' (ranked titles only)' if RANKED_STREAMS_ONLY else ''}...")
        usernames = get_top_apex_streams(top_n_resolved, ranked_only=RANKED_STREAMS_ONLY)
        print(f"  -> {usernames}")

    if not usernames:
        print("No streams found. Check your Twitch API credentials in .env or pass --channel.")
        return

    # Refresh the Predator ground-truth list before pro seeding (age-gated: no-op unless
    # apex_ranked_leaderboard.csv is older than LEADERBOARD_MAX_AGE_HOURS). Opens a brief
    # headed Edge window when it actually scrapes (Cloudflare blocks headless). Any failure
    # falls back to the existing CSV.
    if LEADERBOARD_AUTO_REFRESH:
        import subprocess
        try:
            subprocess.run(
                [sys.executable, "update_leaderboard.py"],
                timeout=180, check=False,
            )
        except Exception as e:
            print(f"[main] Leaderboard refresh skipped ({e}) — using existing CSV.")

    # Shared resources
    db = PlayerDatabase()
    db.load_databases()
    db_lock = threading.Lock()

    # Initialise SQLite killfeed log (auto-imports legacy CSV if DB is new)
    import db_log
    from config import KILLFEED_DB_PATH
    db_log.init_db(KILLFEED_DB_PATH, csv_path=LOG_PATH)

    # Register player DB with Gemini queue so it can re-parse corrections
    if GEMINI_VALIDATE:
        try:
            from gemini_queue import get_queue
            get_queue().set_player_db(db, db_lock)
        except Exception as e:
            print(f"[main] Gemini queue init warning: {e}")

    # Spawn one worker per channel
    workers: list[ChannelWorker] = []
    for username in usernames:
        w = ChannelWorker(username, db, db_lock)
        workers.append(w)
        w.start()
        print(f"[{w.streamer}] Worker started -> twitch.tv/{username}")


    top_n = top_n_resolved  # None only when --channel is used explicitly
    _last_refresh = time.time()  # track last periodic top-stream refresh
    _last_liveness_check = time.time()  # track last periodic liveness sweep

    print(f"\nWatching {len(workers)} channel(s). Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(5)

            # --- Periodic top-stream refresh (--top mode only) ---
            if top_n and (time.time() - _last_refresh) >= STREAM_REFRESH_INTERVAL:
                _last_refresh = time.time()
                try:
                    fresh = get_top_apex_streams(top_n + 10, ranked_only=RANKED_STREAMS_ONLY)
                    active_names = {ww.username for ww in workers if ww.is_alive()}
                    # Desired set: top top_n from fresh list, minus blocklist
                    desired = [u for u in fresh if u not in STREAM_BLOCKLIST][:top_n]
                    desired_set = set(desired)
                    # Stop workers watching channels that fell out of top list
                    for i, w in enumerate(workers):
                        if w.is_alive() and w.username not in desired_set:
                            replacement = next(
                                (u for u in desired if u not in active_names and u != w.username),
                                None,
                            )
                            if replacement:
                                print(f"[refresh] {w.username} dropped from top {top_n} — "
                                      f"replacing with {replacement}")
                                w.stop()
                                active_names.discard(w.username)
                                new_w = ChannelWorker(replacement, db, db_lock)
                                workers[i] = new_w
                                new_w.start()
                                active_names.add(replacement)
                    print(f"[refresh] Periodic refresh done. Watching: "
                          f"{[ww.username for ww in workers if ww.is_alive()]}")
                except Exception as e:
                    print(f"[refresh] Twitch API error during periodic refresh: {e}")

            # --- Periodic liveness sweep (all modes) — a demux loop that never raises
            # StopIteration/an exception when the source stream truly ends will keep a
            # worker "alive" forever, silently re-decoding/OCR-ing stale buffered frames
            # (e.g. a frozen post-match screen) instead of dying and being restarted by
            # the block below. Cross-check against Twitch directly and force a restart.
            if (time.time() - _last_liveness_check) >= LIVENESS_CHECK_INTERVAL:
                _last_liveness_check = time.time()
                try:
                    from twitch_api import is_streams_live
                    live_workers = [w for w in workers if w.is_alive() and not w._stop.is_set()]
                    live_status = is_streams_live([w.username for w in live_workers])
                    for i, w in enumerate(workers):
                        if w in live_workers and not live_status.get(w.username, True):
                            print(f"[{w.streamer}] Stream confirmed offline via Twitch API — "
                                  f"stopping stale worker (was still decoding after stream end).")
                            w.stop()
                            new_w = ChannelWorker(w.username, db, db_lock)
                            workers[i] = new_w
                            new_w.start()
                except Exception as e:
                    print(f"[liveness] Twitch API error during liveness check: {e}")

            # --- Restart any dead workers (stream went offline, etc.) ---
            for i, w in enumerate(workers):
                if not w.is_alive() and not w._stop.is_set():
                    replacement = w.username  # default: retry same channel
                    if top_n:
                        # Re-query top streams and pick the highest-ranked one
                        # not already being watched
                        try:
                            active = {ww.username for ww in workers if ww.is_alive()}
                            fresh = get_top_apex_streams(top_n + 10, ranked_only=RANKED_STREAMS_ONLY)
                            replacement = next(
                                (u for u in fresh if u not in active),
                                w.username,
                            )
                        except Exception as e:
                            print(f"[rotation] Twitch API error: {e} — retrying {w.username}")

                    if replacement != w.username:
                        print(f"[{w.streamer}] Worker died — rotating to {replacement} in 30s...")
                    else:
                        print(f"[{w.streamer}] Worker died — restarting in 30s...")
                    time.sleep(30)
                    new_w = ChannelWorker(replacement, db, db_lock)
                    workers[i] = new_w
                    new_w.start()
    except KeyboardInterrupt:
        print("\nStopping all workers...")

    for w in workers:
        w.stop()
    for w in workers:
        w.join(timeout=10)

    db.save_player_database()
    db.save_legend_typo_database()
    print("Done.")


if __name__ == "__main__":
    main()
