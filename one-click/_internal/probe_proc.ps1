param(
    [string]$OutFile = ""
)
$ErrorActionPreference = "SilentlyContinue"
$lines = @()
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='Siri-OS.exe'" | ForEach-Object {
    $lines += ("PROC|" + $_.ProcessId + "|" + $_.CommandLine)
}
if ($OutFile -ne "") {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($OutFile, $lines, $enc)
}
$lines | ForEach-Object { Write-Output $_ }
