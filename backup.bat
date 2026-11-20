@echo off
rem ================= marvis-proxy / Windows backup =================
rem usage:
rem   backup.bat        keep backup_keep from config.json (default 7)
rem   backup.bat 14     keep 14 snapshots
rem
rem what it does: sqlite hot snapshot through the official backup API
rem (copying marvis.db under WAL would lose recent writes), packed with
rem config.json / models.json into backups\marvis-YYYYmmdd-HHMMSS.tar.gz.
rem safe to run while serve.py is running.
rem NOTE: keep this file pure ASCII (see run.bat header).
rem ================================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---------------- find interpreter ----------------
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python not found.
  echo         Install Python 3.8+ from python.org with "Add python.exe to PATH".
  if "%~1"=="" pause
  exit /b 1
)

rem ---------------- run backup ----------------
%PY% -u tools\backup_now.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [ERROR] backup failed, exit code %RC%
)
if "%~1"=="" (
  echo.
  pause
)
exit /b %RC%
