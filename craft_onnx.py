"""DirectML-accelerated CRAFT text detector for EasyOCR (bead hy2).

EasyOCR's CRAFT detection is ~87% of per-line OCR cost (264ms of 305ms on this CPU). This module runs
that detector on the GPU via onnxruntime-directml instead, measured 13-26x faster and numerically
identical to torch (max-abs-diff 1.8e-07) -- so the OCR output is unchanged, only faster.

Integration is a drop-in: EasyOCR calls its detector exactly once as `y, feature = net(x)` (see
easyocr/detection.py test_net). `reader.detector` is a plain callable, so we replace it with
DmlCraftDetector -- an object whose __call__ runs the ONNX model on DirectML and returns
(torch_tensor, None). All of EasyOCR's surrounding pre/post-processing (normalize, resize, getDetBoxes)
is left untouched on CPU, and the recognizer stays on CPU (torch).

Everything degrades gracefully: if DirectML or the ONNX model is unavailable, install_dml_detector
leaves the stock torch CRAFT in place and OCR keeps working.
"""
import os
import threading

# Process-wide singleton (see install_dml_detector). MANDATORY, not an optimization: ORT-DirectML
# segfaults on concurrent Run() across INDEPENDENT sessions, and ocr.py runs as module `__main__`
# while `import ocr` (e.g. from calibrate_zone's local calibration) creates a SECOND `ocr` module
# copy with its own reader -> its own detector. Sharing one detector here (craft_onnx is always
# imported canonically, never as __main__) guarantees every reader copy points at the SAME session
# + SAME lock, so all detection serializes safely. Two sessions = SIGSEGV (observed live 2026-07-15).
_DML_DETECTOR = None
_DML_DETECTOR_LOCK = threading.Lock()


class DmlCraftDetector:
    """Callable drop-in for EasyOCR's `reader.detector` that runs CRAFT on DirectML via ONNX Runtime.

    Called as `y, feature = detector(x)` where `x` is the already-normalized NCHW float32 tensor
    EasyOCR builds in test_net (do NOT normalize again here -- the ONNX graph contains no
    normalization, matching the exported model). Returns (y, None):
      - y: torch tensor, shape (N, H/2, W/2, 2) -- CRAFT's region+affinity score map. Returned as a
           torch tensor so EasyOCR's `out[:,:,0].cpu().data.numpy()` runs verbatim.
      - feature: None -- EasyOCR never consumes the CRAFT feature map.
    """

    def __init__(self, onnx_path: str):
        import onnxruntime as ort
        # DmlExecutionProvider = AMD/Intel/NVIDIA GPU via DirectX 12; CPU EP is the in-graph fallback
        # for any op DirectML can't run (CRAFT is standard convs, so it runs fully on DML in practice).
        self.sess = ort.InferenceSession(
            onnx_path, providers=["DmlExecutionProvider", "CPUExecutionProvider"]
        )
        self._input_name = self.sess.get_inputs()[0].name  # "x"
        # MANDATORY lock -- do NOT remove. ORT-DirectML segfaults on concurrent Run(), so all worker
        # threads sharing this detector must serialize here (calls are 2-18ms; the GPU serializes
        # anyway). Removing it OR allowing a second session (see the _DML_DETECTOR singleton above)
        # crashes the process -- both were observed as exit-139 SIGSEGV, not theoretical.
        self._lock = threading.Lock()

    def __call__(self, x):
        import torch
        arr = x.detach().cpu().numpy()
        if arr.dtype != "float32":
            arr = arr.astype("float32")
        with self._lock:
            y = self.sess.run(["y"], {self._input_name: arr})[0]
        return torch.from_numpy(y), None

    # EasyOCR/torch code may call .eval() / .to() on the detector object in some paths; make them no-ops
    # so the swap is transparent regardless of how it's referenced.
    def eval(self):
        return self

    def to(self, *args, **kwargs):
        return self


def export_craft_onnx(reader, onnx_path: str) -> None:
    """Export the reader's live CRAFT detector to ONNX (opset 18, dynamic batch/H/W).

    Exports from `reader.detector` so the ONNX mirrors the exact weights production uses. Writes
    `onnx_path` plus an external-data weights file (`<onnx_path>.data`, ~83MB). Requires torch, onnx,
    and onnxscript (the torch>=2.6 dynamo exporter)."""
    import torch

    os.makedirs(os.path.dirname(os.path.abspath(onnx_path)), exist_ok=True)
    net = reader.detector
    if hasattr(net, "eval"):
        net.eval()
    dummy = torch.randn(1, 3, 480, 640)  # NCHW; H/W are dynamic below
    with torch.no_grad():
        torch.onnx.export(
            net, dummy, onnx_path,
            input_names=["x"], output_names=["y", "feat"],
            dynamic_axes={"x": {0: "n", 2: "h", 3: "w"},
                          "y": {0: "n", 1: "h2", 2: "w2"},
                          "feat": {0: "n", 2: "h2", 3: "w2"}},
            opset_version=18, do_constant_folding=True,
        )


def install_dml_detector(reader, onnx_path: str | None = None) -> bool:
    """Swap `reader.detector` for a DirectML ONNX detector. Returns True if installed, False if it
    fell back to the stock torch CRAFT. Never raises for expected-unavailable conditions (no DML GPU,
    export deps missing) -- OCR must keep working either way.

    Generates `craft.onnx` on first run if missing (models/ is gitignored, so it's a local artifact,
    provisioned like craft_mlt_25k.pth rather than committed)."""
    try:
        import onnxruntime as ort
    except ImportError:
        print("[EasyOCR] onnxruntime not installed; using torch CRAFT.")
        return False

    if "DmlExecutionProvider" not in ort.get_available_providers():
        print("[EasyOCR] DirectML provider unavailable; using torch CRAFT.")
        return False

    if onnx_path is None:
        try:
            from config import EASYOCR_CRAFT_ONNX
            onnx_path = str(EASYOCR_CRAFT_ONNX)
        except Exception:
            onnx_path = os.path.join("models", "easyocr_custom", "craft.onnx")

    # Build the DML detector exactly ONCE per process and share it across every reader. If ocr.py is
    # both `__main__` and an imported `ocr` module, each copy builds its own reader; without this
    # shared singleton each reader would get an INDEPENDENT ORT-DirectML session and concurrent Run()
    # across them SIGSEGVs (observed live 2026-07-15). One session + one lock = safe serialization.
    global _DML_DETECTOR
    with _DML_DETECTOR_LOCK:
        if _DML_DETECTOR is None:
            if not os.path.exists(onnx_path):
                print(f"[EasyOCR] Exporting CRAFT -> ONNX (first run): {onnx_path}")
                try:
                    export_craft_onnx(reader, onnx_path)
                except Exception as e:
                    print(f"[EasyOCR] CRAFT ONNX export failed ({type(e).__name__}: {e}); using torch CRAFT.")
                    return False
            _DML_DETECTOR = DmlCraftDetector(onnx_path)
        reader.detector = _DML_DETECTOR
    print(f"[EasyOCR] DirectML CRAFT detector installed ({os.path.basename(onnx_path)}).")
    return True
