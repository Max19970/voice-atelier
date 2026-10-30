#Requires -Version 5.1
[CmdletBinding()]
param([switch]$DownloadApproved, [switch]$SkipBrowser, [switch]$TestsOnly)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$uv = Join-Path $root '.tools\uv\uv.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup.ps1 first.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
$env:VOICE_ATELIER_DEVICE = 'cuda'
$env:HF_HOME = Join-Path $root 'data\cache\huggingface'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
$env:GRADIO_ANALYTICS_ENABLED = 'False'
$env:DO_NOT_TRACK = '1'
$env:TORCH_HOME = Join-Path $root 'data\cache\torch'
$env:NUMBA_CACHE_DIR = Join-Path $root 'data\cache\numba'
$env:UV_CACHE_DIR = Join-Path $root '.cache\uv'
$env:UV_LINK_MODE = 'copy'
$env:UV_NO_CONFIG = '1'
New-Item -ItemType Directory -Path (Join-Path $root 'verification') -Force | Out-Null
& $uv pip freeze --python $python | Out-File -FilePath (Join-Path $root 'verification\requirements-resolved.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Could not record installed dependency versions.' }
Write-Host 'Running backend regression tests...' -ForegroundColor Cyan
& $python -m pytest tests -q '--junitxml=verification/pytest.xml'
if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed. GPU/model tests were not started.' }
& $python 'verification/check_i18n.py'
if ($LASTEXITCODE -ne 0) { throw 'RU/EN localization audit failed. Live tests were not started.' }
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    & $node.Source 'verification/check_design_presets.mjs'
    if ($LASTEXITCODE -ne 0) { throw 'Voice Design preset/state regression tests failed.' }
    foreach ($file in @('web/app.js','web/js/api.js','web/js/audio.js','web/js/i18n.js','web/js/state.js','web/js/ui.js','web/js/util.js','web/js/views.js','web/js/design-presets.js','web/js/design-state.js')) {
        if (Test-Path -LiteralPath $file) {
            & $node.Source --check $file
            if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $file" }
        }
    }
} else {
    Write-Warning 'Node is unavailable: JavaScript preset/state tests and syntax checks were not run.'
}
if ($TestsOnly) { return }
if (-not $SkipBrowser) {
    Write-Host 'Installing project-local browser test library (no browser download)...' -ForegroundColor Cyan
    & $uv pip install --python $python 'playwright==1.64.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install browser verification library.' }
}
$smokeArgs = @('verification/smoke.py')
if ($DownloadApproved) { $smokeArgs += '--download-approved' }
if (-not $SkipBrowser) { $smokeArgs += '--browser' }
Write-Host 'Running real local integration/GPU smoke test...' -ForegroundColor Cyan
& $python @smokeArgs
if ($LASTEXITCODE -ne 0) { throw 'Integration smoke failed. Read verification reports and server.log.' }
if (-not $SkipBrowser) {
    & $python -c 'import json; r=json.load(open("verification/artifacts/browser/browser-report.json",encoding="utf-8")); assert r["summary"]["verdict"] == "PASS", r["summary"]; print("Browser verdict: PASS")'
    if ($LASTEXITCODE -ne 0) { throw 'Browser smoke is not PASS. Audio and browser verdicts remain separate in the reports.' }
}
Write-Host 'Verification finished. Reports do not establish perceptual voice quality.' -ForegroundColor Green
