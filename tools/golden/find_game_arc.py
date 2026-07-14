"""Map a game's arc in ONE continuous VOD read (offset unreliable across separate invocations on a
growing live VOD, but monotonic within a single read). Samples 'N SQUADS LEFT' every N video-sec so
we see game start (20 squads) -> progression -> end (squads->1 = win, or HUD vanishes = death/summary),
plus the video-PTS duration. Saves a downscaled frame each sample for visual spot-check.

Usage: python _vod_arc.py <vod_url> <offset> <duration_sec> [sample_dt]
"""
import sys, os, subprocess
import av, cv2

import config
from detect_killfeed import _NonSeekablePipe, _streamlink_bin, _get_frame_dimensions
from ocr import preprocess_for_easyocr, ocr_with_easyocr, _has_killfeed_content
from detect_squads import read_squads_left

URL = sys.argv[1]; OFFSET = sys.argv[2]
DUR = float(sys.argv[3]) if len(sys.argv) > 3 else 1500.0
DT  = float(sys.argv[4]) if len(sys.argv) > 4 else 20.0

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(SP, "vod_arc"); os.makedirs(OUT, exist_ok=True)

def ocr_region(reg):
    proc = preprocess_for_easyocr(reg)[0]
    return ocr_with_easyocr(proc, color_img=reg)

def open_vod(url, offset, duration):
    sl = subprocess.Popen([_streamlink_bin(), "--stdout", "--hls-start-offset", offset,
                           "--hls-duration", str(int(duration) + 10), url, config.STREAM_QUALITY],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.Popen(["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1",
                           "-loglevel", "error"], stdin=sl.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    return av.open(_NonSeekablePipe(ff.stdout), format="mpegts"), [sl, ff]

def main():
    print(f"ARC scan: offset={OFFSET} dur={DUR}s dt={DT}s(video)")
    container, _ = open_vod(URL, OFFSET, DUR)
    if not container.streams.video:
        print("NO VIDEO"); return
    fw, fh = _get_frame_dimensions(container)
    print(f"dims {fw}x{fh}")
    t0 = None; nxt = 0.0
    for packet in container.demux(video=0):
        try: frames = packet.decode()
        except Exception: continue
        for frame in frames:
            if frame.pts is None: continue
            pts = float(frame.pts * frame.time_base)
            if t0 is None: t0 = pts
            rel = pts - t0
            if rel > DUR:
                print("done (duration reached)"); return
            if rel < nxt: continue
            nxt = rel + DT
            arr = frame.to_ndarray(format="bgra")
            sq, pl, raw = read_squads_left(arr, ocr_region)
            # cheap gameplay marker: any killfeed content in the feed zone right now
            cv2.imwrite(os.path.join(OUT, f"{rel:07.1f}_sq{sq}.jpg"),
                        cv2.resize(cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR), (854, 480)))
            print(f"  t={rel:7.1f}  squads={sq!s:4} players={pl!s:4} | raw={raw[:40]!r}", flush=True)
    print("stream ended")

if __name__ == "__main__":
    main()
