"""Coarse no-OCR peek: grab ONE frame at each given VOD offset to map a game's arc
(find start/end boundaries and whether the streamer wins) before committing to an OCR pass.
Usage: python _vod_peek.py <vod_url> <offset1> <offset2> ...
"""
import sys, os, subprocess
import av, cv2

from detect_killfeed import _NonSeekablePipe, _streamlink_bin
import config

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(SP, "vod_peek"); os.makedirs(OUT, exist_ok=True)

def grab_one(url, offset):
    sl = subprocess.Popen(
        [_streamlink_bin(), "--stdout", "--hls-start-offset", offset, "--hls-duration", "4",
         url, config.STREAM_QUALITY],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.Popen(
        ["ffmpeg", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1", "-loglevel", "error"],
        stdin=sl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        container = av.open(_NonSeekablePipe(ff.stdout), format="mpegts")
        for packet in container.demux(video=0):
            for frame in packet.decode():
                arr = frame.to_ndarray(format="bgr24")
                fn = os.path.join(OUT, f"{offset.replace(':','')}.jpg")
                cv2.imwrite(fn, arr)
                return fn
    finally:
        for p in (sl, ff):
            try: p.terminate()
            except Exception: pass
    return None

if __name__ == "__main__":
    url = sys.argv[1]
    for off in sys.argv[2:]:
        try:
            fn = grab_one(url, off)
            print(f"{off} -> {fn}")
        except Exception as e:
            print(f"{off} -> ERROR {e}")
