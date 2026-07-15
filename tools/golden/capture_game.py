"""SLOW PASS (run once): OCR a continuous VOD read, saving every killfeed read durably
(video-PTS + region box + color crop path + raw OCR text) with NO parsing, plus a squads-left
timeline to locate the game inside the read. The fast parse/collapse pass (_vod_parse.py) then
iterates over reads.jsonl in seconds without re-OCR.

Reuses the real per-frame path (detect_killfeed_from_frame -> preprocess_for_easyocr ->
ocr_with_easyocr with color for skull). Video-PTS gated (offline-correct sampling).

Usage: python _vod_capture.py <vod_url> <offset> <duration_sec>
"""
import sys, os, subprocess, json, time
import av, cv2

import config
from detect_killfeed import (_NonSeekablePipe, _streamlink_bin, _get_frame_dimensions,
                             detect_content_x_bounds, detect_killfeed_from_frame)
from ocr import preprocess_for_easyocr, ocr_with_easyocr, is_empty_line, looks_like_noise
from detect_squads import read_squads_left

URL = sys.argv[1]; OFFSET = sys.argv[2]
DUR = float(sys.argv[3]) if len(sys.argv) > 3 else 1900.0
SAMPLE_DT = 0.5      # killfeed OCR cadence (video-sec)
SQUADS_DT = 8.0      # squads read cadence (video-sec)
FRAME_DT  = 10.0     # downscaled full-frame save cadence (video-sec)

SP = os.environ.get("GOLDEN_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(SP, "vod_capture")
CROPS = os.path.join(OUT, "crops"); FRAMES = os.path.join(OUT, "frames")
os.makedirs(CROPS, exist_ok=True); os.makedirs(FRAMES, exist_ok=True)
READS = open(os.path.join(OUT, "reads.jsonl"), "w", encoding="utf-8")
SQUADS = open(os.path.join(OUT, "squads.jsonl"), "w", encoding="utf-8")

def _sq_ocr(reg):
    return ocr_with_easyocr(preprocess_for_easyocr(reg)[0], color_img=reg)

def open_vod(url, offset, duration):
    sl = subprocess.Popen([_streamlink_bin(), "--stdout", "--hls-start-offset", offset,
                           "--hls-duration", str(int(duration) + 15), url, config.STREAM_QUALITY],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.Popen(["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1",
                           "-loglevel", "error"], stdin=sl.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    return av.open(_NonSeekablePipe(ff.stdout), format="mpegts"), [sl, ff]

def main():
    print(f"CAPTURE offset={OFFSET} dur={DUR}s -> {OUT}", flush=True)
    container, _ = open_vod(URL, OFFSET, DUR)
    if not container.streams.video:
        print("NO VIDEO"); return
    fw, fh = _get_frame_dimensions(container)
    zone = config.STREAMER_SEARCH_ZONES.get("Sang")
    print(f"dims {fw}x{fh} zone={zone}", flush=True)
    cx0, cx1 = 0, fw; sx, sy = 1.0, fh / 1080.0
    first = True; t0 = None
    nxt_s = nxt_q = nxt_f = 0.0
    n_read = 0; n_kf_frames = 0; started = time.time()
    for packet in container.demux(video=0):
        try: frames = packet.decode()
        except Exception: continue
        for frame in frames:
            if frame.pts is None: continue
            pts = float(frame.pts * frame.time_base)
            if t0 is None: t0 = pts
            rel = pts - t0
            if rel > DUR:
                _finish(n_read, n_kf_frames, rel, started); return
            if first:
                arr0 = frame.to_ndarray(format="bgra")
                cx0, cx1 = detect_content_x_bounds(arr0)
                cw = (cx1 or fw) - cx0; sx = fw / cw if cw > 0 else 1.0
                print(f"content x {cx0}-{cx1} sx={sx:.3f} sy={sy:.3f}", flush=True)
                first = False
            do_kf = rel >= nxt_s
            do_sq = rel >= nxt_q
            do_fr = rel >= nxt_f
            if not (do_kf or do_sq or do_fr):
                continue
            arr = frame.to_ndarray(format="bgra")
            if do_fr:
                nxt_f = rel + FRAME_DT
                cv2.imwrite(os.path.join(FRAMES, f"{rel:07.1f}.jpg"),
                            cv2.resize(cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR), (854, 480)))
            if do_sq:
                nxt_q = rel + SQUADS_DT
                try: sq, pl, raw = read_squads_left(arr, _sq_ocr)
                except Exception: sq, pl, raw = None, None, ""
                SQUADS.write(json.dumps({"t": round(rel,2), "squads": sq, "players": pl, "raw": raw}) + "\n"); SQUADS.flush()
            if do_kf:
                nxt_s = rel + SAMPLE_DT
                regions = detect_killfeed_from_frame(arr, fw, fh, content_x0=cx0, content_x1=cx1, search_zone=zone)
                if regions: n_kf_frames += 1
                for ri, r in enumerate(regions):
                    l, t, w, h = r["left"], r["top"], r["width"], r["height"]
                    tt0 = max(0, t-2); tt1 = min(fh, t+h+2); ll0 = max(0, l-25); ll1 = min(fw, l+w+25)
                    img = arr[tt0:tt1, ll0:ll1]
                    try:
                        processed, _, _ = preprocess_for_easyocr(img, stretch_x=sx, stretch_y=sy)
                    except Exception:
                        continue
                    saving = processed[0] if isinstance(processed, list) else processed
                    if is_empty_line(saving): continue
                    text = ocr_with_easyocr(saving, color_img=img)
                    if looks_like_noise(text): continue
                    cropname = f"{rel:07.1f}_r{ri}.png"
                    cv2.imwrite(os.path.join(CROPS, cropname), cv2.cvtColor(img, cv2.COLOR_BGRA2BGR))
                    READS.write(json.dumps({"t": round(rel,2), "ri": ri, "box": [l,t,w,h],
                                            "crop": cropname, "text": text}) + "\n"); READS.flush()
                    n_read += 1
            if int(rel) % 60 == 0 and do_kf:
                print(f"  ...t={rel:7.1f}s reads={n_read} kf_frames={n_kf_frames} wall={time.time()-started:.0f}s", flush=True)
    _finish(n_read, n_kf_frames, rel if t0 is not None else 0, started)

def _finish(n_read, n_kf_frames, rel, started):
    READS.close(); SQUADS.close()
    print(f"\n==== CAPTURE DONE ==== video={rel:.1f}s reads={n_read} kf_frames={n_kf_frames} wall={time.time()-started:.0f}s", flush=True)

if __name__ == "__main__":
    main()
