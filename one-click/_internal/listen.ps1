param(
    [string]$OutFile = "",
    [int]$TimeoutSec = 10,
    [string]$Language = "zh-CN"
)
$ErrorActionPreference = "Stop"
try {
    Add-Type -AssemblyName System.Speech
    Add-Type -ReferencedAssemblies System.Speech -TypeDefinition @"
using System;
using System.Globalization;
using System.Speech;
using System.Speech.Recognition;
using System.Threading;

public static class RecoWithLevel {
    static SpeechRecognitionEngine engine;
    static string result = "";

    public static string Run(int timeoutSec, string culture) {
        engine = new SpeechRecognitionEngine(new CultureInfo(culture));
        engine.LoadGrammar(new DictationGrammar());
        engine.SetInputToDefaultAudioDevice();
        // Warm-up: the very first AudioLevel read can block ~1s while the
        // audio engine spins up. Do it here so listening starts on time.
        try { float warm = engine.AudioLevel; } catch { }
        var sw = System.Diagnostics.Stopwatch.StartNew();

        // Recognition runs on a background thread (blocking).
        // Main thread polls AudioLevel and writes it to stdout in real time.
        var worker = new Thread(delegate() {
            try {
                var r = engine.Recognize(TimeSpan.FromSeconds(timeoutSec));
                if (r != null) { result = r.Text; }
            } catch (Exception e) {
                Console.WriteLine("REC_ERR " + e.GetType().Name);
            }
        });
        worker.IsBackground = true;
        worker.Start();

        while (sw.Elapsed.TotalSeconds < timeoutSec + 0.2) {
            float lvl = 0;
            try { lvl = engine.AudioLevel; } catch { }
            Console.WriteLine("LVL " + sw.Elapsed.TotalSeconds.ToString("F3") + " " + lvl.ToString("F2"));
            Console.Out.Flush();
            Thread.Sleep(40);
        }
        worker.Join(1200);
        try { engine.Dispose(); } catch { }
        return result;
    }
}
"@
    $text = [RecoWithLevel]::Run($TimeoutSec, $Language)
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
