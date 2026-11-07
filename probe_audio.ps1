param(
    [string]$OutFile = ""
)
$ErrorActionPreference = "Stop"
$lines = @()
try {
    Add-Type -AssemblyName System.Speech
    $recs = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
    foreach ($ri in $recs) {
        $lines += ("REC|" + $ri.Id + "|" + $ri.Culture.Name + "|" + $ri.Description)
    }
} catch {
    $lines += ("REC_ERR|" + $_.Exception.Message)
}
try {
    $v = New-Object -ComObject SAPI.SpVoice
    foreach ($t in $v.GetVoices()) {
        $lines += ("VOICE|" + $t.GetDescription())
    }
} catch {
    $lines += ("VOICE_ERR|" + $_.Exception.Message)
}
if ($OutFile -ne "") {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($OutFile, $lines, $enc)
}
$lines | ForEach-Object { Write-Output $_ }
