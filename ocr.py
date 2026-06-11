"""Apex Legends killfeed OCR — multi-channel Twitch stream capture."""

import argparse
import csv
import re
import sys
import threading
import time
from collections import defaultdict, Counter
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
from crop_saver import CropSaver
from database import PlayerDatabase
from detect_killfeed import detect_for_stream, detect_killfeed_from_frame, open_stream, _get_frame_dimensions
from detect_ranked import is_ranked_game
from parsers import parse_killfeed_line


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def preprocess(img):
    """Invert colors, remove gun icons, upscale 2x.

    Returns: (processed_image, icons_removed, gun_icon_positions)
    """
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    _, inverted = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)

    _, dark_mask = cv2.threshold(inverted, 50, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    gun_icon_positions = []
    icons_removed = 0

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        aspect_ratio = w / h if h > 0 else 0
        if 2.0 < aspect_ratio < 6.0 and 15 < h < 40 and 40 < w < 100:
            cv2.rectangle(inverted, (x, y), (x + w, y + h), (255, 255, 255), -1)
            gun_icon_positions.append(x + w // 2)
            icons_removed += 1

    upscaled = cv2.resize(inverted, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gun_icon_positions = [pos * 2 for pos in gun_icon_positions]

    return upscaled, icons_removed, gun_icon_positions


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

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

def find_recent_match(text: str, event_tracker: dict, now: float, threshold: float = 0.75) -> str:
    best_match = None
    best_ratio = 0
    for canonical_text, variants in event_tracker.items():
        if not variants:
            continue
        last_seen = max(v[0] for v in variants)
        if now - last_seen > EVENT_WINDOW:
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
        valid_chars = [c for c in chars if c.isalnum() or c in ' .,!?-']
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
        cleaned = re.sub(r'^[^a-z0-9\s]+', '', text)
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


def flush_old_events(event_tracker, now, event_crops=None):
    to_write = []
    to_delete = []
    for canonical_text, variants in event_tracker.items():
        if not variants:
            continue
        last_seen = max(v[0] for v in variants)
        if now - last_seen > EVENT_WINDOW:
            best_text = pick_best_variant(variants)
            if best_text:
                crop = event_crops.pop(canonical_text, None) if event_crops is not None else None
                to_write.append((last_seen, best_text, crop))
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
    """Watches one Twitch channel: auto-detects killfeed, then OCRs and writes CSV."""

    def __init__(self, twitch_username: str,
                 csv_lock: threading.Lock, db: PlayerDatabase, db_lock: threading.Lock):
        display = _display_name(twitch_username)
        super().__init__(daemon=True, name=f"worker-{display}")
        self.username = twitch_username
        self.streamer = display          # used for logging / CSV
        self.csv_lock = csv_lock
        self.db = db
        self.db_lock = db_lock
        self._stop = threading.Event()
        self.crop_saver = CropSaver(display)
        self.noise_crop_saver = CropSaver(display, base_dir=NOISE_CROP_OUTPUT_DIR)
        self._procs: list = []           # [sl_proc, ff_proc]
        self._last_haiku: float = 0.0    # per-worker Haiku cooldown tracker
        self._non_latin_streak: int = 0
        self._non_english_warned: bool = False

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
            regions, n_read = detect_for_stream(container, fw, fh)
            if len(regions) >= DETECT_MIN_LINES:
                coords = [(r['left'], r['top'], r['width'], r['height']) for r in regions]
                print(f"[{self.streamer}] Detected {len(regions)} killfeed lines "
                      f"(attempt {attempt}, {n_read} frames): {coords}")
                return regions

            if RANKED_STREAMS_ONLY:
                if self._sample_is_ranked(container):
                    not_ranked_streak = 0
                    print(f"[{self.streamer}] Detection attempt {attempt}: "
                          f"only {len(regions)} line(s) found — ranked game active, retrying...")
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

        print(f"[{self.streamer}] Stream opened. Detecting killfeed regions...")

        # Get frame dimensions from first frame
        fw, fh = _get_frame_dimensions(container)

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

                    frame_bgra = frame.to_ndarray(format='bgra')
                    fh_cur, fw_cur = frame_bgra.shape[:2]

                    # Re-detect killfeed position on this frame
                    new_regions = detect_killfeed_from_frame(frame_bgra, fw_cur, fh_cur)
                    if not new_regions:
                        # Killfeed region is blank — nothing to OCR this frame
                        _frames_skipped += 1
                    else:
                        killfeed_lines     = new_regions
                        max_width_upscaled = max(r["width"] for r in killfeed_lines) * 2
                        _frames_active    += 1

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
                        img = frame_bgra[t0:t1, l:l + w]
                        processed, _, gun_positions = preprocess(img)

                        if processed.shape[1] < max_width_upscaled:
                            pad = max_width_upscaled - processed.shape[1]
                            processed = cv2.copyMakeBorder(
                                processed, 0, 0, 0, pad,
                                cv2.BORDER_CONSTANT, value=255
                            )

                        if is_empty_line(processed):
                            continue

                        if USE_TROCR:
                            try:
                                from trocr_inference import ocr_with_trocr
                                text, trocr_conf = ocr_with_trocr(processed, gun_positions, TROCR_MODEL_PATH)
                                if trocr_conf < TROCR_CONF_THRESHOLD:
                                    if SAVE_NOISE_CROPS and is_ranked:
                                        self.noise_crop_saver.maybe_save(processed, line_idx, now)
                                    continue
                            except Exception as e:
                                print(f"[{self.streamer}] TrOCR error, falling back to Tesseract: {e}")
                                text = ocr_with_positions(processed, TESSERACT_CONFIG)
                        else:
                            text = ocr_with_positions(processed, TESSERACT_CONFIG)

                        if looks_like_noise(text):
                            if SAVE_NOISE_CROPS and is_ranked:
                                self.noise_crop_saver.maybe_save(processed, line_idx, now)
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

                        # Save crop only after OCR confirms killfeed content
                        if (SAVE_CROPS and is_ranked
                                and _has_killfeed_content(text)
                                and not (SKIP_NON_ENGLISH_CROPS and self._non_english_warned)):
                            self.crop_saver.maybe_save(processed, line_idx, now)

                        canonical_text = find_recent_match(text, event_tracker, now)
                        event_tracker[canonical_text].append((now, text))
                        event_crops[canonical_text] = processed

                    completed = flush_old_events(event_tracker, now, event_crops)
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
        with self.csv_lock:
            with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for event_time, best_text, crop in completed_events:
                    with self.db_lock:
                        parsed = parse_killfeed_line(best_text, self.db, event_time)
                    if not parsed['event_type']:
                        continue

                    # Haiku validation for low-confidence Kill events
                    if (HAIKU_VALIDATE
                            and crop is not None
                            and parsed.get("event_type") == "Kill"
                            and time.time() - self._last_haiku >= 5.0
                            and (parsed.get("victim_conf", 1.0) < HAIKU_CONF_THRESHOLD
                                 or parsed.get("attacker_conf", 1.0) < HAIKU_CONF_THRESHOLD)):
                        try:
                            from haiku_validator import validate_killfeed_crop, save_training_sample
                            haiku_text = validate_killfeed_crop(crop)
                            self._last_haiku = time.time()
                            if haiku_text:
                                save_training_sample(crop, haiku_text, self.streamer)
                                with self.db_lock:
                                    haiku_parsed = parse_killfeed_line(haiku_text, self.db, event_time)
                                if (haiku_parsed.get("event_type") == "Kill"
                                        and haiku_parsed.get("victim_conf", 0.0)
                                        >= parsed.get("victim_conf", 0.0)):
                                    print(f"[{self.streamer}] Haiku corrected: "
                                          f"{best_text!r} -> {haiku_text!r}")
                                    parsed = haiku_parsed
                        except Exception as e:
                            print(f"[{self.streamer}] Haiku validation error: {e}")

                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_time))
                    print(
                        f"[{self.streamer}] [{timestamp}] {parsed['canonical']}  "
                        f"(event={parsed['event_type']}, atk={parsed['attacker']}, vic={parsed['victim']})"
                    )
                    writer.writerow([
                        self.streamer, timestamp,
                        parsed["raw_line"], parsed["canonical"],
                        parsed["event_type"], parsed["attacker"], parsed["victim"],
                        parsed.get("attacker_conf", 0.0), parsed.get("victim_conf", 0.0),
                    ])


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

    # Shared resources
    db = PlayerDatabase()
    db.load_databases()
    csv_lock = threading.Lock()
    db_lock = threading.Lock()

    # Ensure CSV header
    if not LOG_PATH.exists():
        with LOG_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "streamer", "timestamp", "raw_text", "canonical",
                "event_type", "attacker", "victim",
                "attacker_conf", "victim_conf",
            ])

    # Spawn one worker per channel
    workers: list[ChannelWorker] = []
    for username in usernames:
        w = ChannelWorker(username, csv_lock, db, db_lock)
        workers.append(w)
        w.start()
        print(f"[{w.streamer}] Worker started -> twitch.tv/{username}")

    top_n = top_n_resolved  # None only when --channel is used explicitly
    _last_refresh = time.time()  # track last periodic top-stream refresh

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
                                new_w = ChannelWorker(replacement, csv_lock, db, db_lock)
                                workers[i] = new_w
                                new_w.start()
                                active_names.add(replacement)
                    print(f"[refresh] Periodic refresh done. Watching: "
                          f"{[ww.username for ww in workers if ww.is_alive()]}")
                except Exception as e:
                    print(f"[refresh] Twitch API error during periodic refresh: {e}")

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
                    new_w = ChannelWorker(replacement, csv_lock, db, db_lock)
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
