"""FastAPI REST server exposing killfeed_log.csv data.

Run:
    python api.py
    → http://localhost:8080
    → http://localhost:8080/docs  (auto-generated Swagger UI)
"""

import csv
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import elo_db
from config import *

# Allow env-var overrides for paths and server settings
_LOG_PATH  = Path(os.environ.get("LOG_PATH",  str(LOG_PATH)))
_API_HOST  = os.environ.get("API_HOST", "0.0.0.0")
_API_PORT  = int(os.environ.get("API_PORT", "8080"))

app = FastAPI(title="TesseractApexOCR API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# CSV cache — re-reads only when file modification time changes
# ---------------------------------------------------------------------------

_cache_lock   = threading.Lock()
_cache_events: list[dict] = []
_cache_mtime:  float = 0.0


def _load_events() -> list[dict]:
    global _cache_events, _cache_mtime
    if not _LOG_PATH.exists():
        return []
    mtime = _LOG_PATH.stat().st_mtime
    with _cache_lock:
        if mtime != _cache_mtime:
            with _LOG_PATH.open(encoding="utf-8") as f:
                _cache_events = list(csv.DictReader(f))
            _cache_mtime = mtime
        return list(_cache_events)


# ---------------------------------------------------------------------------
# Periodic ELO reprocessing — runs reprocess.py logic every 30 minutes
# ---------------------------------------------------------------------------

def _reprocess_loop():
    import subprocess, sys
    reprocess_script = Path(__file__).parent / "reprocess.py"
    while True:
        time.sleep(1800)
        try:
            subprocess.run([sys.executable, str(reprocess_script), "--dedupe"], timeout=300)
        except Exception:
            pass


threading.Thread(target=_reprocess_loop, daemon=True, name="elo-reprocess").start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _filter_events(
    events: list[dict],
    streamer: Optional[str],
    event_type: Optional[str],
    attacker: Optional[str],
    victim: Optional[str],
    from_ts: Optional[str],
    to_ts: Optional[str],
) -> list[dict]:
    result = events

    if streamer:
        result = [e for e in result if e.get("streamer", "").lower() == streamer.lower()]
    if event_type:
        result = [e for e in result if e.get("event_type", "").lower() == event_type.lower()]
    if attacker:
        result = [e for e in result if attacker.lower() in e.get("attacker", "").lower()]
    if victim:
        result = [e for e in result if victim.lower() in e.get("victim", "").lower()]

    if from_ts:
        try:
            from_dt = datetime.fromisoformat(from_ts)
            result = [e for e in result if (_parse_ts(e.get("timestamp", "")) or datetime.min) >= from_dt]
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid from_ts: {from_ts!r}")

    if to_ts:
        try:
            to_dt = datetime.fromisoformat(to_ts)
            result = [e for e in result if (_parse_ts(e.get("timestamp", "")) or datetime.max) <= to_dt]
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid to_ts: {to_ts!r}")

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    events = _load_events()
    return {
        "status": "ok",
        "log_exists": _LOG_PATH.exists(),
        "total_events": len(events),
    }


@app.get("/events")
def get_events(
    streamer: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    attacker: Optional[str] = Query(None),
    victim: Optional[str] = Query(None),
    from_ts: Optional[str] = Query(None, description="ISO datetime, e.g. 2026-03-04T20:00:00"),
    to_ts: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    events = _load_events()
    filtered = _filter_events(events, streamer, event_type, attacker, victim, from_ts, to_ts)
    total = len(filtered)
    page = filtered[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "events": page}


@app.get("/events/latest")
def get_latest_events(
    streamer: Optional[str] = Query(None),
    n: int = Query(10, ge=1, le=500),
):
    events = _load_events()
    if streamer:
        events = [e for e in events if e.get("streamer", "").lower() == streamer.lower()]
    return {"events": events[-n:]}


@app.get("/events/kills")
def get_kills(
    streamer: Optional[str] = Query(None),
    attacker: Optional[str] = Query(None),
    victim: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    events = _load_events()
    kills = [e for e in events if e.get("event_type", "").lower() == "kill"]
    filtered = _filter_events(kills, streamer, None, attacker, victim, None, None)
    total = len(filtered)
    page = filtered[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "events": page}


@app.get("/stats/streamers")
def stats_streamers():
    events = _load_events()
    counts: dict[str, int] = {}
    for e in events:
        s = e.get("streamer", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return {"streamers": [{"streamer": k, "event_count": v} for k, v in sorted(counts.items())]}


@app.get("/stats/players")
def stats_players(streamer: Optional[str] = Query(None)):
    events = _load_events()
    if streamer:
        events = [e for e in events if e.get("streamer", "").lower() == streamer.lower()]

    kill_counts: dict[str, int] = {}
    death_counts: dict[str, int] = {}

    for e in events:
        if e.get("event_type") != "kill":
            continue
        atk = e.get("attacker", "")
        vic = e.get("victim", "")
        if atk:
            kill_counts[atk] = kill_counts.get(atk, 0) + 1
        if vic:
            death_counts[vic] = death_counts.get(vic, 0) + 1

    all_players = set(kill_counts) | set(death_counts)
    rows = [
        {"player": p, "kills": kill_counts.get(p, 0), "deaths": death_counts.get(p, 0)}
        for p in sorted(all_players)
    ]
    rows.sort(key=lambda r: r["kills"], reverse=True)
    return {"players": rows}


@app.get("/stats/victims")
def stats_victims(streamer: Optional[str] = Query(None)):
    events = _load_events()
    if streamer:
        events = [e for e in events if e.get("streamer", "").lower() == streamer.lower()]

    victim_counts: dict[str, int] = {}
    for e in events:
        if e.get("event_type") != "kill":
            continue
        vic = e.get("victim", "")
        if vic:
            victim_counts[vic] = victim_counts.get(vic, 0) + 1

    rows = [{"victim": k, "death_count": v} for k, v in sorted(victim_counts.items(), key=lambda x: -x[1])]
    return {"victims": rows}


# ---------------------------------------------------------------------------
# ELO / Rankings endpoints
# ---------------------------------------------------------------------------

@app.get("/rankings")
def get_rankings(
    min_matches: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Player leaderboard sorted by ELO descending."""
    if not elo_db.ELO_DB_PATH.exists():
        raise HTTPException(status_code=503, detail="ELO database not found. Run reprocess.py first.")
    players = elo_db.get_rankings(limit=limit, offset=offset, min_matches=min_matches)
    total = elo_db.get_total_rankings_count(min_matches=min_matches)
    return {"total": total, "offset": offset, "limit": limit, "players": players}


@app.get("/rankings/{player}")
def get_player(player: str, history_limit: int = Query(50, ge=1, le=200)):
    """Single player's ELO, stats, and match history."""
    if not elo_db.ELO_DB_PATH.exists():
        raise HTTPException(status_code=503, detail="ELO database not found. Run reprocess.py first.")
    rating = elo_db.get_player_rating(player)
    if not rating:
        raise HTTPException(status_code=404, detail=f"Player {player!r} not found in ELO database.")
    history = elo_db.get_player_match_history(player, limit=history_limit)
    return {"player": rating, "match_history": history}


@app.get("/matches")
def get_matches(
    streamer: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List detected match sessions."""
    if not elo_db.ELO_DB_PATH.exists():
        raise HTTPException(status_code=503, detail="ELO database not found. Run reprocess.py first.")
    matches = elo_db.get_matches(streamer=streamer, limit=limit, offset=offset)
    return {"total": len(matches), "offset": offset, "limit": limit, "matches": matches}


@app.get("/matches/{match_id}")
def get_match(match_id: str):
    """Full detail for one match: kills in order + player placements with ELO changes."""
    if not elo_db.ELO_DB_PATH.exists():
        raise HTTPException(status_code=503, detail="ELO database not found. Run reprocess.py first.")
    match = elo_db.get_match(match_id)
    if not match:
        raise HTTPException(status_code=404, detail=f"Match {match_id!r} not found.")
    return match


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("api:app", host=_API_HOST, port=_API_PORT, reload=False)
