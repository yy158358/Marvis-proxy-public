@echo off
rem ================= marvis-proxy / Windows launcher =================
rem usage:
rem   run.bat           foreground (Ctrl+C to stop, logs in this window)
rem   run.bat bg        background (logs -> logs\serve.log)
rem   run.bat stop      stop (data\serve.pid, else scan python cmdline)
rem   run.bat status    health check (GET /health)
rem   run.bat log       tail -f logs\serve.log
rem   run.bat accept    acceptance test (key coverage / chat / image / models)
rem   run.bat accept fast   same but skip real upstream calls
rem requires Python 3.8+ from python.org, "Add python.exe to PATH" checked
rem NOTE: keep this file pure ASCII. Chinese text in a .bat is decoded with
rem the console codepage and can swallow the following ASCII char, which
rem turns the rest of the line into a bogus command.
rem ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem console + python both UTF-8 so Chinese logs from serve.py render right
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
  echo         Then verify in cmd:  py -3 --version
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.8+ required. Current:
  %PY% --version
  pause
  exit /b 1
)

if not exist logs mkdir logs
if not exist data mkdir data

rem ---------------- dispatch ----------------
set "MODE=%~1"
if /i "%MODE%"=="bg"     goto :bg
if /i "%MODE%"=="stop"   goto :stop
if /i "%MODE%"=="status" goto :status
if /i "%MODE%"=="log"    goto :log
if /i "%MODE%"=="accept" goto :accept
goto :fg

:fg
echo === foreground on http://127.0.0.1:8088 - Ctrl+C to stop ===
echo     logs print here; use "run.bat bg" to write into logs\serve.log
echo.
%PY% -u serve.py
goto :done

:bg
echo === background: logs go to logs\serve.log ===
rem serve.py always appends its own output to logs\serve.log (tee), so the child
rem must NOT also redirect into that file, otherwise every line shows up twice.
start "marvis-proxy" /min cmd /c "%PY% -u serve.py"
ping -n 4 127.0.0.1 >nul
call :health
goto :done

:status
call :health
goto :done

:log
if not exist logs\serve.log (
  echo no logs\serve.log yet
  goto :done
)
powershell -NoProfile -Command "Get-Content -LiteralPath 'logs\serve.log' -Tail 40 -Wait -Encoding utf8"
goto :done

:accept
echo === acceptance test ===
if /i "%~2"=="fast" (
  %PY% tools\accept.py --fast
) else (
  %PY% tools\accept.py
)
if errorlevel 1 (
  echo.
  echo accept: FAILED - see lines marked FAIL above
) else (
  echo.
  echo accept: all good
)
goto :done

:stop
set "PID="
if exist data\serve.pid set /p PID=<data\serve.pid
if not defined PID goto :stop_scan
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%'; if ($p -and $p.CommandLine -match 'serve\.py') { Stop-Process -Id %PID% -Force; Write-Host ('stopped pid=' + %PID%) } else { Write-Host ('pid %PID% is not serve.py, skipped') }"
if exist data\serve.pid del /q data\serve.pid >nul 2>nul
goto :done

:stop_scan
powershell -NoProfile -Command "$ps = Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { $_.CommandLine -match 'serve\.py' }; if ($ps) { $ps | ForEach-Object { Write-Host ('stopping pid=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host 'no running serve.py found' }"
goto :done

:health
curl -s -m 5 http://127.0.0.1:8088/health
if errorlevel 1 (
  echo.
  echo no response on 127.0.0.1:8088 - not started, or port busy
) else (
  echo.
)
exit /b 0

:done
endlocal
exit /b 0
