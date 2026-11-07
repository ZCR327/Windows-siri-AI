# Siri-OS launcher. ASCII only on purpose: PS 5.1 reads UTF-8 no-BOM as ANSI.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here "siri_orb.py"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Output "python not found in PATH"; exit 1 }
$arg = [char]34 + $py + [char]34
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (Test-Path $pythonw) {
    Start-Process -FilePath $pythonw -ArgumentList $arg -WorkingDirectory $here
    Write-Output "Siri-OS started (pythonw). Tray icon should appear."
} else {
    Start-Process -FilePath $python -ArgumentList $arg -WorkingDirectory $here
    Write-Output "Siri-OS started."
}
