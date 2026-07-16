"""
viewer.py — Visual crop browser for TesseractApexOCR

Shows the preprocessing pipeline (raw approximation ↔ what TrOCR reads) and
lets you browse every crop image associated with any player from the ELO
leaderboard.

Usage:
    python viewer.py [--port 8081] [--crops-dir crops] [--db killfeed.db] [--elo-db elo.db]

Then open http://localhost:8081 in a browser.

For crops captured after the raw-save update, the "On screen" panel shows the
real colour frame crop.  For older crops it falls back to a colour-inversion
of the preprocessed image as an approximation.
"""

from __future__ import annotations

import argparse
import bisect
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import cv2
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

# ---------------------------------------------------------------------------
# Config defaults (overridden by CLI args)
# ---------------------------------------------------------------------------
try:
    from config import KILLFEED_DB_PATH as _KDB
except Exception:
    _KDB = Path("killfeed.db")

try:
    from elo_db import ELO_DB_PATH as _EDB
except Exception:
    _EDB = Path("elo.db")

# ---------------------------------------------------------------------------
# App state (set in main())
# ---------------------------------------------------------------------------
CROPS_DIR: Path = Path("crops")
DB_PATH:   Path = _KDB
ELO_PATH:  Path = Path(_EDB)

app = FastAPI(title="TesseractApexOCR Viewer")

# ---------------------------------------------------------------------------
# DB helpers (read-only)
# ---------------------------------------------------------------------------
def _kdb() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(404, f"killfeed.db not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _edb() -> sqlite3.Connection:
    if not ELO_PATH.exists():
        raise HTTPException(404, f"elo.db not found: {ELO_PATH}")
    conn = sqlite3.connect(str(ELO_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_CROP_TS_RE = re.compile(r"^(\d{8}_\d{6})_")

CROP_MATCH_WINDOW = 30  # seconds; crops saved within this window of a DB event are matched


def _parse_crop_ts(filename: str) -> float | None:
    """Parse a crop filename's timestamp to a Unix float, or None."""
    m = _CROP_TS_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


def _scan_streamer_dir(streamer: str) -> tuple[list[float], list[str]]:
    """Scan a streamer's crop directory.

    Returns two parallel sorted lists: (unix_timestamps, filenames).
    Crops without a parseable timestamp are excluded.
    """
    d = CROPS_DIR / streamer
    if not d.exists():
        return [], []
    pairs = []
    for f in d.iterdir():
        if f.suffix != ".png":
            continue
        ts = _parse_crop_ts(f.name)
        if ts is not None:
            pairs.append((ts, f.name))
    pairs.sort()
    timestamps = [p[0] for p in pairs]
    filenames  = [p[1] for p in pairs]
    return timestamps, filenames


def _find_crops_in_window(
    event_ts: str,
    streamer: str,
    timestamps: list[float],
    filenames: list[str],
    window: int = CROP_MATCH_WINDOW,
) -> list[dict]:
    """Binary-search the pre-scanned crop index for crops within ±window seconds."""
    try:
        ev_unix = datetime.strptime(event_ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return []

    lo = bisect.bisect_left(timestamps, ev_unix - window)
    hi = bisect.bisect_right(timestamps, ev_unix + window)

    results = []
    for i in range(lo, hi):
        results.append({
            "streamer_dir": streamer,
            "filename": filenames[i],
            "offset_s": int(abs(timestamps[i] - ev_unix)),
        })
    return results


def _safe_path(streamer_dir: str, filename: str) -> Path:
    """Resolve image path, rejecting path-traversal attempts."""
    if ".." in streamer_dir or ".." in filename:
        raise HTTPException(400, "Invalid path")
    if not re.match(r"^[\w\-. ]+$", streamer_dir) or not re.match(r"^[\w\-.]+\.png$", filename):
        raise HTTPException(400, "Invalid characters in path")
    p = CROPS_DIR / streamer_dir / filename
    if not p.exists():
        raise HTTPException(404, f"Image not found: {p}")
    return p


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
PROVISIONAL_MIN_MATCHES = 5   # below this a player is 'provisional' and hidden from the default board

@app.get("/api/players")
def api_players(q: str = "", limit: int = 200):
    """ELO leaderboard, optionally filtered by player name substring.

    Ranked by the conservative Glicko-2 estimate (mu - 2*rd). The default board hides provisional
    players (< PROVISIONAL_MIN_MATCHES games) so a lucky small sample can't top it — but a name
    search (q) still returns provisional players so they remain findable. elo (mu) is the displayed
    headline; rd is exposed so the UI can badge provisional ratings."""
    try:
        conn = _edb()
    except HTTPException:
        return JSONResponse({"players": [], "error": "elo.db not found"})

    rows = conn.execute(
        """SELECT player, elo, rd, matches_played, total_kills, total_deaths,
                  (elo - 2.0 * rd) AS conservative
           FROM player_ratings
           ORDER BY (elo - 2.0 * rd) DESC"""
    ).fetchall()
    conn.close()

    players = [dict(r) for r in rows]
    for p in players:
        p["provisional"] = p["matches_played"] < PROVISIONAL_MIN_MATCHES
    if q:
        ql = q.lower()
        players = [p for p in players if ql in p["player"].lower()]   # search finds provisional too
    else:
        players = [p for p in players if not p["provisional"]]        # default board: proven only
    return {"players": players[:limit]}


@app.get("/api/player/{player_name}")
def api_player(player_name: str, limit: int = 120):
    """All Kill/BleedOut events where this player appears, with matching crop files."""
    conn = _kdb()

    rows = conn.execute(
        """SELECT id, timestamp, streamer, event_type, raw_text, canonical,
                  attacker, victim, attacker_conf, victim_conf, source, gemini_corrected, crop_filename
           FROM events
           WHERE (attacker = ? OR victim = ?)
             AND event_type IN ('Kill', 'BleedOut', 'Revive')
           ORDER BY timestamp DESC
           LIMIT ?""",
        (player_name, player_name, limit),
    ).fetchall()
    conn.close()

    # Pre-scan each unique streamer dir exactly once
    unique_streamers = {row["streamer"] for row in rows}
    crop_index: dict[str, tuple[list, list]] = {
        s: _scan_streamer_dir(s) for s in unique_streamers
    }

    events = []
    for row in rows:
        d = dict(row)
        d["role"] = "attacker" if row["attacker"] == player_name else "victim"
        
        # Use exact crop filename if the database has it
        crop_fn = row.get("crop_filename")
        if crop_fn:
            if not crop_fn.endswith(".png"):
                crop_fn += ".png"
            stem = crop_fn.removesuffix(".png")
            c = {
                "streamer_dir": row["streamer"],
                "filename": crop_fn,
                "offset_s": 0,
            }
            raw_path = CROPS_DIR / row["streamer"] / f"{stem}_raw.png"
            c["has_color"] = raw_path.exists()
            d["crops"] = [c]
        else:
            # Fallback for old events that don't have a linked crop_filename
            ts_list, fn_list = crop_index.get(row["streamer"], ([], []))
            crops = _find_crops_in_window(row["timestamp"], row["streamer"], ts_list, fn_list)
            for c in crops:
                stem = c["filename"].removesuffix(".png")
                raw_path = CROPS_DIR / c["streamer_dir"] / f"{stem}_raw.png"
                c["has_color"] = raw_path.exists()
            d["crops"] = crops
            
        events.append(d)

    return {"player": player_name, "events": events}


@app.get("/img/{streamer_dir}/{filename}")
def serve_processed(streamer_dir: str, filename: str):
    """Serve the saved (preprocessed) PNG — what TrOCR actually reads."""
    p = _safe_path(streamer_dir, filename)
    return Response(content=p.read_bytes(), media_type="image/png")


@app.get("/img/{streamer_dir}/{filename}/color")
def serve_color(streamer_dir: str, filename: str):
    """Serve the raw colour crop saved alongside the processed one.

    Falls back to a colour-inverted version of the processed image for older
    crops that predate raw-saving support.
    """
    if ".." in streamer_dir or ".." in filename:
        raise HTTPException(400, "Invalid path")
    if not re.match(r"^[\w\-. ]+$", streamer_dir) or not re.match(r"^[\w\-.]+\.png$", filename):
        raise HTTPException(400, "Invalid characters in path")

    stem = filename.removesuffix(".png")
    raw_path = CROPS_DIR / streamer_dir / f"{stem}_raw.png"

    if raw_path.exists():
        return Response(content=raw_path.read_bytes(), media_type="image/png")

    # Fallback: invert the processed image to approximate the on-screen look
    proc_path = CROPS_DIR / streamer_dir / filename
    if not proc_path.exists():
        raise HTTPException(404, f"Image not found: {proc_path}")
    img = cv2.imread(str(proc_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise HTTPException(500, "Failed to decode image")
    ok, buf = cv2.imencode(".png", 255 - img)
    if not ok:
        raise HTTPException(500, "Failed to encode image")
    return Response(content=buf.tobytes(), media_type="image/png")


@app.get("/img/{streamer_dir}/{filename}/raw")
def serve_raw(streamer_dir: str, filename: str):
    """Serve an inverted version of the crop — approximates the original on-screen look."""
    p = _safe_path(streamer_dir, filename)
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise HTTPException(500, "Failed to decode image")
    inverted = 255 - img
    ok, buf = cv2.imencode(".png", inverted)
    if not ok:
        raise HTTPException(500, "Failed to encode image")
    return Response(content=buf.tobytes(), media_type="image/png")


# ---------------------------------------------------------------------------
# HTML single-page app
# ---------------------------------------------------------------------------
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TesseractApexOCR Viewer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: #090a0f;
    color: #e2e8f0;
    display: flex;
    height: 100vh;
    overflow: hidden;
  }

  /* ── Custom Scrollbar ── */
  ::-webkit-scrollbar {
    width: 6px;
    height: 6px;
  }
  ::-webkit-scrollbar-track {
    background: #090a0f;
  }
  ::-webkit-scrollbar-thumb {
    background: #1e2235;
    border-radius: 3px;
  }
  ::-webkit-scrollbar-thumb:hover {
    background: #3b82f6;
  }

  /* ── Sidebar ── */
  #sidebar {
    width: 320px;
    min-width: 240px;
    border-right: 1px solid #1e2335;
    display: flex;
    flex-direction: column;
    background: #0f111a;
  }
  #sidebar-header {
    padding: 18px 16px 14px;
    border-bottom: 1px solid #1e2335;
  }
  #sidebar-header h2 { 
    font-size: 11px; 
    color: #64748b; 
    letter-spacing: .08em; 
    text-transform: uppercase; 
    margin-bottom: 10px;
    font-weight: 700;
  }
  #search {
    width: 100%;
    padding: 8px 12px;
    background: #161925;
    border: 1px solid #242b3e;
    border-radius: 6px;
    color: #f1f5f9;
    font-size: 13px;
    outline: none;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  }
  #search:focus { 
    border-color: #3b82f6; 
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15);
  }

  #player-list {
    overflow-y: auto;
    flex: 1;
  }
  .player-item {
    padding: 10px 16px;
    cursor: pointer;
    border-bottom: 1px solid #141724;
    display: flex;
    align-items: center;
    gap: 12px;
    transition: all 0.15s ease;
  }
  .player-item:hover  { 
    background: #161925; 
  }
  .player-item.active { 
    background: rgba(59, 130, 246, 0.12); 
    border-left: 3px solid #3b82f6;
    padding-left: 13px;
  }
  .player-rank { 
    font-size: 11px; 
    color: #475569; 
    min-width: 28px; 
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .player-name { 
    flex: 1; 
    font-size: 13px; 
    overflow: hidden; 
    text-overflow: ellipsis; 
    white-space: nowrap; 
    font-weight: 500;
    color: #cbd5e1;
  }
  .player-item.active .player-name {
    color: #60a5fa;
  }
  .player-elo  { 
    font-size: 12px; 
    color: #3b82f6; 
    font-weight: 600;
    font-variant-numeric: tabular-nums; 
  }

  /* ── Main ── */
  #main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #main-header {
    padding: 20px 24px;
    border-bottom: 1px solid #1e2335;
    background: #0f111a;
    display: flex;
    align-items: center;
  }
  #main-title { 
    font-size: 18px; 
    font-weight: 700; 
    background: linear-gradient(135deg, #8b5cf6, #3b82f6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
  }
  #no-player { 
    font-size: 12px; 
    color: #64748b; 
    font-weight: 500;
  }
  
  #crop-area {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
  }

  /* ── Cards ── */
  .event-card {
    background: #11131f;
    border: 1px solid #1e233b;
    border-radius: 8px;
    margin-bottom: 24px;
    overflow: hidden;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
    transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
  }
  .event-card:hover {
    transform: translateY(-1px);
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
    border-color: #2e355b;
  }
  .event-meta {
    padding: 12px 18px;
    background: #151829;
    border-bottom: 1px solid #1e233b;
    display: flex;
    gap: 14px;
    align-items: center;
    flex-wrap: wrap;
    font-size: 12px;
  }
  
  /* ── Badges ── */
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 9999px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: .05em;
    text-transform: uppercase;
  }
  .badge-attacker { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.2); }
  .badge-victim   { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.2); }
  .badge-kill     { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }
  .badge-bleedout { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.2); }
  .badge-revive   { background: rgba(6, 182, 212, 0.15); color: #22d3ee; border: 1px solid rgba(6, 182, 212, 0.2); }
  .badge-gemini   { background: rgba(139, 92, 246, 0.15); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.2); }
  
  .meta-ts   { color: #64748b; font-variant-numeric: tabular-nums; }
  .meta-name { color: #f1f5f9; font-weight: 600; }
  .meta-vs   { color: #475569; font-weight: 500; }
  .meta-conf { color: #64748b; font-variant-numeric: tabular-nums; }
  .meta-text { 
    color: #60a5fa; 
    font-family: monospace; 
    font-size: 12px;
    max-width: 320px; 
    overflow: hidden; 
    text-overflow: ellipsis; 
    white-space: nowrap; 
    background: #0a0b12;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid #1e233b;
  }

  /* ── Crop Layout ── */
  .crops-container {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 18px;
  }
  .crop-pair {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    background: #0c0e17;
    border-radius: 6px;
    padding: 16px;
    border: 1px solid #1a1e33;
  }
  @media (max-width: 800px) {
    .crop-pair {
      grid-template-columns: 1fr;
    }
  }
  .crop-panel {
    display: flex;
    flex-direction: column;
  }
  .crop-label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .label-before { color: #a78bfa; }
  .label-after  { color: #3b82f6; }
  .crop-desc    { font-size: 11px; color: #475569; margin-bottom: 10px; }
  .crop-img-wrap {
    background: #06070a;
    border: 1px solid #1e233b;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 12px;
    overflow: hidden;
    max-width: 100%;
  }
  .crop-img-wrap img {
    display: block;
    max-width: 100%;
    image-rendering: pixelated;
    height: auto;
  }
  .event-nocrop {
    padding: 16px 18px;
    font-size: 12px;
    color: #475569;
    font-style: italic;
    border-top: 1px solid #1e233b;
  }

  /* ── States ── */
  #loading { 
    display: flex; 
    flex-direction: column; 
    align-items: center; 
    justify-content: center; 
    padding: 60px 20px; 
    color: #3b82f6; 
    font-size: 13px; 
    font-weight: 500;
  }
  .spinner {
    width: 24px;
    height: 24px;
    border: 3px solid rgba(59, 130, 246, 0.15);
    border-top-color: #3b82f6;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 12px;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  #empty { 
    color: #475569; 
    padding: 40px 20px; 
    font-size: 13px; 
    font-style: italic;
    text-align: center;
  }
</style>
</head>
<body>

<nav id="sidebar">
  <div id="sidebar-header">
    <h2>Leaderboard</h2>
    <input id="search" type="text" placeholder="Search player…" autocomplete="off">
  </div>
  <div id="player-list"></div>
</nav>

<main id="main">
  <div id="main-header">
    <div>
      <h1 id="main-title">TesseractApexOCR Viewer</h1>
      <div id="no-player">← Select a player to browse their crop images</div>
    </div>
  </div>
  <div id="crop-area">
    <div id="loading" style="display:none">
      <div class="spinner"></div>
      <span>Loading crop events…</span>
    </div>
    <div id="empty" style="display:none">No crop events found for this player.</div>
    <div id="events"></div>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let allPlayers = [];
let activePlayer = null;

// ── Load player list ──────────────────────────────────────────────────────
async function loadPlayers() {
  const resp = await fetch('/api/players?limit=500');
  const data = await resp.json();
  if (data.error) {
    const el = $('player-list');
    el.innerHTML = `
      <div style="padding: 32px 16px; text-align: center; color: #f87171;">
        <div style="font-size: 28px; margin-bottom: 8px;">⚠️</div>
        <div style="font-size: 13px; font-weight: 600; margin-bottom: 4px;">Database Error</div>
        <div style="font-size: 11px; color: #64748b; line-height: 1.4;">${esc(data.error)}</div>
      </div>`;
    return;
  }
  allPlayers = data.players || [];
  renderPlayers(allPlayers);
}

function renderPlayers(list) {
  const el = $('player-list');
  el.innerHTML = '';
  list.forEach((p) => {
    // Find actual rank from allPlayers so searching doesn't change ELO rank
    const actualRank = allPlayers.findIndex(x => x.player === p.player) + 1;
    const div = document.createElement('div');
    div.className = 'player-item' + (p.player === activePlayer ? ' active' : '');
    div.dataset.player = p.player;
    div.innerHTML = `
      <span class="player-rank">#${actualRank}</span>
      <span class="player-name" title="${esc(p.player)}">${esc(p.player)}</span>
      <span class="player-elo">${Math.round(p.elo)}</span>`;
    div.addEventListener('click', () => selectPlayer(p.player, div));
    el.appendChild(div);
  });
}

$('search').addEventListener('input', function() {
  const q = this.value.toLowerCase().trim();
  renderPlayers(q ? allPlayers.filter(p => p.player.toLowerCase().includes(q)) : allPlayers);
});

// ── Select player ─────────────────────────────────────────────────────────
async function selectPlayer(name, clickedEl) {
  activePlayer = name;
  document.querySelectorAll('.player-item').forEach(el => el.classList.remove('active'));
  if (clickedEl) clickedEl.classList.add('active');

  $('main-title').textContent = name;
  $('no-player').style.display = 'none';
  $('loading').style.display = 'flex';
  $('empty').style.display = 'none';
  $('events').innerHTML = '';

  const resp = await fetch(`/api/player/${encodeURIComponent(name)}?limit=120`);
  const data = await resp.json();

  $('loading').style.display = 'none';

  const events = data.events || [];
  if (!events.length) { $('empty').style.display = 'block'; return; }

  const statsEl = $('no-player');
  const player = allPlayers.find(p => p.player === name);
  if (player) {
    statsEl.style.display = 'block';
    statsEl.textContent =
      `ELO ${Math.round(player.elo)}  ·  ${player.matches_played} matches  ·  ` +
      `K ${player.total_kills}  D ${player.total_deaths}`;
  }

  const container = $('events');
  events.forEach(ev => container.appendChild(buildEventCard(ev)));
}

// ── Build event card ──────────────────────────────────────────────────────
function buildEventCard(ev) {
  const card = document.createElement('div');
  card.className = 'event-card';

  const roleBadge = `<span class="badge badge-${ev.role}">${ev.role}</span>`;
  const typeBadge = `<span class="badge badge-${ev.event_type.toLowerCase()}">${ev.event_type}</span>`;
  const gemBadge  = ev.gemini_corrected ? `<span class="badge badge-gemini">Gemini</span>` : '';
  const opponent  = ev.role === 'attacker' ? ev.victim : ev.attacker;
  const conf      = ev.role === 'attacker' ? ev.attacker_conf : ev.victim_conf;
  const confStr   = conf != null ? `conf ${(+conf).toFixed(2)}` : '';
  const oppStr    = opponent ? `<span class="meta-vs">vs</span> <span class="meta-name">${esc(opponent)}</span>` : '';

  card.innerHTML = `
    <div class="event-meta">
      <span class="meta-ts">${esc(ev.timestamp)}</span>
      ${typeBadge}${roleBadge}${gemBadge}
      ${oppStr}
      <span class="meta-conf">${confStr}</span>
      <span class="meta-name">${esc(ev.streamer)}</span>
      <span class="meta-text" title="${esc(ev.raw_text || '')}">${esc(ev.raw_text || '')}</span>
    </div>`;

  if (!ev.crops || !ev.crops.length) {
    card.innerHTML += `<div class="event-nocrop">No saved crop for this event (not ranked, dedup'd, or non-English)</div>`;
    return card;
  }

  const container = document.createElement('div');
  container.className = 'crops-container';

  ev.crops.forEach(c => {
    const base      = encodeURIComponent(c.streamer_dir) + '/' + encodeURIComponent(c.filename);
    const colorUrl  = `/img/${base}/color`;
    const procUrl   = `/img/${base}`;
    const colorLabel = c.has_color ? 'On screen — colour' : 'On screen — approx';
    const colorDesc  = c.has_color
      ? 'Raw colour frame crop from stream'
      : 'Colour-inverted from saved crop (pre-colour-saving data)';

    const pair = document.createElement('div');
    pair.className = 'crop-pair';
    pair.innerHTML = `
      <div class="crop-panel">
        <div class="crop-label label-before">${colorLabel}</div>
        <div class="crop-desc">${colorDesc}</div>
        <div class="crop-img-wrap">
          <img src="${colorUrl}" alt="on screen" loading="lazy">
        </div>
      </div>
      <div class="crop-panel">
        <div class="crop-label label-after">TrOCR input — preprocessed</div>
        <div class="crop-desc">${esc(c.filename)}</div>
        <div class="crop-img-wrap">
          <img src="${procUrl}" alt="processed" loading="lazy">
        </div>
      </div>`;
    container.appendChild(pair);
  });

  card.appendChild(container);
  return card;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadPlayers();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML


# ---------------------------------------------------------------------------
# CLI + startup
# ---------------------------------------------------------------------------
def main() -> None:
    global CROPS_DIR, DB_PATH, ELO_PATH

    p = argparse.ArgumentParser(description="TesseractApexOCR visual crop browser")
    p.add_argument("--port",      type=int, default=8081)
    p.add_argument("--crops-dir", default=str(Path("crops")), metavar="PATH")
    p.add_argument("--db",        default=str(_KDB), metavar="PATH")
    p.add_argument("--elo-db",    default=str(_EDB), dest="elo_db", metavar="PATH")
    args = p.parse_args()

    CROPS_DIR = Path(args.crops_dir)
    DB_PATH   = Path(args.db)
    ELO_PATH  = Path(args.elo_db)

    if not CROPS_DIR.exists():
        print(f"[viewer] Warning: crops directory not found: {CROPS_DIR}")
    if not DB_PATH.exists():
        print(f"[viewer] Warning: killfeed.db not found: {DB_PATH}")
    if not ELO_PATH.exists():
        print(f"[viewer] Warning: elo.db not found: {ELO_PATH}")

    print(f"[viewer] Starting at http://localhost:{args.port}")
    print(f"[viewer] crops: {CROPS_DIR}  db: {DB_PATH}  elo: {ELO_PATH}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
