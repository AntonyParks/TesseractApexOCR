import sqlite3
from pathlib import Path
import sys
from datetime import datetime

# Add project root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")

def _split_chunk_recursive(kills_chunk: list[tuple[datetime, dict]], max_players: int = 62, max_duration: float = 1500.0) -> list[list[tuple[datetime, dict]]]:
    """Recursively split a grouped kills chunk if it exceeds player count or maximum duration bounds."""
    if len(kills_chunk) < 2:
        return [kills_chunk]

    # Count unique players observed
    seen_players = set()
    for _, d in kills_chunk:
        if d.get("attacker"):
            seen_players.add(d["attacker"])
        if d.get("victim"):
            seen_players.add(d["victim"])

    duration_sec = (kills_chunk[-1][0] - kills_chunk[0][0]).total_seconds()

    # If within unique player and duration bounds, no splitting needed
    if len(seen_players) < max_players and duration_sec < max_duration:
        return [kills_chunk]

    # Find the largest internal time gap
    max_gap = -1.0
    split_idx = -1
    for i in range(len(kills_chunk) - 1):
        gap = (kills_chunk[i + 1][0] - kills_chunk[i][0]).total_seconds()
        if gap > max_gap:
            max_gap = gap
            split_idx = i + 1

    if split_idx == -1:
        return [kills_chunk]

    # Split into left and right sub-chunks and recurse
    left = kills_chunk[:split_idx]
    right = kills_chunk[split_idx:]
    
    return (
        _split_chunk_recursive(left, max_players, max_duration) +
        _split_chunk_recursive(right, max_players, max_duration)
    )

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found")
        return
        
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT streamer, timestamp, attacker, victim, attacker_conf, victim_conf
        FROM events
        WHERE event_type = 'Kill'
          AND source = 'trocr'
        ORDER BY timestamp ASC
        """
    ).fetchall()
    conn.close()

    by_streamer = {}
    for (streamer, ts_str, attacker, victim, a_conf, v_conf) in rows:
        ts = _parse_ts(ts_str)
        by_streamer.setdefault(streamer, []).append((ts, {
            "attacker": attacker,
            "victim": victim,
            "attacker_conf": a_conf,
            "victim_conf": v_conf,
        }))
        
    print("=" * 80)
    print("TESTING DURATION-BASED STITCH SPLITTER ON REAL DB")
    print("=" * 80)
    
    total_matches = 0
    mega_matches_after = 0
    
    for streamer, events in by_streamer.items():
        events.sort(key=lambda x: x[0])
        
        # Group raw chunks using default gap_seconds = 90
        current_kills = []
        raw_chunks = []
        for ts, data in events:
            if current_kills and (ts - current_kills[-1][0]).total_seconds() > 90:
                raw_chunks.append(current_kills)
                current_kills = []
            current_kills.append((ts, data))
        if current_kills:
            raw_chunks.append(current_kills)
            
        # Run recursive splitter on each chunk
        for chunk in raw_chunks:
            if len(chunk) < 3: # min_kills = 3
                continue
            split_results = _split_chunk_recursive(chunk, max_players=62, max_duration=1500.0)
            
            for split_chunk in split_results:
                if len(split_chunk) < 3:
                    continue
                total_matches += 1
                dur = (split_chunk[-1][0] - split_chunk[0][0]).total_seconds()
                if len(split_chunk) >= 100:
                    mega_matches_after += 1
                    print(f"[!] Warning: Still got mega-match! Streamer: {streamer}, Kills: {len(split_chunk)}, Duration: {dur/60:.1f} mins")
                    
    print(f"\nSummary:")
    print(f"  - Total matches after split: {total_matches}")
    print(f"  - Mega-matches remaining:    {mega_matches_after}")

if __name__ == "__main__":
    main()
