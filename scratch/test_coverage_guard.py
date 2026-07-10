"""Coverage guard in _derive_zone_from_regions: reject facecam mis-anchors, drop low mislabels,
cap zone height. No Claude/streams needed."""
import sys
sys.path.insert(0, r"g:\PycharmProjects\TesseractApexOCR")
import calibrate_zone as cz
from config import KILLFEED_TOP_MAX_FRAC, KILLFEED_MAX_SPAN_FRAC
FH = 1080; CW = 1920
def reg(topf, h=28, left=1400): return {"left":left,"top":int(topf*FH),"width":400,"height":h}
def derive(topfs): return cz._derive_zone_from_regions([reg(t) for t in topfs], 0, CW, FH)
P=[0]; F=[0]
def ck(d, cond):
    ok=bool(cond); print(f"  [{'PASS' if ok else 'FAIL'}] {d}"); P[0]+=ok; F[0]+=not ok

# 1. Xcamorex-exact facecam mis-anchor (y0=0.327) -> REJECT
ck("facecam mis-anchor (tops 0.327/0.35/0.42) rejected", derive([0.327,0.35,0.42]) is None)
# 2. Real killfeed at top -> accept, sane zone
z = derive([0.17,0.20,0.23])
ck("real top killfeed accepted", z is not None)
ck("  y0 tight (~0.155)", z and 0.14 <= z["y0_frac"] <= 0.17)
ck("  span within cap", z and (z["y1_frac"]-z["y0_frac"]) <= KILLFEED_MAX_SPAN_FRAC + 0.001)
# 3. Low mislabel mixed in -> dropped, zone not inflated
z2 = derive([0.17,0.20,0.45])
ck("low mislabel (0.45) dropped, zone stays tight", z2 is not None and z2["y1_frac"] < 0.36)
# 4. Single line after filter -> below MIN -> None
ck("single killfeed line rejected (< MIN_LINES)", derive([0.18]) is None)
# 5. Fredstxr-exact (y0=0.416) -> reject
ck("deep facecam mis-anchor (0.416) rejected", derive([0.416,0.44,0.47]) is None)
# 6. Borderline just above threshold accepted (0.24), just below rejected (0.30)
ck("borderline 0.24 top accepted", derive([0.24,0.27]) is not None)
ck("borderline 0.30 top rejected", derive([0.30,0.33]) is None)
# 7. Very tall span clamped
z3 = derive([0.16,0.20,0.24,0.28,0.32,0.36])   # spans 0.20, all within band
ck("tall stack: span capped at MAX_SPAN", z3 and (z3["y1_frac"]-z3["y0_frac"]) <= KILLFEED_MAX_SPAN_FRAC + 0.001)
print(f"\nKILLFEED_TOP_MAX_FRAC={KILLFEED_TOP_MAX_FRAC} MAX_SPAN={KILLFEED_MAX_SPAN_FRAC}")
print("ALL PASS" if F[0]==0 else f"{F[0]} FAILED")
