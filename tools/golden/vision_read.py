"""Vision-model ground-truth read of the killfeed crops (NOT OCR). Sends WIDE killfeed crops to
Claude with tool-use structured output; Claude transcribes every kill/knock/bleed-out line it can
read. Aggregated + deduped, this is the golden ground-truth kill list to compare against the OCR
pipeline (catches kills OCR garbled or whose narrow zone clipped).

Usage:
  python _vision_read.py sample 8        # read 8 evenly-spaced crops, print (validation)
  python _vision_read.py run [batch]     # read ALL crops (batch imgs/call, default 6) -> jsonl
"""
import sys, os, io, base64, glob, json, time
import cv2
from PIL import Image

import config  # loads .env -> ANTHROPIC_API_KEY
import anthropic

MODEL = "claude-opus-4-8"
SP = os.environ.get("GOLDEN_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CROPS = os.path.join(SP, "kf_crops")
OUTF = os.path.join(SP, "vision_reads.jsonl")

_TOOL = {
    "name": "report_killfeed",
    "description": "Report every kill-feed line visible in each numbered killfeed image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "images": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer", "description": "1-based index of the image."},
                        "lines": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "attacker": {"type": "string", "description": "Left name (incl. [CLAN] tag if shown), '' if unreadable/absent."},
                                    "victim": {"type": "string", "description": "Right name (incl. [CLAN] tag if shown)."},
                                    "kind": {"type": "string", "enum": ["kill", "knock", "bleedout", "other"],
                                             "description": "kill=weapon icon WITH a small red skull; knock=weapon icon only, no skull; bleedout='[Bleed Out]' text between names; other=shield-broken/ping/non-elimination."},
                                },
                                "required": ["attacker", "victim", "kind"],
                            },
                        },
                    },
                    "required": ["image_index", "lines"],
                },
            },
        },
        "required": ["images"],
    },
}

_PROMPT = (
    "Each image is a crop of the Apex Legends kill feed (top-right of the screen), in time order.\n"
    "Transcribe EVERY feed line you can read in each image. A feed line is 'ATTACKER <icon> VICTIM'.\n"
    "Classify each line's kind:\n"
    "- kill: the weapon icon has a small RED SKULL next to it (a confirmed elimination).\n"
    "- knock: a weapon icon only, NO red skull (a knockdown).\n"
    "- bleedout: the words '[Bleed Out]' appear between the two names (a knocked player bled out).\n"
    "- other: 'Enemy Shield Broken', pings ('spotted', 'pinged'), 'X is the new Kill Leader', join banners, etc.\n"
    "Read names EXACTLY as shown, including any [CLAN] tag in brackets. Players often have numbers or "
    "unusual names (e.g. 'Revenant8223', 'I AM HERE', '225 Bench Press Manifestation'). If a name is "
    "partly cut off or unreadable, transcribe what you can and leave attacker '' if truly illegible.\n"
    "Ignore the 'N SQUADS LEFT' counter, the minimap, and the kill-leader badge icon.\n"
    "Report via the report_killfeed tool, one entry per image (in order)."
)

def client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def b64(path):
    img = cv2.imread(path)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    buf = io.BytesIO(); Image.fromarray(rgb).save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()

def read_batch(cl, paths):
    content = [{"type": "text", "text": _PROMPT}]
    for i, p in enumerate(paths, 1):
        content.append({"type": "text", "text": f"Image {i} (t={os.path.basename(p)[:-4]}s):"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(p)}})
    resp = cl.messages.create(
        model=MODEL, max_tokens=4096,
        tools=[_TOOL], tool_choice={"type": "tool", "name": "report_killfeed"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input.get("images", [])
    return []

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "sample"
    crops = sorted(glob.glob(os.path.join(CROPS, "*.png")))
    if not crops:
        print("no crops found"); return
    cl = client()
    if mode == "sample":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        step = max(1, len(crops) // n)
        picks = crops[::step][:n]
        res = read_batch(cl, picks)
        for r, p in zip(res, picks):
            print(f"\n--- {os.path.basename(p)} ---")
            for ln in r.get("lines", []):
                print(f"   [{ln['kind']:8}] {ln.get('attacker','')!r:24} -> {ln.get('victim','')!r}")
        return
    # run: all crops, batched
    batch = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    out = open(OUTF, "w", encoding="utf-8")
    done = 0; t0 = time.time()
    for i in range(0, len(crops), batch):
        chunk = crops[i:i+batch]
        try:
            res = read_batch(cl, chunk)
        except Exception as e:
            print(f"  batch {i} ERROR {type(e).__name__}: {e}", flush=True); time.sleep(3); continue
        for r, p in zip(res, chunk):
            t = int(os.path.basename(p)[:-4])
            for ln in r.get("lines", []):
                out.write(json.dumps({"t": t, "kind": ln["kind"], "attacker": ln.get("attacker", ""),
                                      "victim": ln.get("victim", "")}) + "\n")
        out.flush(); done += len(chunk)
        if done % 60 < batch:
            print(f"  {done}/{len(crops)} crops, {time.time()-t0:.0f}s", flush=True)
    out.close()
    print(f"DONE {done} crops -> {OUTF}", flush=True)

if __name__ == "__main__":
    main()
