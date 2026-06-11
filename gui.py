"""Apex OCR Control Panel — tkinter GUI for managing OCR, API, and ELO processes."""

import csv
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext, ttk

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from PIL import Image, ImageTk
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    from parsers import debug_parse_line
except Exception:
    def debug_parse_line(line):  # type: ignore[misc]
        return ([f"[ERROR] parsers.py not importable"], None, None)

LOG_PATH       = Path("killfeed_log.csv")
CROPS_DIR      = Path("crops")
PLAYER_DB_PATH = Path("player_names.json")
ELO_DB_PATH    = Path("elo.db")
API_URL        = "http://localhost:8080"
PYTHON         = sys.executable  # same interpreter as gui.py


# ─────────────────────────────────────────────────────────── Crop Viewer ──────

class CropViewer(tk.Toplevel):
    """Side-by-side crop image + OCR text viewer."""

    _MAX_CROPS = 300  # max crops loaded per streamer

    def __init__(self, master):
        super().__init__(master)
        self.title("Crop Viewer")
        self.configure(bg="#1e1e1e")
        self.geometry("1100x600")
        self.resizable(True, True)

        self._crops: list[Path] = []
        self._idx   = 0
        self._photo = None  # keep reference to avoid GC

        # Load CSV rows once for matching
        self._csv_rows = self._load_csv()

        self._build_ui()
        self._populate_streamers()

    def _load_csv(self) -> list[dict]:
        rows = []
        if LOG_PATH.exists():
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                rows = list(csv.DictReader(f))
        # Build per-streamer sorted (timestamp_float, row) list for fast lookup
        self._csv_by_streamer: dict[str, list[tuple[float, dict]]] = {}
        for row in rows:
            key = row.get("streamer", "").lower()
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, KeyError):
                continue
            self._csv_by_streamer.setdefault(key, []).append((ts, row))
        for v in self._csv_by_streamer.values():
            v.sort(key=lambda x: x[0])
        return rows

    def _build_ui(self):
        # ── top bar ──
        bar = tk.Frame(self, bg="#252526")
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))

        tk.Label(bar, text="Streamer:", bg="#252526", fg="#cccccc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._streamer_var = tk.StringVar()
        self._streamer_cb  = ttk.Combobox(bar, textvariable=self._streamer_var,
                                          state="readonly", width=18,
                                          font=("Consolas", 9))
        self._streamer_cb.pack(side=tk.LEFT, padx=(4, 12))
        self._streamer_cb.bind("<<ComboboxSelected>>", lambda _: self._load_streamer())

        tk.Label(bar, text="Filter:", bg="#252526", fg="#cccccc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Entry(bar, textvariable=self._filter_var, width=14,
                 bg="#3c3c3c", fg="white", insertbackground="white",
                 relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(4, 12))

        self._events_only = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Events only", variable=self._events_only,
                       bg="#252526", fg="#cccccc", selectcolor="#3c3c3c",
                       activebackground="#252526", activeforeground="#cccccc",
                       font=("Consolas", 9),
                       command=self._apply_filter).pack(side=tk.LEFT, padx=(0, 12))

        self._counter = tk.Label(bar, text="0 / 0", bg="#252526", fg="#888888",
                                 font=("Consolas", 9))
        self._counter.pack(side=tk.LEFT)

        tk.Button(bar, text="◀ Prev", command=self._prev,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.RIGHT, padx=2)
        tk.Button(bar, text="Next ▶", command=self._next,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.RIGHT, padx=2)

        # ── main area ──
        main = tk.Frame(self, bg="#1e1e1e")
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Left: listbox
        list_frame = tk.Frame(main, bg="#1e1e1e")
        list_frame.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(list_frame, text="Crops", bg="#1e1e1e", fg="#888888",
                 font=("Consolas", 8)).pack(anchor="w")
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox = tk.Listbox(list_frame, yscrollcommand=sb.set,
                                   bg="#0d0d0d", fg="#d4d4d4", selectbackground="#0e639c",
                                   font=("Consolas", 8), width=28, relief=tk.FLAT,
                                   activestyle="none")
        self._listbox.pack(fill=tk.Y, expand=True)
        sb.config(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        # Right: image + info
        right = tk.Frame(main, bg="#1e1e1e")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        # Image panel (fixed height, white bg so crops are visible)
        self._img_label = tk.Label(right, bg="#2d2d2d", text="Select a crop",
                                   fg="#888888", font=("Consolas", 10))
        self._img_label.pack(fill=tk.X, pady=(0, 6))

        # Info panel
        self._info = scrolledtext.ScrolledText(
            right, bg="#0d0d0d", fg="#d4d4d4",
            font=("Consolas", 9), height=12, state=tk.DISABLED, relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._info.pack(fill=tk.BOTH, expand=True)
        self._info.tag_config("label",  foreground="#888888")
        self._info.tag_config("value",  foreground="#d4d4d4")
        self._info.tag_config("match",  foreground="#4ec994")
        self._info.tag_config("warn",   foreground="#e2c08d")
        self._info.tag_config("nodata", foreground="#cc3333")

    # ── data loading ──────────────────────────────────────────────────────────

    def _populate_streamers(self):
        if not CROPS_DIR.exists():
            return
        streamers = sorted(p.name for p in CROPS_DIR.iterdir() if p.is_dir())
        self._streamer_cb["values"] = streamers
        if streamers:
            self._streamer_cb.current(0)
            self._load_streamer()

    def _load_streamer(self):
        streamer = self._streamer_var.get()
        if not streamer:
            return
        folder = CROPS_DIR / streamer
        all_crops = sorted(folder.glob("*.png"), key=lambda p: p.name, reverse=True)
        self._all_crops = all_crops[:self._MAX_CROPS]
        self._filter_var.set("")
        self._apply_filter()

    def _apply_filter(self):
        filt = self._filter_var.get().lower()
        candidates = [p for p in self._all_crops if not filt or filt in p.name.lower()]

        if self._events_only.get():
            streamer = self._streamer_var.get().lower()
            csv_entries = self._csv_by_streamer.get(streamer, [])
            csv_timestamps = [ts for ts, _ in csv_entries]
            filtered = []
            for p in candidates:
                try:
                    crop_ts = datetime.strptime(p.stem[:15], "%Y%m%d_%H%M%S").timestamp()
                    if csv_timestamps and min(abs(crop_ts - t) for t in csv_timestamps) <= 12:
                        filtered.append(p)
                except ValueError:
                    pass
            candidates = filtered

        self._crops = candidates
        self._idx = 0
        self._refresh_listbox()
        self._show_current()

    def _refresh_listbox(self):
        self._listbox.delete(0, tk.END)
        for p in self._crops:
            self._listbox.insert(tk.END, p.name)
        if self._crops:
            self._listbox.selection_set(0)

    # ── navigation ────────────────────────────────────────────────────────────

    def _prev(self):
        if self._crops and self._idx > 0:
            self._idx -= 1
            self._show_current()

    def _next(self):
        if self._crops and self._idx < len(self._crops) - 1:
            self._idx += 1
            self._show_current()

    def _on_listbox_select(self, _event):
        sel = self._listbox.curselection()
        if sel:
            self._idx = sel[0]
            self._show_current()

    # ── display ───────────────────────────────────────────────────────────────

    def _show_current(self):
        if not self._crops:
            self._counter.config(text="0 / 0")
            return

        self._counter.config(text=f"{self._idx + 1} / {len(self._crops)}")
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(self._idx)
        self._listbox.see(self._idx)

        path = self._crops[self._idx]
        self._show_image(path)
        self._show_info(path)

    def _show_image(self, path: Path):
        if not _PIL_OK:
            self._img_label.config(image="", text="Pillow not installed — cannot display image")
            return
        try:
            img = Image.open(path).convert("RGB")
            # Scale up short crops (killfeed strips are ~35px tall) for readability
            w, h = img.size
            scale = max(1, min(4, 120 // max(h, 1)))
            _nearest = getattr(Image, "NEAREST", getattr(Image.Resampling, "NEAREST", 0))
            _lanczos = getattr(Image, "LANCZOS", getattr(Image.Resampling, "LANCZOS", 1))
            if scale > 1:
                img = img.resize((w * scale, h * scale), _nearest)
            # Fit to panel width (~800px max)
            max_w = 800
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize((max_w, int(img.height * ratio)), _lanczos)
            self._photo = ImageTk.PhotoImage(img)
            self._img_label.config(image=self._photo, text="")
        except Exception as e:
            self._img_label.config(image="", text=f"Error loading image: {e}")

    def _show_info(self, path: Path):
        # Parse timestamp from filename: YYYYMMDD_HHMMSS_...
        crop_ts = None
        try:
            crop_ts = datetime.strptime(path.stem[:15], "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            pass

        # Find closest CSV row using pre-built index
        streamer = self._streamer_var.get()
        match = None
        best_delta = float("inf")
        entries = self._csv_by_streamer.get(streamer.lower(), [])
        if crop_ts and entries:
            for ts, row in entries:
                delta = abs(crop_ts - ts)
                if delta < best_delta:
                    best_delta = delta
                    match = row

        self._info.config(state=tk.NORMAL)
        self._info.delete("1.0", tk.END)

        def line(label, value, tag="value"):
            self._info.insert(tk.END, f"{label:<16}", "label")
            self._info.insert(tk.END, f"{value}\n", tag)

        line("File:", path.name)
        line("Streamer:", streamer)
        if crop_ts:
            line("Crop time:", datetime.fromtimestamp(crop_ts).strftime("%Y-%m-%d %H:%M:%S"))

        self._info.insert(tk.END, "\n")

        if match:
            # Color by proximity: green ≤15s, yellow ≤60s, red >60s
            if best_delta <= 15:
                dist_tag = "match"
            elif best_delta <= 60:
                dist_tag = "warn"
            else:
                dist_tag = "nodata"
            self._info.insert(tk.END,
                f"── Nearest CSV event ({best_delta:.0f}s away) ──\n", dist_tag)
            line("Event type:", match.get("event_type", ""))
            line("Attacker:",   match.get("attacker", "—"))
            line("Victim:",     match.get("victim", "—"))
            line("Atk conf:",   match.get("attacker_conf", "—"))
            line("Vic conf:",   match.get("victim_conf", "—"))
            self._info.insert(tk.END, "\n")
            line("Raw OCR:",   match.get("raw_text", ""))
            line("Canonical:", match.get("canonical", ""))
        else:
            self._info.insert(tk.END,
                "── No CSV rows for this streamer ──\n"
                "(streamer may predate the current CSV)\n", "nodata")

        self._info.config(state=tk.DISABLED)


class PlayerCropSearch(tk.Toplevel):
    """Search crop images by player name across all streamers."""

    _MATCH_WINDOW    = 12  # seconds either side of a CSV event
    _MIN_VARIANT_LEN = 5   # variants shorter than this are too noisy to search raw_text

    def __init__(self, master):
        super().__init__(master)
        self.title("Player Crop Search")
        self.configure(bg="#1e1e1e")
        self.geometry("1200x650")
        self.resizable(True, True)

        self._results: list[dict] = []
        self._idx = 0
        self._photo = None

        self._csv_rows, self._csv_by_streamer = self._load_csv()
        self._player_db = self._load_player_db()
        self._build_ui()

    # ── data ──────────────────────────────────────────────────────────────────

    def _load_player_db(self) -> dict:
        path = Path(__file__).parent / "player_names.json"
        if path.exists():
            try:
                import json
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _load_csv(self):
        rows = []
        if LOG_PATH.exists():
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                rows = list(csv.DictReader(f))
        by_streamer: dict[str, list[tuple[float, dict]]] = {}
        for row in rows:
            key = row.get("streamer", "").lower()
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, KeyError):
                continue
            by_streamer.setdefault(key, []).append((ts, row))
        for v in by_streamer.values():
            v.sort(key=lambda x: x[0])
        return rows, by_streamer

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        bar = tk.Frame(self, bg="#252526")
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))

        tk.Label(bar, text="Player name:", bg="#252526", fg="#cccccc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._search_var = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self._search_var, width=22,
                         bg="#3c3c3c", fg="white", insertbackground="white",
                         relief=tk.FLAT, font=("Consolas", 9))
        entry.pack(side=tk.LEFT, padx=(4, 8))
        entry.bind("<Return>", lambda _: self._search())

        tk.Button(bar, text="Search", command=self._search,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.LEFT)

        self._flowchart_btn = tk.Button(
            bar, text="Show Flowchart", command=self._open_flowchart,
            bg="#6b3d9a", fg="white", relief=tk.FLAT,
            font=("Consolas", 9), cursor="hand2", state=tk.DISABLED,
        )
        self._flowchart_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._counter = tk.Label(bar, text="", bg="#252526", fg="#888888",
                                 font=("Consolas", 9))
        self._counter.pack(side=tk.LEFT, padx=12)

        tk.Button(bar, text="◀ Prev", command=self._prev,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.RIGHT, padx=2)
        tk.Button(bar, text="Next ▶", command=self._next,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.RIGHT, padx=2)

        main = tk.Frame(self, bg="#1e1e1e")
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        list_frame = tk.Frame(main, bg="#1e1e1e")
        list_frame.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(list_frame, text="Results", bg="#1e1e1e", fg="#888888",
                 font=("Consolas", 8)).pack(anchor="w")
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox = tk.Listbox(list_frame, yscrollcommand=sb.set,
                                   bg="#0d0d0d", fg="#d4d4d4", selectbackground="#0e639c",
                                   font=("Consolas", 8), width=38, relief=tk.FLAT,
                                   activestyle="none")
        self._listbox.pack(fill=tk.Y, expand=True)
        sb.config(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(main, bg="#1e1e1e")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self._img_label = tk.Label(right, bg="#2d2d2d", text="Search for a player name",
                                   fg="#888888", font=("Consolas", 10))
        self._img_label.pack(fill=tk.X, pady=(0, 6))

        self._info = scrolledtext.ScrolledText(
            right, bg="#0d0d0d", fg="#d4d4d4",
            font=("Consolas", 9), height=12, state=tk.DISABLED, relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._info.pack(fill=tk.BOTH, expand=True)
        self._info.tag_config("label",  foreground="#888888")
        self._info.tag_config("value",  foreground="#d4d4d4")
        self._info.tag_config("match",  foreground="#4ec994")
        self._info.tag_config("warn",   foreground="#e2c08d")
        self._info.tag_config("nodata", foreground="#cc3333")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _find_crop(self, row: dict):
        """Return the closest crop file within ±_MATCH_WINDOW of the event, or None."""
        streamer = row.get("streamer", "")
        try:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, KeyError):
            return None
        if not CROPS_DIR.exists():
            return None
        folder = CROPS_DIR / streamer
        if not folder.exists():
            return None
        best_delta = float("inf")
        crop_path = None
        for p in folder.glob("*.png"):
            try:
                ct = datetime.strptime(p.stem[:15], "%Y%m%d_%H%M%S").timestamp()
                d = abs(ct - ts)
                if d <= self._MATCH_WINDOW and d < best_delta:
                    best_delta = d
                    crop_path = p
            except ValueError:
                pass
        return crop_path

    # ── search ────────────────────────────────────────────────────────────────

    def _search(self):
        query = self._search_var.get().strip().lower()
        if not query:
            return

        self._results = []

        # ── Pass 1: direct attacker / victim match ─────────────────────────────
        for row in self._csv_rows:
            atk = (row.get("attacker") or "").lower()
            vic = (row.get("victim") or "").lower()
            if atk != query and vic != query:
                continue
            crop_path = self._find_crop(row)
            role = "ATK" if atk == query else "VIC"
            ts_str = row.get("timestamp", "")[-8:]
            streamer = row.get("streamer", "")
            label = f"[{role}] {streamer:<14} {ts_str}  {row.get('event_type',''):<10}"
            if crop_path:
                label += " 📷"
            self._results.append({"row": row, "crop_path": crop_path, "label": label})

        # ── Pass 2: variant raw-text scan ──────────────────────────────────────
        seen_ids = {id(r["row"]) for r in self._results}
        entry = None
        for k, v in self._player_db.items():
            if k.lower() == query:
                entry = v
                break
        if entry:
            long_variants = [
                (var, cnt)
                for var, cnt in entry.get("variants", {}).items()
                if len(var) >= self._MIN_VARIANT_LEN
            ]
            for row in self._csv_rows:
                if id(row) in seen_ids:
                    continue
                raw = (row.get("raw_text") or "").lower()
                # pick the longest variant that appears in raw_text
                matched = max(
                    ((var, cnt) for var, cnt in long_variants if var.lower() in raw),
                    key=lambda x: len(x[0]),
                    default=None,
                )
                if not matched:
                    continue
                matched_variant = matched[0]
                crop_path = self._find_crop(row)
                streamer = row.get("streamer", "")
                ts_str = row.get("timestamp", "")[-8:]
                tag = matched_variant[:12]
                label = f"[raw:{tag:<12}] {streamer:<10} {ts_str}  {row.get('event_type',''):<10}"
                if crop_path:
                    label += " 📷"
                self._results.append({"row": row, "crop_path": crop_path, "label": label})
                seen_ids.add(id(row))

        self._listbox.delete(0, tk.END)
        for r in self._results:
            self._listbox.insert(tk.END, r["label"])

        direct     = sum(1 for r in self._results if not r["label"].startswith("[raw:"))
        via_raw    = len(self._results) - direct
        crops_found = sum(1 for r in self._results if r["crop_path"])
        self._counter.config(
            text=f"{direct} direct · {via_raw} via raw text · {crops_found} crop(s)"
        )
        self._flowchart_btn.config(state=tk.NORMAL if query else tk.DISABLED)

        self._idx = 0
        if self._results:
            self._listbox.selection_set(0)
            self._show_current()

    # ── navigation ────────────────────────────────────────────────────────────

    def _prev(self):
        if self._results and self._idx > 0:
            self._idx -= 1
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(self._idx)
            self._listbox.see(self._idx)
            self._show_current()

    def _next(self):
        if self._results and self._idx < len(self._results) - 1:
            self._idx += 1
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(self._idx)
            self._listbox.see(self._idx)
            self._show_current()

    def _on_select(self, _event):
        sel = self._listbox.curselection()
        if sel:
            self._idx = sel[0]
            self._show_current()

    def _open_flowchart(self):
        query = self._search_var.get().strip()
        if query:
            VariantFlowchart(self, query, self._csv_rows)

    # ── display ───────────────────────────────────────────────────────────────

    def _show_current(self):
        if not self._results:
            return
        result = self._results[self._idx]
        self._show_image(result["crop_path"])
        self._show_info(result)

    def _show_image(self, path):
        if path is None:
            self._img_label.config(image="", text="No crop image found for this event")
            return
        if not _PIL_OK:
            self._img_label.config(image="", text="Pillow not installed")
            return
        try:
            img = Image.open(path).convert("RGB")
            w, h = img.size
            scale = max(1, min(4, 120 // max(h, 1)))
            _nearest = getattr(Image, "NEAREST", getattr(Image.Resampling, "NEAREST", 0))
            _lanczos = getattr(Image, "LANCZOS", getattr(Image.Resampling, "LANCZOS", 1))
            if scale > 1:
                img = img.resize((w * scale, h * scale), _nearest)
            max_w = 800
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize((max_w, int(img.height * ratio)), _lanczos)
            self._photo = ImageTk.PhotoImage(img)
            self._img_label.config(image=self._photo, text="")
        except Exception as e:
            self._img_label.config(image="", text=f"Error: {e}")

    def _show_info(self, result):
        row = result["row"]
        crop_path = result["crop_path"]

        self._info.config(state=tk.NORMAL)
        self._info.delete("1.0", tk.END)

        def line(label, value, tag="value"):
            self._info.insert(tk.END, f"{label:<16}", "label")
            self._info.insert(tk.END, f"{value}\n", tag)

        line("Streamer:", row.get("streamer", ""))
        line("Timestamp:", row.get("timestamp", ""))
        line("Event type:", row.get("event_type", ""))
        line("Attacker:", row.get("attacker") or "—")
        line("Victim:", row.get("victim") or "—")
        line("Atk conf:", row.get("attacker_conf", "—"))
        line("Vic conf:", row.get("victim_conf", "—"))
        self._info.insert(tk.END, "\n")
        line("Raw OCR:", row.get("raw_text", ""))
        line("Canonical:", row.get("canonical", ""))
        self._info.insert(tk.END, "\n")
        if crop_path:
            line("Crop file:", crop_path.name, "match")
        else:
            self._info.insert(tk.END, "No crop image found within ±12s\n", "nodata")

        # ── Variant info from player_names.json ──────────────────────────────
        canonical_key = self._search_var.get().strip()
        entry = None
        for k, v in self._player_db.items():
            if k.lower() == canonical_key.lower():
                entry = v
                break
        if entry:
            variants = entry.get("variants", {})
            total_seen = entry.get("total_seen", sum(variants.values()))
            sorted_vars = sorted(variants.items(), key=lambda x: -x[1])
            self._info.insert(tk.END,
                f"\n── Known variants ({len(sorted_vars)}) · total sightings {total_seen} ──\n",
                "match")
            for vname, cnt in sorted_vars[:20]:
                self._info.insert(tk.END, f"  {vname:<30}  ×{cnt}\n", "value")
            if len(sorted_vars) > 20:
                self._info.insert(tk.END,
                    f"  … and {len(sorted_vars) - 20} more\n", "nodata")

        self._info.config(state=tk.DISABLED)


class VariantFlowchart(tk.Toplevel):
    """3-column Canvas flowchart: raw OCR events → variants → canonical."""

    _BOX_W       = 290   # event / variant box width
    _BOX_H       = 28
    _GAP_Y       = 8
    _COL_A       = 20    # x-start column A (events)
    _COL_B       = 370   # x-start column B (variants)
    _COL_C       = 710   # x-start column C (canonical)
    _CAN_W       = 180
    _CANVAS_MIN_W = 950
    _MARGIN_TOP  = 40
    _MARGIN_BOT  = 30
    _MAX_EVENTS  = 40    # cap events shown

    def __init__(self, master, canonical: str, csv_rows: list):
        super().__init__(master)
        self.title(f"Variant Flowchart — {canonical}")
        self.configure(bg="#1e1e1e")
        self.geometry("980x620")
        self.resizable(True, True)

        self._canonical = canonical
        self._events = [
            r for r in csv_rows
            if (r.get("attacker") or "").lower() == canonical.lower()
            or (r.get("victim")   or "").lower() == canonical.lower()
        ][: self._MAX_EVENTS]

        # Load variants from player_names.json
        self._variants: dict[str, int] = {}
        pdb_path = Path(__file__).parent / "player_names.json"
        if pdb_path.exists():
            try:
                import json
                data = json.loads(pdb_path.read_text(encoding="utf-8"))
                for key, val in data.items():
                    if key.lower() == canonical.lower():
                        self._variants = val.get("variants", {})
                        break
            except Exception:
                pass

        # Load ELO stats from elo.db
        self._elo     = None
        self._matches = 0
        self._kills   = 0
        self._deaths  = 0
        edb_path = Path(__file__).parent / "elo.db"
        if edb_path.exists():
            try:
                import sqlite3
                with sqlite3.connect(str(edb_path)) as conn:
                    row = conn.execute(
                        "SELECT elo, matches_played, total_kills, total_deaths "
                        "FROM player_ratings WHERE lower(player) = lower(?)",
                        (canonical,),
                    ).fetchone()
                    if row:
                        self._elo, self._matches, self._kills, self._deaths = row
            except Exception:
                pass

        self._photo = None
        self._build_ui()
        self._draw()

    def _build_ui(self):
        hdr = tk.Frame(self, bg="#252526")
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text=f"  {self._canonical}",
                 bg="#252526", fg="#e2c08d", font=("Consolas", 11, "bold"),
                 pady=5).pack(side=tk.LEFT)
        info = (f"   {len(self._events)} events  ·  "
                f"{len(self._variants)} variants  ·  "
                + (f"ELO {self._elo:.0f}  ·  {self._matches} matches"
                   if self._elo else "no ELO data"))
        tk.Label(hdr, text=info, bg="#252526", fg="#888888",
                 font=("Consolas", 9)).pack(side=tk.LEFT)

        frame = tk.Frame(self, bg="#0d0d0d")
        frame.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(frame, bg="#0d0d0d", bd=0, highlightthickness=0)
        h_sb = tk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self._canvas.xview)
        v_sb = tk.Scrollbar(frame, orient=tk.VERTICAL,   command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=h_sb.set, yscrollcommand=v_sb.set)
        v_sb.pack(side=tk.RIGHT,  fill=tk.Y)
        h_sb.pack(side=tk.BOTTOM, fill=tk.X)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-(e.delta // 120), "units"),
        )

    def _draw(self):
        c  = self._canvas
        bh = self._BOX_H
        gy = self._GAP_Y
        mt = self._MARGIN_TOP

        sorted_vars = sorted(self._variants.items(), key=lambda x: -x[1])
        max_count   = sorted_vars[0][1] if sorted_vars else 1

        # ── column headers ─────────────────────────────────────────────────────
        for x, label in [
            (self._COL_A + self._BOX_W // 2, "RAW OCR EVENTS"),
            (self._COL_B + self._BOX_W // 2, "VARIANTS"),
            (self._COL_C + self._CAN_W // 2, "CANONICAL"),
        ]:
            c.create_text(x, mt - 16, text=label, fill="#555555",
                          font=("Consolas", 7, "bold"), anchor="center")

        # ── Column A — event boxes ─────────────────────────────────────────────
        event_anchors: list[tuple[int, int]] = []  # (right_x, center_y)
        for i, row in enumerate(self._events):
            y   = mt + i * (bh + gy)
            raw = (row.get("raw_text") or "").strip()
            ts  = (row.get("timestamp") or "")[-8:]
            role = "A" if (row.get("attacker") or "").lower() == self._canonical.lower() else "V"
            label = f"[{role}] {ts}  {raw}"
            if len(label) > 46:
                label = label[:45] + "…"
            x0, y0, x1, y1 = self._COL_A, y, self._COL_A + self._BOX_W, y + bh
            c.create_rectangle(x0, y0, x1, y1, fill="#162330", outline="#2a4a5a", width=1)
            c.create_text(x0 + 6, (y0 + y1) // 2, text=label, fill="#9cdcfe",
                          font=("Consolas", 7), anchor="w")
            event_anchors.append((x1, (y0 + y1) // 2))

        # ── Column B — variant boxes ───────────────────────────────────────────
        var_anchors: dict[str, tuple[int, int, int]] = {}  # name→(left_x, right_x, cy)
        for i, (variant, count) in enumerate(sorted_vars):
            y     = mt + i * (bh + gy)
            ratio = count / max_count
            r = int(20  + ratio * 15)
            g = int(90  + ratio * 110)
            b = int(150 + ratio * 100)
            fill = f"#{r:02x}{g:02x}{b:02x}"
            x0, y0, x1, y1 = self._COL_B, y, self._COL_B + self._BOX_W, y + bh
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline="#3a6a8a", width=1)
            label = f"{variant}  [×{count}]"
            if len(label) > 42:
                label = label[:41] + "…"
            text_color = "#0a0a0a" if ratio > 0.4 else "#d4d4d4"
            c.create_text(x0 + 6, (y0 + y1) // 2, text=label,
                          fill=text_color, font=("Consolas", 8, "bold"), anchor="w")
            var_anchors[variant] = (x0, x1, (y0 + y1) // 2)

        # ── Column C — canonical box ───────────────────────────────────────────
        n_rows    = max(len(self._events), len(sorted_vars), 1)
        total_h   = mt + n_rows * (bh + gy)
        can_h     = max(90, min(150, n_rows * (bh + gy)))
        can_y     = mt + (n_rows * (bh + gy) - can_h) // 2
        cx0, cy0  = self._COL_C, can_y
        cx1, cy1  = cx0 + self._CAN_W, can_y + can_h
        can_left  = cx0
        can_mid_y = (cy0 + cy1) // 2

        c.create_rectangle(cx0, cy0, cx1, cy1, fill="#2a1e00", outline="#e2a000", width=2)
        c.create_text((cx0 + cx1) // 2, cy0 + 18,
                      text=self._canonical, fill="#e2c08d",
                      font=("Consolas", 10, "bold"), anchor="center")
        if self._elo is not None:
            c.create_text((cx0 + cx1) // 2, cy0 + 38,
                          text=f"ELO  {self._elo:.0f}", fill="#cccc55",
                          font=("Consolas", 9), anchor="center")
            c.create_text((cx0 + cx1) // 2, cy0 + 56,
                          text=f"Matches  {self._matches}", fill="#888888",
                          font=("Consolas", 8), anchor="center")
            c.create_text((cx0 + cx1) // 2, cy0 + 70,
                          text=f"K {self._kills}  D {self._deaths}", fill="#888888",
                          font=("Consolas", 8), anchor="center")
        else:
            c.create_text((cx0 + cx1) // 2, cy0 + 48,
                          text="No ELO data", fill="#555555",
                          font=("Consolas", 8), anchor="center")

        # ── Arrows: events → variants (or canonical) ──────────────────────────
        for i, row in enumerate(self._events):
            raw = (row.get("raw_text") or "").lower()
            ex, ey = event_anchors[i]
            matched = None
            best    = 0
            for variant in self._variants:
                vl = variant.lower()
                if len(vl) >= 3 and vl in raw and len(vl) > best:
                    best    = len(vl)
                    matched = variant
            if matched and matched in var_anchors:
                vx0, _, vy = var_anchors[matched]
                c.create_line(ex, ey, vx0, vy,
                              fill="#2a5a7a", arrow=tk.LAST, width=1)
            else:
                c.create_line(ex, ey, can_left, can_mid_y,
                              fill="#3a4a3a", arrow=tk.LAST, width=1)

        # ── Arrows: variants → canonical ──────────────────────────────────────
        for _, (vx0, vx1, vy) in var_anchors.items():
            c.create_line(vx1, vy, can_left, can_mid_y,
                          fill="#8a6000", arrow=tk.LAST, width=1)

        # ── Update scroll region ───────────────────────────────────────────────
        c.configure(scrollregion=(
            0, 0,
            self._CANVAS_MIN_W,
            max(total_h + self._MARGIN_BOT, 400),
        ))


class NoiseAuditWindow(tk.Toplevel):
    """Noise audit table + parse debugger for killfeed CSV events.

    Left pane: filterable Treeview of all CSV rows scored by suspicion.
    Right pane: crop image + OCR line entry + step-by-step parse trace.
    """

    _MATCH_WINDOW = 12    # seconds either side of event for crop lookup
    _MAX_ROWS     = 5000  # cap on CSV rows loaded

    def __init__(self, master):
        super().__init__(master)
        self.title("Noise Audit / Parse Debugger")
        self.configure(bg="#1e1e1e")
        self.geometry("1400x750")
        self.resizable(True, True)

        self._photo = None          # keep ImageTk ref to prevent GC
        self._all_rows: list = []
        self._scored_rows: list = []  # list of (score, reasons, row)

        self._load_csv()
        self._build_ui()
        self._populate_streamer_filter()
        self._refresh_table()

    # ── data ──────────────────────────────────────────────────────────────────

    def _load_csv(self):
        self._all_rows = []
        if not LOG_PATH.exists():
            return
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
        self._all_rows = rows[:self._MAX_ROWS]

    @staticmethod
    def _score_row(attacker: str, victim: str) -> tuple:
        score = 0
        reasons = []
        for name, label in [(attacker, "ATK"), (victim, "VIC")]:
            if not name:
                continue
            if " " in name:
                score += 2
                reasons.append(f"{label}:space")
            if len(name) <= 4:
                score += 2
                reasons.append(f"{label}:len{len(name)}")
            elif len(name) <= 6 and sum(c.isdigit() for c in name) / len(name) >= 0.5:
                score += 1
                reasons.append(f"{label}:short+digits")
            if re.search(r'[^A-Za-z0-9._\-/]', name):
                score += 1
                reasons.append(f"{label}:special_chars")
        return score, " | ".join(reasons)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        pw = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg="#1e1e1e",
                            sashrelief=tk.FLAT, sashwidth=4)
        pw.pack(fill=tk.BOTH, expand=True)

        # ── Left pane ──────────────────────────────────────────────────────────
        left = tk.Frame(pw, bg="#1e1e1e")
        pw.add(left, minsize=500)

        # Filter bar
        bar = tk.Frame(left, bg="#252526")
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))

        self._suspicious_only = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bar, text="Suspicious only", variable=self._suspicious_only,
            command=self._refresh_table,
            bg="#252526", fg="#cccccc", selectcolor="#3c3c3c",
            activebackground="#252526", activeforeground="#cccccc",
            font=("Consolas", 9),
        ).pack(side=tk.LEFT, padx=(4, 10))

        tk.Label(bar, text="Streamer:", bg="#252526", fg="#cccccc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._streamer_filter = ttk.Combobox(bar, state="readonly", width=14,
                                              font=("Consolas", 9))
        self._streamer_filter.pack(side=tk.LEFT, padx=(4, 10))
        self._streamer_filter.bind("<<ComboboxSelected>>",
                                   lambda _: self._refresh_table())

        self._count_label = tk.Label(bar, text="", bg="#252526", fg="#888888",
                                     font=("Consolas", 9))
        self._count_label.pack(side=tk.LEFT, padx=6)

        # Treeview
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Audit.Treeview",
                        background="#1e1e1e", foreground="#d4d4d4",
                        fieldbackground="#1e1e1e", rowheight=22,
                        font=("Consolas", 8))
        style.configure("Audit.Treeview.Heading",
                        background="#252526", foreground="#888888",
                        font=("Consolas", 8, "bold"))
        style.map("Audit.Treeview",
                  background=[("selected", "#0e639c")],
                  foreground=[("selected", "#ffffff")])

        tree_frame = tk.Frame(left, bg="#1e1e1e")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        cols = ("#", "Streamer", "Attacker", "Victim", "Score", "Reasons")
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                   selectmode="browse", style="Audit.Treeview")

        col_cfg = {
            "#":        (45,  tk.CENTER),
            "Streamer": (90,  tk.W),
            "Attacker": (150, tk.W),
            "Victim":   (150, tk.W),
            "Score":    (50,  tk.CENTER),
            "Reasons":  (200, tk.W),
        }
        for col, (w, anchor) in col_cfg.items():
            self._tree.heading(col, text=col,
                               command=(self._sort_by_score if col == "Score" else lambda: None))
            self._tree.column(col, width=w, anchor=anchor,
                              stretch=(col == "Reasons"))

        self._tree.tag_configure("red",    background="#4a1515")
        self._tree.tag_configure("yellow", background="#3a3010")
        self._tree.tag_configure("normal", background="#1e1e1e")

        v_sb = tk.Scrollbar(tree_frame, orient=tk.VERTICAL,   command=self._tree.yview)
        h_sb = tk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=v_sb.set, xscrollcommand=h_sb.set)
        v_sb.pack(side=tk.RIGHT,  fill=tk.Y)
        h_sb.pack(side=tk.BOTTOM, fill=tk.X)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)

        # ── Right pane ─────────────────────────────────────────────────────────
        right = tk.Frame(pw, bg="#1e1e1e")
        pw.add(right, minsize=380)

        # Crop image
        self._img_label = tk.Label(right, bg="#2d2d2d",
                                   text="Select a row to view crop",
                                   fg="#888888", font=("Consolas", 9))
        self._img_label.pack(fill=tk.X, padx=6, pady=(6, 3))

        # Parse entry bar
        parse_bar = tk.Frame(right, bg="#252526")
        parse_bar.pack(fill=tk.X, padx=6, pady=(0, 3))
        tk.Label(parse_bar, text="OCR line:", bg="#252526", fg="#888888",
                 font=("Consolas", 8)).pack(side=tk.LEFT, padx=(4, 0))
        self._parse_entry = tk.Entry(
            parse_bar, bg="#3c3c3c", fg="white",
            insertbackground="white", relief=tk.FLAT, font=("Consolas", 9),
        )
        self._parse_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 6))
        self._parse_entry.bind("<Return>", lambda _: self._run_parse())
        tk.Button(parse_bar, text="Parse", command=self._run_parse,
                  bg="#0e639c", fg="white", relief=tk.FLAT,
                  font=("Consolas", 9), cursor="hand2").pack(side=tk.LEFT)

        # Trace output
        tk.Label(right, text="PARSE TRACE", bg="#1e1e1e", fg="#444444",
                 font=("Consolas", 7)).pack(anchor="w", padx=6)
        self._trace_box = scrolledtext.ScrolledText(
            right, bg="#0d0d0d", fg="#d4d4d4",
            font=("Consolas", 8), state=tk.DISABLED, relief=tk.FLAT, wrap=tk.WORD,
        )
        self._trace_box.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        self._trace_box.tag_config("input",  foreground="#9cdcfe")
        self._trace_box.tag_config("split",  foreground="#888888")
        self._trace_box.tag_config("seg",    foreground="#dcdcaa")
        self._trace_box.tag_config("pass_",  foreground="#4ec994")
        self._trace_box.tag_config("fail",   foreground="#666666")
        self._trace_box.tag_config("prefix", foreground="#e2c08d")
        self._trace_box.tag_config("result", foreground="#e2c08d")

    # ── populate / refresh ────────────────────────────────────────────────────

    def _populate_streamer_filter(self):
        streamers = sorted({r.get("streamer", "") for r in self._all_rows if r.get("streamer")})
        self._streamer_filter["values"] = ["All"] + streamers
        self._streamer_filter.current(0)

    def _refresh_table(self):
        suspicious_only  = self._suspicious_only.get()
        streamer_filter  = self._streamer_filter.get() if self._streamer_filter["values"] else "All"

        display_rows = []
        for row in self._all_rows:
            if streamer_filter and streamer_filter != "All":
                if row.get("streamer", "") != streamer_filter:
                    continue
            atk = row.get("attacker") or ""
            vic = row.get("victim")   or ""
            score, reasons = self._score_row(atk, vic)
            if suspicious_only and score == 0:
                continue
            display_rows.append((score, reasons, row))

        display_rows.sort(key=lambda x: (-x[0], x[2].get("timestamp", "")))
        self._scored_rows = display_rows

        self._tree.delete(*self._tree.get_children())
        for i, (score, reasons, row) in enumerate(display_rows):
            atk      = row.get("attacker") or "—"
            vic      = row.get("victim")   or "—"
            streamer = row.get("streamer") or ""
            tag      = "red" if score >= 3 else ("yellow" if score >= 1 else "normal")
            self._tree.insert("", tk.END, iid=str(i),
                              values=(i + 1, streamer, atk, vic, score, reasons),
                              tags=(tag,))

        self._count_label.config(text=f"{len(display_rows)} rows")

    def _sort_by_score(self):
        # Toggle sort direction
        self._sort_asc = not getattr(self, "_sort_asc", False)
        self._scored_rows.sort(key=lambda x: (x[0], x[2].get("timestamp", "")),
                               reverse=not self._sort_asc)
        self._tree.delete(*self._tree.get_children())
        for i, (score, reasons, row) in enumerate(self._scored_rows):
            atk      = row.get("attacker") or "—"
            vic      = row.get("victim")   or "—"
            streamer = row.get("streamer") or ""
            tag      = "red" if score >= 3 else ("yellow" if score >= 1 else "normal")
            self._tree.insert("", tk.END, iid=str(i),
                              values=(i + 1, streamer, atk, vic, score, reasons),
                              tags=(tag,))

    # ── row selection ─────────────────────────────────────────────────────────

    def _on_row_select(self, _event):
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self._scored_rows):
            return
        _score, _reasons, row = self._scored_rows[idx]

        self._show_image(self._find_crop(row))

        raw = row.get("raw_text", "")
        self._parse_entry.delete(0, tk.END)
        self._parse_entry.insert(0, raw)
        self._run_parse()

    # ── crop helpers ──────────────────────────────────────────────────────────

    def _find_crop(self, row: dict):
        streamer = row.get("streamer", "")
        try:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, KeyError):
            return None
        if not CROPS_DIR.exists():
            return None
        folder = CROPS_DIR / streamer
        if not folder.exists():
            return None
        best_delta = float("inf")
        crop_path  = None
        for p in folder.glob("*.png"):
            try:
                ct = datetime.strptime(p.stem[:15], "%Y%m%d_%H%M%S").timestamp()
                d  = abs(ct - ts)
                if d <= self._MATCH_WINDOW and d < best_delta:
                    best_delta = d
                    crop_path  = p
            except ValueError:
                pass
        return crop_path

    def _show_image(self, path):
        if path is None:
            self._img_label.config(image="", text="No crop image within ±12s")
            return
        if not _PIL_OK:
            self._img_label.config(image="", text="Pillow not installed")
            return
        try:
            img = Image.open(path).convert("RGB")
            w, h = img.size
            scale = max(1, min(4, 120 // max(h, 1)))
            _nearest = getattr(Image, "NEAREST", getattr(Image.Resampling, "NEAREST", 0))
            _lanczos = getattr(Image, "LANCZOS",  getattr(Image.Resampling, "LANCZOS",  1))
            if scale > 1:
                img = img.resize((w * scale, h * scale), _nearest)
            max_w = 650
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize((max_w, int(img.height * ratio)), _lanczos)
            self._photo = ImageTk.PhotoImage(img)
            self._img_label.config(image=self._photo, text="")
        except Exception as e:
            self._img_label.config(image="", text=f"Error: {e}")

    # ── parse trace ───────────────────────────────────────────────────────────

    def _run_parse(self):
        raw = self._parse_entry.get().strip()
        self._trace_box.config(state=tk.NORMAL)
        self._trace_box.delete("1.0", tk.END)

        if not raw:
            self._trace_box.insert(tk.END, "Enter a raw OCR line above.\n", "fail")
            self._trace_box.config(state=tk.DISABLED)
            return

        try:
            trace, _atk, _vic = debug_parse_line(raw)
        except Exception as e:
            self._trace_box.insert(tk.END, f"[ERROR] {e}\n", "result")
            self._trace_box.config(state=tk.DISABLED)
            return

        for line in trace:
            if line.startswith("[INPUT]") or line.startswith("[NORM]"):
                tag = "input"
            elif line.startswith("[SPLIT]"):
                tag = "split"
            elif line.startswith("[ATK SEG]") or line.startswith("[VIC SEG]"):
                tag = "seg"
            elif "PASS" in line:
                tag = "pass_"
            elif "short_prefix" in line or "prefix" in line.lower():
                tag = "prefix"
            elif (line.startswith("[RESULT]") or line.startswith("[ATK]")
                  or line.startswith("[VIC]") or line.startswith("[SKIP]")):
                tag = "result"
            else:
                tag = "fail"
            self._trace_box.insert(tk.END, line + "\n", tag)

        self._trace_box.config(state=tk.DISABLED)


class ProcessManager:
    """Wraps a subprocess.Popen handle with a background stdout-reader thread."""

    def __init__(self, log_queue: queue.Queue, prefix: str):
        self._q       = log_queue
        self._prefix  = prefix
        self._proc: subprocess.Popen | None = None

    def start(self, cmd: list[str]) -> None:
        if self.is_running():
            return
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(Path(__file__).parent),
        )
        t = threading.Thread(target=self._read_stdout, daemon=True)
        t.start()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_stdout(self) -> None:
        if self._proc is None:
            return
        for line in self._proc.stdout:
            self._q.put((self._prefix, line.rstrip()))
        # Process exited — send sentinel
        self._q.put((self._prefix, f"--- {self._prefix} process ended ---"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Apex OCR Control Panel")
        self.resizable(True, True)
        self.configure(bg="#1e1e1e")

        self._log_q = queue.Queue()
        self._ocr_mgr  = ProcessManager(self._log_q, "OCR")
        self._api_mgr  = ProcessManager(self._log_q, "API")
        self._elo_mgr  = ProcessManager(self._log_q, "ELO")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()
        self._update_status()
        self._update_stats()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        # ── main frames ──
        left  = tk.Frame(self, bg="#252526", width=240)
        right = tk.Frame(self, bg="#1e1e1e")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=8)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        left.pack_propagate(False)

        self._build_controls(left)
        self._build_log(right)

    def _build_controls(self, parent):
        pad = {"padx": 8, "pady": 4}

        def section(text):
            tk.Label(parent, text=text, bg="#252526", fg="#888888",
                     font=("Consolas", 8)).pack(anchor="w", **pad)

        def btn(parent, text, cmd, color="#0e639c"):
            return tk.Button(
                parent, text=text, command=cmd,
                bg=color, fg="white", activebackground="#1177bb",
                relief=tk.FLAT, padx=6, pady=3,
                font=("Consolas", 9), cursor="hand2",
            )

        # ── OCR section ──
        section("── OCR CAPTURE ──")

        top_row = tk.Frame(parent, bg="#252526")
        top_row.pack(fill=tk.X, **pad)
        tk.Label(top_row, text="Top N:", bg="#252526", fg="#cccccc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self._top_n = tk.IntVar(value=20)
        tk.Spinbox(top_row, from_=1, to=100, textvariable=self._top_n,
                   width=4, bg="#3c3c3c", fg="white", insertbackground="white",
                   relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(4, 0))

        self._ocr_start_btn = btn(parent, "Start OCR", self._start_ocr, "#16825d")
        self._ocr_start_btn.pack(fill=tk.X, **pad)
        self._ocr_stop_btn  = btn(parent, "Stop OCR",  self._stop_ocr,  "#8b1a1a")
        self._ocr_stop_btn.pack(fill=tk.X, **pad)
        self._ocr_stop_btn.config(state=tk.DISABLED)

        self._ocr_status = tk.Label(parent, text="● Stopped", bg="#252526",
                                    fg="#cc3333", font=("Consolas", 9))
        self._ocr_status.pack(anchor="w", **pad)

        # ── API section ──
        section("── REST API ──")
        self._api_start_btn = btn(parent, "Start API", self._start_api, "#16825d")
        self._api_start_btn.pack(fill=tk.X, **pad)
        self._api_stop_btn  = btn(parent, "Stop API",  self._stop_api,  "#8b1a1a")
        self._api_stop_btn.pack(fill=tk.X, **pad)
        self._api_stop_btn.config(state=tk.DISABLED)

        self._api_status = tk.Label(parent, text="● Stopped", bg="#252526",
                                    fg="#cc3333", font=("Consolas", 9))
        self._api_status.pack(anchor="w", **pad)

        # ── ELO section ──
        section("── ELO RANKING ──")
        self._elo_btn = btn(parent, "Rebuild ELO", self._run_reprocess, "#6b3d9a")
        self._elo_btn.pack(fill=tk.X, **pad)

        # ── Stats section ──
        section("── STATS ──")
        self._stat_kills  = tk.Label(parent, text="Kills logged: —", bg="#252526",
                                     fg="#cccccc", font=("Consolas", 9), anchor="w")
        self._stat_kills.pack(fill=tk.X, **pad)
        self._stat_top    = tk.Label(parent, text="Top player: —", bg="#252526",
                                     fg="#cccccc", font=("Consolas", 9), anchor="w",
                                     wraplength=200, justify=tk.LEFT)
        self._stat_top.pack(fill=tk.X, **pad)

        # ── View Data section ──
        tk.Frame(parent, height=1, bg="#444444").pack(fill=tk.X, padx=8, pady=6)
        section("── VIEW DATA ──")
        btn(parent, "API Docs (browser)",    lambda: webbrowser.open(f"{API_URL}/docs"),      "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Rankings (browser)",    lambda: webbrowser.open(f"{API_URL}/rankings"),  "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Kill Events (browser)", lambda: webbrowser.open(f"{API_URL}/events/kills"), "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Open CSV",      self._open_csv,          "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Open DB",       self._open_db,           "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Browse Crops",  self._open_crop_viewer,  "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Search by Player", self._open_player_search, "#555555").pack(fill=tk.X, **pad)
        btn(parent, "Noise Audit",      self._open_noise_audit,   "#555555").pack(fill=tk.X, **pad)

        # ── Clear log ──
        tk.Frame(parent, height=1, bg="#444444").pack(fill=tk.X, padx=8, pady=6)
        btn(parent, "Clear Log", self._clear_log, "#555555").pack(fill=tk.X, **pad)

    def _build_log(self, parent):
        tk.Label(parent, text="LOG OUTPUT", bg="#1e1e1e", fg="#888888",
                 font=("Consolas", 8)).pack(anchor="w")
        self._log = scrolledtext.ScrolledText(
            parent, bg="#0d0d0d", fg="#d4d4d4", insertbackground="white",
            font=("Consolas", 9), state=tk.DISABLED, relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._log.pack(fill=tk.BOTH, expand=True)

        # Color tags
        self._log.tag_config("OCR", foreground="#569cd6")
        self._log.tag_config("API", foreground="#4ec994")
        self._log.tag_config("ELO", foreground="#dcdcaa")
        self._log.tag_config("SYS", foreground="#888888")

    # ------------------------------------------------------------------ actions

    def _start_ocr(self):
        n = self._top_n.get()
        self._ocr_mgr.start([PYTHON, "ocr.py", "--top", str(n)])
        self._log_sys(f"Started OCR — watching top {n} streams")
        self._ocr_start_btn.config(state=tk.DISABLED)
        self._ocr_stop_btn.config(state=tk.NORMAL)

    def _stop_ocr(self):
        self._ocr_mgr.stop()
        self._log_sys("OCR stopped")
        self._ocr_start_btn.config(state=tk.NORMAL)
        self._ocr_stop_btn.config(state=tk.DISABLED)

    def _start_api(self):
        self._api_mgr.start([PYTHON, "api.py"])
        self._log_sys("Started API — http://localhost:8080")
        self._api_start_btn.config(state=tk.DISABLED)
        self._api_stop_btn.config(state=tk.NORMAL)

    def _stop_api(self):
        self._api_mgr.stop()
        self._log_sys("API stopped")
        self._api_start_btn.config(state=tk.NORMAL)
        self._api_stop_btn.config(state=tk.DISABLED)

    def _run_reprocess(self):
        if self._elo_mgr.is_running():
            return
        self._elo_btn.config(state=tk.DISABLED, text="Rebuilding…")
        self._elo_mgr.start([PYTHON, "reprocess.py", "--reset"])
        self._log_sys("Rebuilding ELO rankings…")

    def _open_crop_viewer(self):
        CropViewer(self)

    def _open_player_search(self):
        PlayerCropSearch(self)

    def _open_noise_audit(self):
        NoiseAuditWindow(self)

    def _open_csv(self):
        path = Path(__file__).parent / "killfeed_log.csv"
        if path.exists():
            os.startfile(path)
        else:
            self._log_sys("killfeed_log.csv not found")

    def _open_db(self):
        path = Path(__file__).parent / "elo.db"
        if path.exists():
            os.startfile(path)
        else:
            self._log_sys("elo.db not found — run Rebuild ELO first")

    def _clear_log(self):
        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)

    def _log_sys(self, msg: str):
        self._append_line("SYS", f"[SYS] {msg}")

    def _append_line(self, tag: str, text: str):
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n", tag)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    # ------------------------------------------------------------------ polling

    def _poll_queue(self):
        try:
            while True:
                prefix, line = self._log_q.get_nowait()
                self._append_line(prefix, f"[{prefix}] {line}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_status(self):
        # OCR
        if self._ocr_mgr.is_running():
            self._ocr_status.config(text="● Running", fg="#3fb950")
            self._ocr_start_btn.config(state=tk.DISABLED)
            self._ocr_stop_btn.config(state=tk.NORMAL)
        else:
            self._ocr_status.config(text="● Stopped", fg="#cc3333")
            self._ocr_start_btn.config(state=tk.NORMAL)
            self._ocr_stop_btn.config(state=tk.DISABLED)

        # API
        if self._api_mgr.is_running():
            self._api_status.config(text="● Running", fg="#3fb950")
            self._api_start_btn.config(state=tk.DISABLED)
            self._api_stop_btn.config(state=tk.NORMAL)
        else:
            self._api_status.config(text="● Stopped", fg="#cc3333")
            self._api_start_btn.config(state=tk.NORMAL)
            self._api_stop_btn.config(state=tk.DISABLED)

        # ELO button re-enable when done
        if not self._elo_mgr.is_running() and str(self._elo_btn.cget("text")) != "Rebuild ELO":
            self._elo_btn.config(state=tk.NORMAL, text="Rebuild ELO")

        self.after(1000, self._update_status)

    def _update_stats(self):
        # Try API first
        kills = None
        top_player = None

        if _REQUESTS_OK and self._api_mgr.is_running():
            try:
                r = requests.get(f"{API_URL}/rankings", timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    rankings = data.get("rankings", [])
                    if rankings:
                        top = rankings[0]
                        top_player = f"{top.get('player', '?')}  ELO {top.get('elo', 0):.0f}"
            except Exception:
                pass

        # Fallback: count CSV rows
        try:
            if LOG_PATH.exists():
                lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                kills = max(0, len(lines) - 1)  # subtract header
        except Exception:
            pass

        if kills is not None:
            self._stat_kills.config(text=f"Kills logged: {kills:,}")
        if top_player:
            self._stat_top.config(text=f"#1: {top_player}")

        self.after(10_000, self._update_stats)

    # ------------------------------------------------------------------ close

    def _on_close(self):
        self._ocr_mgr.stop()
        self._api_mgr.stop()
        self._elo_mgr.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
