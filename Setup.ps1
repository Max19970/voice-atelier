#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$RuntimeOnly,
    [switch]$BootstrapOnly,
    [switch]$CpuOnly,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = $PSScriptRoot
if (-not [Environment]::Is64BitOperatingSystem) { throw '64-bit Windows is required.' }
Set-Location -LiteralPath $root
$tools = Join-Path $root '.tools'
$uvDir = Join-Path $tools 'uv'
$uv = Join-Path $uvDir 'uv.exe'
$python = Join-Path $root '.venv\Scripts\python.exe'
$uvVersion = '0.13.6'
$uvSha256 = 'a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f'
foreach ($dir in @($tools, $uvDir, (Join-Path $tools 'tmp'), (Join-Path $root '.cache\uv'))) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
# Project-local tools/caches only: no PATH, registry, system Python, or admin changes.
$env:UV_CACHE_DIR = Join-Path $root '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $tools 'bin'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_PYTHON_NO_REGISTRY = '1'
$env:UV_NO_CONFIG = '1'
$env:UV_LINK_MODE = 'copy'
$env:UV_HTTP_TIMEOUT = '300'
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
$env:TEMP = Join-Path $tools 'tmp'
$env:TMP = $env:TEMP
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Invoke-Uv {
    param([string[]]$UvArgs)
    & $uv @UvArgs
    if ($LASTEXITCODE -ne 0) { throw "uv failed (exit $LASTEXITCODE): $($UvArgs -join ' ')" }
}
if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
    Write-Host "Downloading verified portable uv $uvVersion..." -ForegroundColor Cyan
    $zip = Join-Path $tools "uv-$uvVersion.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip -TimeoutSec 300
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $uvSha256) { throw "uv archive checksum mismatch; refusing to execute. Expected $uvSha256, got $actual" }
    Expand-Archive -LiteralPath $zip -DestinationPath $uvDir -Force
    if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) { throw 'Portable uv archive has an unexpected layout.' }
}
Write-Host 'Installing isolated Python 3.11.18 (no system changes)...' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Invoke-Uv -UvArgs @('python', 'install', '3.11.18')
    Invoke-Uv -UvArgs @('venv', '--managed-python', '--python', '3.11.18', (Join-Path $root '.venv'))
}
& $python -c 'import sys; assert sys.version_info[:2] == (3,11), "Expected Python 3.11"; print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw 'Unexpected or broken Python environment; existing files were left unchanged.' }
if ($BootstrapOnly) {
    Write-Host 'Portable Python is ready; no packages or model weights were installed.' -ForegroundColor Green
    return
}
Write-Host 'Installing studio runtime and tests...' -ForegroundColor Cyan
Invoke-Uv -UvArgs @('pip', 'install', '--python', $python, '-r', (Join-Path $root 'requirements.txt'))
if (-not $RuntimeOnly) {
    Write-Host 'Installing local synthesis engine (several GB, NOT model weights)...' -ForegroundColor Cyan
    $index = 'https://download.pytorch.org/whl/cu126'
    $torchVersion = '2.7.1+cu126'
    if ($CpuOnly) { $index = 'https://download.pytorch.org/whl/cpu'; $torchVersion = '2.7.1+cpu' }
    Invoke-Uv -UvArgs @('pip', 'install', '--python', $python, "torch==$torchVersion", "torchaudio==$torchVersion", '--index-url', $index)
    Invoke-Uv -UvArgs @('pip', 'install', '--python', $python, '-r', (Join-Path $root 'requirements-engine.txt'))
    & $python -c 'import torch; from qwen_tts import Qwen3TTSModel; print("PyTorch:",torch.__version__,"CUDA:",torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU only; synthesis is slow")'
    if ($LASTEXITCODE -ne 0) { throw 'Engine import verification failed. See the error above; no model was downloaded.' }
}
Invoke-Uv -UvArgs @('pip', 'check', '--python', $python)
if (-not $SkipTests) {
    & $python -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Studio tests failed; installation files were retained for diagnosis.' }
}
Write-Host ''
Write-Host 'Ready. Run Start.cmd. Model weights must be downloaded explicitly in the Models tab.' -ForegroundColor Green
if ($RuntimeOnly) { Write-Host 'Runtime-only installation: run Setup.ps1 again without -RuntimeOnly to enable synthesis.' -ForegroundColor Yellow }
