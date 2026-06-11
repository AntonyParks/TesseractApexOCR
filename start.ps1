# start.ps1 — Launch and auto-restart ocr.py, api.py, and Cloudflare Tunnel
# Run from the project root: .\start.ps1
#
# Requirements:
#   - cloudflared.exe must be on PATH or in the project root
#     Download: https://github.com/cloudflare/cloudflared/releases/latest
#     (Windows AMD64: cloudflared-windows-amd64.exe → rename to cloudflared.exe)

$Python    = "$PSScriptRoot\.venv\Scripts\python.exe"
$OcrScript = "$PSScriptRoot\ocr.py"
$ApiScript = "$PSScriptRoot\api.py"

# Load .env for Vercel credentials
$EnvFile = "$PSScriptRoot\.env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | Where-Object { $_ -match "^\s*[^#].+=.+" } | ForEach-Object {
        $k, $v = $_ -split "=", 2
        [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
    }
}
$VercelToken      = $env:VERCEL_TOKEN
$VercelProjectId  = $env:VERCEL_PROJECT_ID
$VercelEnvId      = $env:VERCEL_ENV_ID
$VercelDeployHook = $env:VERCEL_DEPLOY_HOOK

# Find cloudflared — project root first, then PATH
$Cloudflared = "$PSScriptRoot\cloudflared.exe"
if (-not (Test-Path $Cloudflared)) {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { $Cloudflared = $cmd.Source } else { $Cloudflared = $null }
}
if (-not $Cloudflared) {
    Write-Host "[tunnel] cloudflared.exe not found. Download from:" -ForegroundColor Red
    Write-Host "  https://github.com/cloudflare/cloudflared/releases/latest" -ForegroundColor Red
    Write-Host "  Place cloudflared.exe in the project root or on PATH." -ForegroundColor Red
}

# ── api.py background job ──────────────────────────────────────────────────
$apiJob = Start-Job -ScriptBlock {
    param($py, $script, $dir)
    Set-Location $dir
    while ($true) {
        & $py $script
        Start-Sleep -Seconds 10
    }
} -ArgumentList $Python, $ApiScript, $PSScriptRoot -Name "apex-api"
Write-Host "[api] Started (job id $($apiJob.Id))" -ForegroundColor Cyan

# ── Cloudflare quick tunnel ────────────────────────────────────────────────
if ($Cloudflared) {
    $tunnelJob = Start-Job -ScriptBlock {
        param($cf)
        while ($true) {
            & $cf tunnel --url http://localhost:8080 2>&1
            Start-Sleep -Seconds 10
        }
    } -ArgumentList $Cloudflared -Name "apex-tunnel"
    Write-Host "[tunnel] Started (job id $($tunnelJob.Id))" -ForegroundColor Cyan
    Write-Host "[tunnel] Waiting for public URL (up to 30s)..." -ForegroundColor Yellow
    $tunnelUrl = $null
    for ($i = 0; $i -lt 30 -and -not $tunnelUrl; $i++) {
        Start-Sleep -Seconds 1
        $tunnelUrl = Receive-Job $tunnelJob | Where-Object { $_ -match "https://[^\s]+trycloudflare\.com" } | Select-Object -Last 1
    }
    if ($tunnelUrl) {
        if ($tunnelUrl -match "(https://[^\s]+trycloudflare\.com[^\s]*)") { $tunnelUrl = $Matches[1] }
        Write-Host "[tunnel] PUBLIC URL: $tunnelUrl" -ForegroundColor Green

        # Auto-update Vercel env var and trigger redeploy
        if ($VercelToken -and $VercelEnvId -and $VercelDeployHook) {
            Write-Host "[vercel] Updating NEXT_PUBLIC_API_URL..." -ForegroundColor Cyan
            $headers = @{ Authorization = "Bearer $VercelToken"; "Content-Type" = "application/json" }
            try {
                $body = @{ value = $tunnelUrl } | ConvertTo-Json
                Invoke-RestMethod -Method Patch `
                    -Uri "https://api.vercel.com/v9/projects/$VercelProjectId/env/$VercelEnvId" `
                    -Headers $headers -Body $body | Out-Null
                Invoke-RestMethod -Method Post -Uri $VercelDeployHook | Out-Null
                Write-Host "[vercel] Redeploy triggered. Site live in ~1 min: https://ocr-frontend-phi.vercel.app" -ForegroundColor Green
            } catch {
                Write-Host "[vercel] Update failed: $_" -ForegroundColor Red
            }
        }
    } else {
        Write-Host "[tunnel] URL not detected yet. Run: Receive-Job -Name apex-tunnel" -ForegroundColor Yellow
    }
}

# ── ocr.py foreground with auto-restart ───────────────────────────────────
try {
    while ($true) {
        Write-Host "[ocr] Starting..." -ForegroundColor Cyan
        & $Python $OcrScript
        Write-Host "[ocr] Exited. Restarting in 10s..." -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
} finally {
    Write-Host "Shutting down..." -ForegroundColor Yellow
    Stop-Job $apiJob;    Remove-Job $apiJob
    if ($Cloudflared) { Stop-Job $tunnelJob; Remove-Job $tunnelJob }
}
