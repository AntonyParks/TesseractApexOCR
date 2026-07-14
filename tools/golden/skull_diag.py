"""Diagnose why detect_kill_skull misses a visible skull. Replays the exact pipeline path on a
crop (preprocess -> EasyOCR word boxes -> gap -> map to original coords) and dumps EVERY red
connected component in the gap region with all measured properties vs the thresholds, so we can
see which gate rejects the real skull."""
import sys, os
import cv2, numpy as np
import ocr as ocr_mod
from ocr import preprocess_for_easyocr, _get_easyocr_reader, EASYOCR_GAP_THRESHOLD

CROP = sys.argv[1]
color = cv2.imread(CROP)
print(f"crop {os.path.basename(CROP)} shape={color.shape}")
proc = preprocess_for_easyocr(color, stretch_x=1.0, stretch_y=936/1080)[0]
ocr_mod._easyocr_reader = None
reader = _get_easyocr_reader()
results = reader.readtext(proc, detail=1, paragraph=False)
results.sort(key=lambda r: min(pt[0] for pt in r[0]))
words = [(min(p[0] for p in b), max(p[0] for p in b), w) for b, w, c in results if w.strip()]
print("words:", [w for _,_,w in words])

def analyze_gap(color_img, x0, x1, label):
    if color_img.ndim == 3 and color_img.shape[2] == 4:
        color_img = cv2.cvtColor(color_img, cv2.COLOR_BGRA2BGR)
    H, W = color_img.shape[:2]
    x0 = max(0, int(x0) - 3); x1 = min(W, int(x1) + 3)
    if x1 - x0 < 6:
        print(f"  {label}: gap too small ({x1-x0}px)"); return
    region = color_img[:, x0:x1]; regW = region.shape[1]
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv,(0,120,80),(9,255,255)) | cv2.inRange(hsv,(171,120,80),(180,255,255))
    orange = cv2.inRange(hsv,(10,100,80),(26,255,255))
    redfrac = cv2.countNonZero(red)/red.size
    print(f"  {label}: gap x{x0}-{x1} (w={regW}) H={H} redfrac={redfrac:.2f}")
    if redfrac > 0.45: print("     -> BAILS: red flood >0.45");
    red2 = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((2,2),np.uint8))
    n,_,stats,cents = cv2.connectedComponentsWithStats(red2,8)
    for i in range(1,n):
        cx,cy,cw,ch,area = stats[i]
        if area < 8: continue
        aspect = cw/max(ch,1); fill = area/(cw*ch); posx=(cx+cw/2)/regW; cyf=cents[i][1]/H
        bo = int(cv2.countNonZero(orange[cy:cy+ch,cx:cx+cw]))/(cw*ch+1e-9)
        pad=8; bx0,by0=max(0,cx-pad),max(0,cy-pad); bx1,by1=min(regW,cx+cw+pad),min(H,cy+ch+pad)
        ctx=hsv[by0:by1,bx0:bx1]; nctx=(by1-by0)*(bx1-bx0)
        sat=cv2.inRange(ctx,(0,100,60),(180,255,255))
        other_sat=(cv2.countNonZero(sat)-cv2.countNonZero(red[by0:by1,bx0:bx1])-cv2.countNonZero(orange[by0:by1,bx0:bx1]))/max(nctx,1)
        fails=[]
        if not (40<=area<=450): fails.append(f"area={area}")
        if not (0.45<=aspect<=1.8): fails.append(f"aspect={aspect:.2f}")
        if not (7<=ch<=30): fails.append(f"h={ch}")
        if not (0.10<=cyf<=0.90): fails.append(f"cy={cyf:.2f}")
        if not (0.45<=fill<=0.84): fails.append(f"fill={fill:.2f}")
        if not (0.50<=posx<=0.93): fails.append(f"posx={posx:.2f}")
        if bo>0.45: fails.append(f"orange={bo:.2f}")
        if other_sat>0.25: fails.append(f"ctx_sat={other_sat:.2f}")
        verdict = "SKULL-PASS" if not fails else "reject:"+",".join(fails)
        print(f"     comp area={area:4d} aspect={aspect:.2f} h={ch:2d} cy={cyf:.2f} fill={fill:.2f} posx={posx:.2f} orange={bo:.2f} ctx_sat={other_sat:.2f}  -> {verdict}")

prev=0
for xl,xr,w in words:
    if prev>0 and (xl-prev)>EASYOCR_GAP_THRESHOLD:
        analyze_gap(color,(prev-15)/2,(xl-15)/2,f"gap before {w!r}")
    prev=xr
