@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Career Radar baslatiliyor...
".venv\Scripts\python.exe" -m career_radar.cli serve
pause
