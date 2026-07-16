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
import db_log
from config import *

# Allow env-var overrides for paths and server settings
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
    total = db_log.count_events()
    return {
        "status": "ok",
        "db_exists": KILLFEED_DB_PATH.exists(),
        "total_events": total,
    }


@app.get("/events")
def get_events(
    streamer: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    attacker: Optional[str] = Query(None),
    victim: Optional[str] = Query(None),
    from_ts: Optional[str] = Query(None, description="ISO datetime, e.g. 2026-03-04T20:00:00"),
    to_ts: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="'trocr' or 'gemini'"),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    events = db_log.query_events(
        streamer=streamer, event_type=event_type,
        attacker=attacker, victim=victim,
        from_ts=from_ts, to_ts=to_ts,
        source=source,
        limit=limit, offset=offset,
    )
    total = db_log.count_events(streamer=streamer, event_type=event_type, source=source)
    return {"total": total, "offset": offset, "limit": limit, "events": events}


@app.get("/events/latest")
def get_latest_events(
    streamer: Optional[str] = Query(None),
    n: int = Query(10, ge=1, le=500),
):
    events = db_log.query_events(
        streamer=streamer, order_desc=True, limit=n
    )
    return {"events": list(reversed(events))}


@app.get("/events/kills")
def get_kills(
    streamer: Optional[str] = Query(None),
    attacker: Optional[str] = Query(None),
    victim: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    events = db_log.query_events(
        streamer=streamer, event_type="Kill",
        attacker=attacker, victim=victim,
        source=source,
        limit=limit, offset=offset,
    )
    total = db_log.count_events(streamer=streamer, event_type="Kill", source=source)
    return {"total": total, "offset": offset, "limit": limit, "events": events}


@app.get("/stats/streamers")
def stats_streamers():
    import sqlite3
    conn = sqlite3.connect(str(KILLFEED_DB_PATH), check_same_thread=False)
    rows = conn.execute(
        "SELECT streamer, COUNT(*) as event_count FROM events GROUP BY streamer ORDER BY streamer"
    ).fetchall()
    conn.close()
    return {"streamers": [{"streamer": r[0], "event_count": r[1]} for r in rows]}


@app.get("/stats/players")
def stats_players(streamer: Optional[str] = Query(None)):
    import sqlite3
    conn = sqlite3.connect(str(KILLFEED_DB_PATH), check_same_thread=False)
    where = "WHERE event_type='Kill'" + (f" AND LOWER(streamer)=LOWER('{streamer}')" if streamer else "")
    kill_rows  = conn.execute(f"SELECT attacker, COUNT(*) FROM events {where} GROUP BY attacker").fetchall()
    death_rows = conn.execute(f"SELECT victim,   COUNT(*) FROM events {where} GROUP BY victim").fetchall()
    conn.close()
    kill_counts  = {r[0]: r[1] for r in kill_rows  if r[0]}
    death_counts = {r[0]: r[1] for r in death_rows if r[0]}
    all_players = set(kill_counts) | set(death_counts)
    rows = [
        {"player": p, "kills": kill_counts.get(p, 0), "deaths": death_counts.get(p, 0)}
        for p in sorted(all_players)
    ]
    rows.sort(key=lambda r: r["kills"], reverse=True)
    return {"players": rows}


@app.get("/stats/victims")
def stats_victims(streamer: Optional[str] = Query(None)):
    import sqlite3
    conn = sqlite3.connect(str(KILLFEED_DB_PATH), check_same_thread=False)
    where = "WHERE event_type='Kill'" + (f" AND LOWER(streamer)=LOWER('{streamer}')" if streamer else "")
    rows = conn.execute(
        f"SELECT victim, COUNT(*) as death_count FROM events {where} GROUP BY victim ORDER BY death_count DESC"
    ).fetchall()
    conn.close()
    return {"victims": [{"victim": r[0], "death_count": r[1]} for r in rows if r[0]]}

@app.get("/gemini-stats")
def gemini_stats():
    """Live stats from the async Gemini validation queue.

    Shows how many crops have been validated, the TrOCR agreement rate,
    and how many corrections have been saved as training data.
    Only available while ocr.py is running in the same process (or shared memory).
    """
    try:
        from gemini_queue import get_queue
        return get_queue().get_stats()
    except Exception:
        return {
            "validated": 0,
            "agreed": 0,
            "corrections": 0,
            "dropped": 0,
            "agree_rate": 0.0,
            "queue_size": 0,
            "note": "Gemini queue not active (ocr.py not running in this process)",
        }


# ---------------------------------------------------------------------------
# ELO / Rankings endpoints
# ---------------------------------------------------------------------------

@app.get("/rankings")
def get_rankings(
    min_matches: int = Query(5, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Player leaderboard ranked by the conservative Glicko-2 estimate (mu - 2*rd), descending.

    Defaults to min_matches=5 so the board shows only players with enough games to have earned a
    rating; unproven players are 'provisional' (pass min_matches=1 to include them). Match-count is
    the gate because rd does not converge under this dataset's sparse per-match coverage."""
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
