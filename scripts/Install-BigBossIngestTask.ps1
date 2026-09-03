# Install-BigBossIngestTask.ps1 — keep BigBoss's LAN registry-ingest server running.
#
# Registers a Scheduled Task ("BigBoss LAN Ingest") that runs `bigboss serve --lan` hidden in the
# background at logon, so the :8787 ingest survives closing the terminal AND reboot (the remote host's nightly
# 03:00 crawl can then POST its bundle instead of falling back to the file drop).
#
# Run once:   powershell -ExecutionPolicy Bypass -File scripts\Install-BigBossIngestTask.ps1
# Remove:     Unregister-ScheduledTask -TaskName "BigBoss LAN Ingest" -Confirm:$false
#
# No admin needed to RUN the server (the 8787 firewall rule already exists). Registering an S4U task
# MAY require an elevated PowerShell — if you see Access Denied, re-run this from an Administrator shell.

param(
    [int]$Port = 8787,
    [switch]$AtStartup   # also start at machine boot (before logon), not just at logon
)

$ErrorActionPreference = "Stop"
$taskName = "BigBoss LAN Ingest"
$repo = Split-Path -Parent $PSScriptRoot            # scripts\ -> repo root
$cmd  = Join-Path $repo "scripts\bigboss.cmd"

if (-not (Test-Path $cmd)) { throw "Launcher not found: $cmd (run from the BigBoss repo)." }

# cmd.exe /c "<launcher> serve --lan ..." — the launcher sets PYTHONPATH/UV_CACHE_DIR + repo-pin.
# Under S4U the console runs in a background session (no visible window). --no-open-pair keeps it headless.
$inner = '"' + $cmd + '" serve --lan --port ' + $Port + ' --no-open-pair'
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "' + $inner + '"') -WorkingDirectory $repo

$triggers = @(New-ScheduledTaskTrigger -AtLogOn)
if ($AtStartup) { $triggers += New-ScheduledTaskTrigger -AtStartup }

$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
                -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)

# Idempotent: drop any prior version first.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
        -Principal $principal -Settings $settings `
        -Description "Keeps BigBoss's LAN registry-ingest server (0.0.0.0:$Port) up for remote-host bundle delivery." | Out-Null
} catch {
    Write-Host "[X] Could not register the task: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "    S4U tasks often need elevation. Re-run this script from an Administrator PowerShell:" -ForegroundColor Yellow
    Write-Host ('    powershell -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath) -ForegroundColor Yellow
    exit 1
}

Start-ScheduledTask -TaskName $taskName
Write-Host "[OK] Registered + started '$taskName' (serve --lan on port $Port)." -ForegroundColor Green
Write-Host "     Verify:  netstat -ano | findstr :$Port      (expect 0.0.0.0:$Port LISTENING)"
Write-Host "     Health:  curl http://<this-machine-lan-ip>:$Port/api/health"
Write-Host "     Remove:  Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
