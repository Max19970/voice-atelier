@echo off
setlocal
cd /d "%~dp0"
echo Voice Atelier - project-local Python, CUDA runtime and dependencies.
echo Internet is required. Several GB will be downloaded. No model weights.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1" %*
if errorlevel 1 (pause) else (echo Installation finished. Run Start.cmd. & pause)
