# restart.ps1 - Clean restart of the Apex OCR pipeline with schema migrate + ELO backfill.
#
# Run from the project root:  .\restart.ps1
#
# What it does:
#   1. Stops the running stack (ocr.py, api.py, elo_autorebuild.py, reprocess.py, cloudflared)
#      by matching command lines - leaves any unrelated Python processes untouched.
#   2. Migrates killfeed.db up front (adds icon_vote/read_count) so /audit works the moment the
#      API comes back.
#   3. Clean ELO rebuild (reprocess.py --reset --dedupe) - backfills match_kills.source_event_id
#      for ALL historical kills, so the audit trace works retroactively, not just for new events.
#   4. Relaunches the full stack via start.ps1 (api + tunnel + ocr, each with auto-restart).
#
# Switches:
#   -SkipRebuild   Skip step 3 (the multi-minute ELO rebuild). Use for a plain code-change restart
#                  once the historical backfill has already been done once.
#   -NoStart       Do stop + migrate (+ rebuild) but do NOT relaunch start.ps1. Useful for a
#                  maintenance window where you want to start the stack yourself afterwards.

param(
    [switch]$SkipRebuild,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Python = "$PSScriptRoot\.venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "[restart] venv python not found at $Python" -ForegroundColor Red
    Write-Host "[restart] Create the venv first, or edit the Python path in this script." -ForegroundColor Red
    exit 1
}

# --- 1. Stop the running stack ---
Write-Host "[restart] Stopping pipeline processes..." -ForegroundColor Cyan
$script:killed = 0
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'ocr\.py|api\.py|elo_autorebuild\.py|reprocess\.py' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $script:killed++ } catch {}
    }
Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $script:killed++ } catch {}
}
Write-Host "[restart] Stopped $($script:killed) process(es)." -ForegroundColor Green
Start-Sleep -Seconds 2   # let file handles (elo.db / killfeed.db WAL) release

# --- 2. Migrate killfeed.db (idempotent) ---
Write-Host "[restart] Migrating killfeed.db (icon_vote / read_count)..." -ForegroundColor Cyan
& $Python -c "import db_log; from config import KILLFEED_DB_PATH; db_log.init_db(KILLFEED_DB_PATH)"
if ($LASTEXITCODE -ne 0) { Write-Host "[restart] killfeed.db migration failed." -ForegroundColor Red; exit 1 }

# --- 3. Clean ELO rebuild - backfills source_event_id for all history ---
if ($SkipRebuild) {
    Write-Host "[restart] -SkipRebuild set: leaving elo.db as-is (no historical backfill)." -ForegroundColor Yellow
} else {
    Write-Host "[restart] Rebuilding elo.db (reprocess.py --reset --dedupe) - this takes a few minutes..." -ForegroundColor Cyan
    & $Python "$PSScriptRoot\reprocess.py" --reset --dedupe
    if ($LASTEXITCODE -ne 0) { Write-Host "[restart] ELO rebuild failed." -ForegroundColor Red; exit 1 }
    Write-Host "[restart] ELO rebuild complete." -ForegroundColor Green
}

# --- 4. Relaunch the stack ---
if ($NoStart) {
    Write-Host "[restart] -NoStart set: stack NOT relaunched. Run .\start.ps1 when ready." -ForegroundColor Yellow
    exit 0
}
Write-Host "[restart] Launching start.ps1 (api + tunnel + ocr)..." -ForegroundColor Cyan
& "$PSScriptRoot\start.ps1"
