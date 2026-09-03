# Create BigBoss desktop shortcuts (run once). Places clickable icons on your Desktop that open a
# console running the launcher / chat / dashboard. Idempotent (overwrites existing shortcuts).
$scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo   = Split-Path -Parent $scriptsDir
$menu   = Join-Path $scriptsDir 'BigBoss-Launcher.cmd'
$shim   = Join-Path $scriptsDir 'bigboss.cmd'
$desktop = [Environment]::GetFolderPath('Desktop')
$shell  = New-Object -ComObject WScript.Shell

function New-BB-Shortcut($name, $arguments, $iconPath) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$name.lnk"))
    $lnk.TargetPath = $env:ComSpec                      # cmd.exe
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $repo
    $lnk.IconLocation = $iconPath
    $lnk.Description = $name
    $lnk.Save()
    Write-Host "Created: $name.lnk"
}

# /k keeps the console open after the command; /c would close it.
New-BB-Shortcut 'BigBoss'           ("/k `"$menu`"")               "$env:SystemRoot\System32\shell32.dll,137"
New-BB-Shortcut 'BigBoss Chat'      ("/k `"$shim`" chat")          "$env:SystemRoot\System32\shell32.dll,13"
New-BB-Shortcut 'BigBoss Dashboard' ("/k `"$shim`" serve --port 8787") "$env:SystemRoot\System32\imageres.dll,153"

Write-Host ''
Write-Host "Done. Three icons are on your Desktop: BigBoss (menu), BigBoss Chat, BigBoss Dashboard."
