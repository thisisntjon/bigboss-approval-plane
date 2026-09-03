# One-time: add <repo>\scripts to the USER PATH so `bigboss <command>` resolves from any directory.
# Idempotent — does nothing if already present. Only touches the current user's PATH.
$scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }

$entries = $userPath.Split(';') | Where-Object { $_ -ne '' }
if ($entries -contains $scriptsDir) {
    Write-Host "Already on PATH: $scriptsDir"
} else {
    $newPath = ($userPath.TrimEnd(';') + ';' + $scriptsDir).TrimStart(';')
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Host "Added to your user PATH: $scriptsDir"
    Write-Host "Open a NEW terminal, then run:  bigboss ps"
}
