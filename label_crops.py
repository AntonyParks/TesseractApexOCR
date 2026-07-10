"""Send killfeed crops to Claude Batch API for labeling, then collect and quality-filter results.

Usage:
    python label_crops.py submit                   # Submit batch job(s), print batch_id(s)
    python label_crops.py poll <batch_id>          # Check job status
    python label_crops.py collect <batch_id>       # Download results → labels_clean.csv
    python label_crops.py run                      # submit + poll until done + collect
"""

import argparse
import base64
import csv
import json
import random
import sys
import time
import os
from pathlib import Path

import anthropic

# Auto-load .env
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

LABELS_DIR = Path("labels")
BATCH_INPUT_PATH = LABELS_DIR / "batch_input.jsonl"
BATCH_RESULTS_PATH = LABELS_DIR / "batch_results.jsonl"
BATCH_IDS_FILE = LABELS_DIR / "batch_ids.txt"
ID_TO_PATH_FILE = LABELS_DIR / "id_to_path.json"
LABELS_CLEAN_CSV = LABELS_DIR / "labels_clean.csv"

CROPS_ROOT = Path("crops")
NOISE_CROPS_ROOT = Path("crops_noise")
MODEL = "claude-haiku-4-5-20251001"

# Claude sometimes uses typographic punctuation (en/em dash, curly quotes) that isn't in the
# EasyOCR training charset (see KNOWN_ISSUES.md) -- dataset.py silently strips anything outside
# the charset at training time, so normalize to the ASCII equivalents actually in the charset
# here, at label-extraction time, rather than losing that content later.
_CHAR_NORMALIZE = str.maketrans({
    "–": "-", "—": "-",           # en dash, em dash
    "‘": "'", "’": "'",           # curly single quotes
    "“": '"', "”": '"',           # curly double quotes
})
MAX_TOKENS = 128
CHUNK_SIZE = 5_000   # Max requests per API batch (well under the 256 MB limit)

PROMPT = (
    "Transcribe EXACTLY what text you can read from this Apex killfeed strip.\n"
    "- Where a gun/weapon icon gap appears, output: <GUN_ICON>\n"
    "- If the image is blank/noise/unreadable, output exactly: EMPTY\n"
    "- Output one line only. No explanation."
)

# Quality filter constants
MIN_CHARS = 4
MIN_ALPHA_RATIO = 0.5
EXPLANATION_TOKENS = {"because", "sorry", "i ", "the image", "this image", "i can", "i see", "note:", "please"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_image(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def _quality_tier(label: str) -> str | None:
    """Return 'high', 'medium', 'low', 'noise', or None (reject)."""
    text = label.strip()

    if text == "EMPTY":
        return "noise"
    if not text:
        return None
    if "\n" in text:
        return None
    if len(text) < MIN_CHARS:
        return None

    lower = text.lower()
    for tok in EXPLANATION_TOKENS:
        if tok in lower:
            return None

    alpha = sum(c.isalnum() or c == " " for c in text) / len(text)
    if alpha < MIN_ALPHA_RATIO:
        return None

    if "<GUN_ICON>" in text:
        return "high"
    if len(text) >= 12:
        return "medium"
    return "low"


EVAL_HOLDOUT_CSV = LABELS_DIR / "eval_holdout.csv"


def _eval_holdout_filenames() -> set:
    """Filenames hand-labeled as the held-out eval set -- must never enter training labels."""
    if not EVAL_HOLDOUT_CSV.exists():
        return set()
    with EVAL_HOLDOUT_CSV.open(encoding="utf-8") as f:
        return {row["filename"] for row in csv.DictReader(f)}


def _collect_crop_paths(streamers: set | None = None, noise_limit: int | None = None) -> list[Path]:
    """Return PNG crop paths under crops/ and crops_noise/, ready for labeling.

    Excludes *_raw.png companions: those are unprocessed color crops saved only
    for human viewing (crop_saver.py), never what EasyOCR actually sees at
    inference (preprocess_for_easyocr's inverted/upscaled/stretched grayscale
    output). Labeling or training on them would waste budget and introduce a
    train/inference mismatch.

    Always excludes the hand-labeled eval_holdout.csv filenames -- that set exists
    specifically to measure training results on data the model never trained on;
    silently mixing it back into labels_clean.csv would make it useless for that.

    Args:
        streamers: if given, only include crops from these streamer subdirectories
            (matches crops/<streamer>/... and crops_noise/<streamer>/...).
        noise_limit: if given, cap how many crops_noise/ files are included (randomly
            sampled, seeded for reproducibility) -- most noise crops are near-duplicate
            blank/terrain frames with little marginal training value.
    """
    eval_names = _eval_holdout_filenames()

    def _keep(p: Path) -> bool:
        if p.name in eval_names:
            return False
        if streamers is not None and p.parent.name not in streamers:
            return False
        return True

    positive = [p for p in CROPS_ROOT.rglob("*.png")
                if not p.name.endswith("_raw.png") and _keep(p)]

    noise = []
    if NOISE_CROPS_ROOT.exists():
        noise = [p for p in NOISE_CROPS_ROOT.rglob("*.png") if _keep(p)]
        if noise_limit is not None and len(noise) > noise_limit:
            random.seed(20260702)
            noise = random.sample(noise, noise_limit)

    return sorted(positive + noise)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_submit(args) -> list[str]:
    """Build requests and submit to Anthropic Batch API (auto-chunks into <=5k batches)."""
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    streamers = None
    if getattr(args, "streamers", None):
        streamers = {s.strip() for s in args.streamers.split(",") if s.strip()}
    noise_limit = getattr(args, "noise_limit", None)

    crop_paths = _collect_crop_paths(streamers=streamers, noise_limit=noise_limit)
    if not crop_paths:
        print(f"No crops found under {CROPS_ROOT}. Run ocr.py with SAVE_CROPS=True first.")
        sys.exit(1)

    # Filter out blank crops (pHash all-zeros or all-ones = uniform image)
    crop_paths = [p for p in crop_paths if '_0000.' not in p.name and '_ffff.' not in p.name]

    # Skip already-labeled crops
    if LABELS_CLEAN_CSV.exists():
        already_labeled = set()
        with LABELS_CLEAN_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                already_labeled.add(row["filename"])
        before = len(crop_paths)
        crop_paths = [p for p in crop_paths if p.name not in already_labeled]
        print(f"Skipping {before - len(crop_paths)} already-labeled crops.")

    if not crop_paths:
        print("All crops are already labeled.")
        return []

    limit = getattr(args, "limit", None)
    best  = getattr(args, "best",  False)

    if best:
        # Sort by file size descending: larger PNG = more complex image = more text
        print("Ranking crops by content (file size)...")
        crop_paths.sort(key=lambda p: p.stat().st_size, reverse=True)
        if limit and limit < len(crop_paths):
            crop_paths = crop_paths[:limit]
    elif limit and limit < len(crop_paths):
        crop_paths = random.sample(crop_paths, limit)

    print(f"Found {len(crop_paths)} crops. Building requests...")

    id_to_path: dict[str, str] = {}
    all_requests = []

    for i, path in enumerate(crop_paths):
        custom_id = f"crop_{i:06d}"
        id_to_path[custom_id] = str(path)
        image_data = _encode_image(path)
        all_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_data,
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
            },
        })

    # Save id→path mapping (covers all chunks)
    ID_TO_PATH_FILE.write_text(json.dumps(id_to_path, indent=2), encoding="utf-8")

    # Submit in chunks
    client = anthropic.Anthropic()
    batch_ids: list[str] = []
    chunks = [all_requests[i:i + CHUNK_SIZE] for i in range(0, len(all_requests), CHUNK_SIZE)]

    print(f"Submitting {len(all_requests)} requests in {len(chunks)} batch(es)...")
    for idx, chunk in enumerate(chunks):
        batch = client.messages.batches.create(requests=chunk)
        batch_ids.append(batch.id)
        # Save after every batch so IDs aren't lost if a later submission fails
        BATCH_IDS_FILE.write_text("\n".join(batch_ids), encoding="utf-8")
        print(f"  Batch {idx+1}/{len(chunks)}: {batch.id} — {len(chunk)} requests | {batch.processing_status}")
    return batch_ids


def cmd_poll(batch_id: str) -> str:
    """Poll batch status and print it."""
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    print(
        f"Batch {batch_id}: {batch.processing_status} | "
        f"succeeded={counts.succeeded} errored={counts.errored} "
        f"processing={counts.processing} canceled={counts.canceled}"
    )
    return batch.processing_status


def cmd_collect(batch_id: str, append: bool = False) -> tuple[int, int]:
    """Download results and write (or append to) labels_clean.csv.

    Returns (total_results, kept_results).
    """
    client = anthropic.Anthropic()
    id_to_path: dict[str, str] = json.loads(ID_TO_PATH_FILE.read_text(encoding="utf-8"))

    rows = []
    raw_lines = []

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        raw_lines.append(json.dumps({"custom_id": custom_id, "result": result.model_dump()}))

        if result.result.type != "succeeded":
            continue

        label = result.result.message.content[0].text.strip().translate(_CHAR_NORMALIZE)
        tier = _quality_tier(label)
        if tier is None:
            continue

        path = Path(id_to_path.get(custom_id, ""))
        streamer = path.parent.name if path.parent not in (CROPS_ROOT, NOISE_CROPS_ROOT) else "unknown"
        rows.append({
            "filename": path.name,
            "streamer": streamer,
            "filepath": str(path),
            "label": "" if tier == "noise" else label,
            "quality": tier,
        })

    # Append raw results
    with BATCH_RESULTS_PATH.open("a" if append else "w", encoding="utf-8") as f:
        f.write("\n".join(raw_lines))
        if raw_lines:
            f.write("\n")

    # Write / append clean CSV
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with LABELS_CLEAN_CSV.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "streamer", "filepath", "label", "quality"])
        if not append:
            writer.writeheader()
        writer.writerows(rows)

    return len(raw_lines), len(rows)


def cmd_extract_noise(args) -> None:
    """Scan existing batch_results.jsonl for EMPTY-labeled crops and append them to labels_clean.csv.

    Safe to run multiple times — skips filepaths already present in the CSV.
    """
    if not BATCH_RESULTS_PATH.exists():
        print(f"No batch results found at {BATCH_RESULTS_PATH}")
        return
    if not ID_TO_PATH_FILE.exists():
        print(f"No id_to_path mapping found at {ID_TO_PATH_FILE}")
        return

    id_to_path: dict[str, str] = json.loads(ID_TO_PATH_FILE.read_text(encoding="utf-8"))

    # Build set of filepaths already in the CSV to avoid duplicates
    existing_paths: set[str] = set()
    if LABELS_CLEAN_CSV.exists():
        for row in csv.DictReader(LABELS_CLEAN_CSV.open(encoding="utf-8")):
            existing_paths.add(row["filepath"])

    rows = []
    for line in BATCH_RESULTS_PATH.open(encoding="utf-8"):
        if not line.strip():
            continue
        obj = json.loads(line)
        # Handle both flat and doubly-nested result structures
        inner = obj.get("result", {})
        if "result" in inner:
            inner = inner["result"]

        if inner.get("type") != "succeeded":
            continue

        text = inner["message"]["content"][0]["text"].strip()
        if text != "EMPTY":
            continue

        custom_id = obj["custom_id"]
        path = Path(id_to_path.get(custom_id, ""))
        if not path.is_file() or str(path) in existing_paths:
            continue

        streamer = path.parent.name if path.parent != CROPS_ROOT else "unknown"
        rows.append({
            "filename": path.name,
            "streamer": streamer,
            "filepath": str(path),
            "label": "",
            "quality": "noise",
        })

    if not rows:
        print("No new noise crops to add.")
        return

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not LABELS_CLEAN_CSV.exists()
    with LABELS_CLEAN_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "streamer", "filepath", "label", "quality"])
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"Added {len(rows)} noise crops to {LABELS_CLEAN_CSV}")


def cmd_run(args) -> None:
    """One-shot: submit all chunks, poll until all done, collect."""
    batch_ids = cmd_submit(args)
    n_batches = len(batch_ids)

    print(f"\nPolling {n_batches} batch(es) until complete (checking every 30s)...")
    pending = set(batch_ids)
    while pending:
        for bid in list(pending):
            status = cmd_poll(bid)
            if status == "ended":
                pending.discard(bid)
        if pending:
            time.sleep(30)

    print("\nAll batches complete. Collecting results...")
    total_results = 0
    total_kept = 0
    for i, bid in enumerate(batch_ids):
        # Always append: labels_clean.csv also accumulates rows from the live Gemini
        # validation queue (ocr.py), so an unconditional overwrite on the first batch
        # would silently destroy that history. Only `collect` invoked standalone
        # without --append still defaults to overwrite, for explicit one-off resets.
        n_total, n_kept = cmd_collect(bid, append=True)
        total_results += n_total
        total_kept += n_kept
        print(f"  Batch {i+1}/{n_batches} ({bid}): {n_total} results, {n_kept} kept")

    tier_counts: dict[str, int] = {}
    if LABELS_CLEAN_CSV.exists():
        for row in csv.DictReader(LABELS_CLEAN_CSV.open(encoding="utf-8")):
            tier_counts[row["quality"]] = tier_counts.get(row["quality"], 0) + 1

    print(f"\nTotal: {total_results} results -> kept {total_kept} ({total_kept/max(total_results,1)*100:.1f}%)")
    print(f"  Quality tiers: {tier_counts}")
    print(f"  Written to: {LABELS_CLEAN_CSV}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Label killfeed crops via Anthropic Batch API")
    sub = parser.add_subparsers(dest="command")

    p_submit = sub.add_parser("submit", help="Submit batch job(s)")
    p_submit.add_argument("--limit", type=int, default=None, help="Max crops to submit")
    p_submit.add_argument("--best", action="store_true",
                          help="Select highest-content crops (by file size) instead of random")
    p_submit.add_argument("--streamers", type=str, default=None,
                          help="Comma-separated streamer allowlist (e.g. Nati,Matafe_,Mande)")
    p_submit.add_argument("--noise-limit", type=int, default=None,
                          help="Cap how many crops_noise/ files are included (random sample)")

    p_poll = sub.add_parser("poll", help="Poll batch status")
    p_poll.add_argument("batch_id")

    p_collect = sub.add_parser("collect", help="Collect results -> labels_clean.csv")
    p_collect.add_argument("batch_id")
    p_collect.add_argument("--append", action="store_true", help="Append to existing labels_clean.csv instead of overwriting")

    p_run = sub.add_parser("run", help="submit + poll + collect (one-shot)")
    p_run.add_argument("--limit", type=int, default=None, help="Max crops to submit")
    p_run.add_argument("--best", action="store_true",
                       help="Select highest-content crops (by file size) instead of random")
    p_run.add_argument("--streamers", type=str, default=None,
                       help="Comma-separated streamer allowlist (e.g. Nati,Matafe_,Mande)")
    p_run.add_argument("--noise-limit", type=int, default=None,
                       help="Cap how many crops_noise/ files are included (random sample)")

    sub.add_parser("extract_noise", help="Append EMPTY-labeled crops from batch_results.jsonl to labels_clean.csv")

    args = parser.parse_args()

    if args.command == "submit":
        cmd_submit(args)
    elif args.command == "poll":
        cmd_poll(args.batch_id)
    elif args.command == "collect":
        cmd_collect(args.batch_id, append=args.append)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "extract_noise":
        cmd_extract_noise(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
