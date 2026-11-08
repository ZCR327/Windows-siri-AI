param([int]$TimeoutMs = 12000)
# Windows location -> stdout "LAT=.. LON=.. ACC=.."
# ASCII only on purpose: the console codepage here is GBK and non-ASCII
# output would break the parsing on the python side.
$ErrorActionPreference = "SilentlyContinue"
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Devices.Geolocation.Geolocator,Windows.Devices.Geolocation,ContentType=WindowsRuntime]
    $loc = New-Object Windows.Devices.Geolocation.Geolocator
    try { $loc.DesiredAccuracyInMeters = 1000 } catch { }
    # NOTE: the type name contains a backtick - it MUST stay in single quotes,
    # in a double-quoted string the backtick would escape the "1" and never match.
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1)
    if ($asTask -eq $null) { Write-Output "ERR=no_as_task"; exit 1 }
    function AwaitOp($op, $type) {
        $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
        $null = $t.Wait($TimeoutMs)
        if (-not $t.IsCompleted) { return $null }
        return $t.Result
    }
    $pos = AwaitOp ($loc.GetGeopositionAsync()) ([Windows.Devices.Geolocation.Geoposition])
    if ($pos -eq $null) { Write-Output "ERR=timeout"; exit 1 }
    Write-Output ("LAT=" + $pos.Coordinate.Point.Position.Latitude)
    Write-Output ("LON=" + $pos.Coordinate.Point.Position.Longitude)
    Write-Output ("ACC=" + $pos.Coordinate.Accuracy)
} catch {
    Write-Output ("ERR=" + $_.Exception.Message)
    exit 1
}
