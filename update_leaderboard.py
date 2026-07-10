"""Refresh apex_ranked_leaderboard.csv from apexlegendsstatus.com's live ranked leaderboard.

The site sits behind Cloudflare's JS challenge, so plain HTTP clients get 403 -- a real
browser engine is required. This drives Edge (preinstalled on Windows) headlessly via
Selenium, waits for the table to render, and extracts (rank, player, RP) rows.

Usage:
    python update_leaderboard.py                # refresh if CSV older than MAX_AGE_HOURS
    python update_leaderboard.py --force        # refresh regardless of age
    python update_leaderboard.py --check        # exit 0 if fresh, 1 if stale (no scrape)

The CSV feeds database.py::load_top_players, which seeds every listed name as a
protected 'pro' entry so OCR fuzzy-matching never merges garbles INTO a pro name.
Refreshing only ever ADDS names to the player DB -- names that fell off the board stay
protected (historical matches reference them).
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

URL = "https://apexlegendsstatus.com/live-ranked-leaderboards/Battle_Royale/PC"
CSV_PATH = Path("apex_ranked_leaderboard.csv")
MIN_ROWS_SANE = 200   # refuse to overwrite the CSV with a suspiciously short scrape

try:
    from config import LEADERBOARD_MAX_AGE_HOURS as MAX_AGE_HOURS
except Exception:
    MAX_AGE_HOURS = 24

# Windows console UTF-8 (player names include CJK/Cyrillic)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def csv_age_hours() -> float:
    if not CSV_PATH.exists():
        return float("inf")
    return (time.time() - CSV_PATH.stat().st_mtime) / 3600.0


def scrape() -> list[tuple[int, str, int]]:
    """Drive a HEADED Edge window (Cloudflare's challenge never clears in headless mode,
    verified 2026-07-05) to the leaderboard, expand the client-side DataTable to show all
    rows, and extract (rank, player, RP).

    Page structure (verified): #liveTable rows with cells
      [avatar] [#rank (+rank-change)] [name \n status lines...] [RP \n RP-change]
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.support.ui import WebDriverWait

    opts = Options()
    opts.add_argument("--window-size=1400,900")
    # Cloudflare fingerprints obvious automation markers; these keep Edge looking normal.
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = webdriver.Edge(options=opts)
    try:
        driver.set_page_load_timeout(60)
        driver.get(URL)
        # Cloudflare interstitials for a few seconds before the real page loads.
        WebDriverWait(driver, 45).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "#liveTable tbody tr")) > 10
        )
        # The table is a client-side DataTable paginated at 50/page with ALL rows in its
        # data model -- switch it to show everything, then read the DOM once.
        driver.execute_script("$('#liveTable').DataTable().page.len(-1).draw(false);")
        WebDriverWait(driver, 20).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "#liveTable tbody tr")) > 100
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "#liveTable tbody tr")

        players: list[tuple[int, str, int]] = []
        seen_ranks: set[int] = set()
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 4:
                continue
            rank_txt = (cells[1].text or "").splitlines()
            m_rank = re.search(r"#\s*(\d+)", rank_txt[0] if rank_txt else "")
            if not m_rank:
                continue
            rank = int(m_rank.group(1))
            if rank in seen_ranks:
                continue
            name_lines = (cells[2].text or "").splitlines()
            name = name_lines[0].strip() if name_lines else ""
            rp_lines = (cells[3].text or "").splitlines()
            rp_digits = re.sub(r"[^\d]", "", rp_lines[0]) if rp_lines else ""
            if not name or not rp_digits:
                continue
            seen_ranks.add(rank)
            players.append((rank, name, int(rp_digits)))

        players.sort(key=lambda p: p[0])
        return players
    finally:
        driver.quit()


def write_csv(players: list[tuple[int, str, int]]) -> None:
    if CSV_PATH.exists():
        backup = CSV_PATH.with_suffix(f".csv.bak_{datetime.now():%Y%m%d}")
        backup.write_bytes(CSV_PATH.read_bytes())
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Player", "RP"])
        for rank, name, rp in players:
            # commas inside names are csv-quoted automatically; database.py handles both
            w.writerow([rank, name, rp])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="refresh regardless of CSV age")
    ap.add_argument("--check", action="store_true", help="only report freshness, no scrape")
    args = ap.parse_args()

    age = csv_age_hours()
    if args.check:
        fresh = age <= MAX_AGE_HOURS
        print(f"CSV age: {age:.1f}h ({'fresh' if fresh else 'stale'}, max {MAX_AGE_HOURS}h)")
        return 0 if fresh else 1

    if not args.force and age <= MAX_AGE_HOURS:
        print(f"CSV is {age:.1f}h old (max {MAX_AGE_HOURS}h) -- nothing to do. Use --force to refresh anyway.")
        return 0

    print(f"Scraping {URL} ...", flush=True)
    try:
        players = scrape()
    except Exception as e:
        print(f"Scrape FAILED ({type(e).__name__}: {e}) -- keeping the existing CSV.")
        return 1

    if len(players) < MIN_ROWS_SANE:
        print(f"Scrape returned only {len(players)} rows (< {MIN_ROWS_SANE}) -- keeping the existing CSV.")
        return 1

    old_names: set[str] = set()
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8") as f:
            next(f, None)
            for line in f:
                parts = line.rstrip("\n").split(",")
                if len(parts) >= 2:
                    old_names.add(",".join(parts[1:-1]) if len(parts) > 3 else parts[1])

    write_csv(players)
    new_names = {name for _, name, _ in players} - old_names
    print(f"Wrote {len(players)} players to {CSV_PATH} ({len(new_names)} names not in previous list).")
    if new_names:
        sample = sorted(new_names)[:10]
        print("  new e.g.:", ", ".join(sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())
