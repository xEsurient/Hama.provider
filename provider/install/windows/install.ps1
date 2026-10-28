# Registers the HAMA provider as a Windows scheduled task that starts at boot (run in an elevated PowerShell).
#   powershell -ExecutionPolicy Bypass -File install\windows\install.ps1            install + start
#   powershell -ExecutionPolicy Bypass -File install\windows\install.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$TaskName = "HAMA Plex Provider"
$Dir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (files in $Dir kept)."
    exit 0
}

$Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $Python) { throw "Python 3.8+ not found in PATH. Install it from python.org (tick 'Add to PATH')." }
& $Python.Replace("pythonw.exe", "python.exe") -c "import sys; sys.exit(sys.version_info < (3, 8))"
if ($LASTEXITCODE -ne 0) { throw "Python 3.8+ is required." }

$Action   = New-ScheduledTaskAction -Execute $Python -Argument "`"$Dir\run.py`"" -WorkingDirectory $Dir
$Trigger  = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started '$TaskName'. Log: set log_file in config.json. Test: http://127.0.0.1:32550/tv"
Write-Host "Plex: Settings > Metadata Agents > Add Provider > http://127.0.0.1:32550/tv (and /movies)"
