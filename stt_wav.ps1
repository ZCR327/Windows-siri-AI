param(
    [string]$WavFile = "",
    [string]$Language = "zh-CN",
    [string]$OutFile = ""
)
$ErrorActionPreference = "Stop"
try {
    Add-Type -AssemblyName System.Speech
    $ci = [System.Globalization.CultureInfo]::new($Language)
    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine -ArgumentList $ci
    $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
    $engine.SetInputToWaveFile($WavFile)
    $deadline = (Get-Date).AddSeconds(30)
    $text = ""
    while ((Get-Date) -lt $deadline) {
        $r = $engine.Recognize([TimeSpan]::FromSeconds(6))
        if ($r -ne $null) { $text = $r.Text; break }
    }
    try { $engine.Dispose() } catch { }
    if ($OutFile -ne "") {
        $enc = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($OutFile, $text, $enc)
    }
    Write-Output ("OK len=" + $text.Length)
} catch {
    if ($OutFile -ne "") {
        try {
            $enc = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($OutFile, "", $enc)
        } catch { }
    }
    Write-Output ("ERR: " + $_.Exception.Message)
}
