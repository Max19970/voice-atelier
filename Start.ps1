#Requires -Version 5.1
[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8765, [switch]$NoBrowser, [switch]$Offline, [ValidateSet('auto','cuda','cpu')][string]$Device = 'auto')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup.cmd first. Python will be installed inside this project.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
$env:VOICE_ATELIER_DEVICE = $Device
if ($Offline) { $env:VOICE_ATELIER_OFFLINE = '1' }
$env:HF_HOME = Join-Path $root 'data\cache\huggingface'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
$env:GRADIO_ANALYTICS_ENABLED = 'False'
$env:DO_NOT_TRACK = '1'
$env:TORCH_HOME = Join-Path $root 'data\cache\torch'
$env:NUMBA_CACHE_DIR = Join-Path $root 'data\cache\numba'
$url = "http://127.0.0.1:$Port"
# Do not open or reuse an unrelated service which happens to occupy our port.
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
try { $listener.Start() } catch { throw "Port $Port is busy. Close the existing studio or use Start.ps1 -Port 8766." } finally { $listener.Stop() }
Write-Host "Voice Atelier: $url" -ForegroundColor Cyan
Write-Host 'Stop with Ctrl+C or close this console. Voice data stays in the project data directory.'
$server = Start-Process -FilePath $python -ArgumentList @('-m','studio','--port',"$Port") -WorkingDirectory $root -NoNewWindow -PassThru
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        if ($server.HasExited) { throw "Studio exited during startup (exit $($server.ExitCode))." }
        try {
            $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 1
            if ($health.ok -and $health.version) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) { throw 'Studio did not become ready. See the console output.' }
    if (-not $NoBrowser) { Start-Process $url }
    $server.WaitForExit()
    if ($server.ExitCode -ne 0) { throw "Studio exited (exit $($server.ExitCode))." }
} finally {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -ErrorAction SilentlyContinue }
    $server.Dispose()
}
