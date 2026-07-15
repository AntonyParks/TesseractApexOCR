"""Extract WIDE killfeed-region crops at 1 fps (video-PTS) from a VOD game, for a vision-model
ground-truth pass. Wider than the OCR search zone so it catches lines the narrow zone clipped.
Usage: python _vod_kf_crops.py <vod_url> <offset> <duration_sec>
"""
import sys, os, subprocess
import av, cv2

import config
from detect_killfeed import _NonSeekablePipe, _streamlink_bin, _get_frame_dimensions

URL = sys.argv[1]; OFFSET = sys.argv[2]
DUR = float(sys.argv[3]) if len(sys.argv) > 3 else 860.0
DT = 1.0  # one crop per video-second

# Generous killfeed region (fractions of frame): top-right, wider+taller than the OCR zone
# (Sang OCR zone was x0=0.58 y0=0.16 y1=0.32). Extra margin catches clipped/edge lines.
X0F, X1F, Y0F, Y1F = 0.60, 1.00, 0.075, 0.35

SP = os.environ.get("GOLDEN_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(SP, "kf_crops"); os.makedirs(OUT, exist_ok=True)

def open_vod(url, offset, duration):
    sl = subprocess.Popen([_streamlink_bin(), "--stdout", "--hls-start-offset", offset,
                           "--hls-duration", str(int(duration) + 10), url, config.STREAM_QUALITY],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.Popen(["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1",
                           "-loglevel", "error"], stdin=sl.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    return av.open(_NonSeekablePipe(ff.stdout), format="mpegts"), [sl, ff]

def main():
    container, _ = open_vod(URL, OFFSET, DUR)
    if not container.streams.video:
        print("NO VIDEO"); return
    fw, fh = _get_frame_dimensions(container)
    x0, x1 = int(X0F * fw), int(X1F * fw)
    y0, y1 = int(Y0F * fh), int(Y1F * fh)
    print(f"dims {fw}x{fh}  crop x{x0}-{x1} y{y0}-{y1} ({x1-x0}x{y1-y0})", flush=True)
    t0 = None; nxt = 0.0; n = 0
    for packet in container.demux(video=0):
        try: frames = packet.decode()
        except Exception: continue
        for frame in frames:
            if frame.pts is None: continue
            pts = float(frame.pts * frame.time_base)
            if t0 is None: t0 = pts
            rel = pts - t0
            if rel > DUR:
                print(f"DONE {n} crops", flush=True); return
            if rel < nxt: continue
            nxt = rel + DT
            arr = frame.to_ndarray(format="bgr24")
            cv2.imwrite(os.path.join(OUT, f"{int(round(rel)):04d}.png"), arr[y0:y1, x0:x1])
            n += 1
            if n % 60 == 0: print(f"  {n} crops (t={rel:.0f}s)", flush=True)
    print(f"DONE {n} crops", flush=True)

if __name__ == "__main__":
    main()
