@echo off
REM ============================================================
REM  GHTrend desktop launcher
REM  IMPORTANT: keep this file pure ASCII.
REM  cmd.exe parses .bat in the OEM codepage (GBK on zh-CN), so
REM  UTF-8 Chinese in comments corrupts the script. All Chinese
REM  text is printed by Python, which handles UTF-8 correctly.
REM
REM  This script exits immediately (no console window lingers).
REM  server.py opens the app window itself, and pops a message
REM  box if startup fails. See logs\server.log for diagnostics.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
title GHTrend

set "PORT=8765"

REM ---- locate Python: prefer local venv, then system PATH ----
set "PY="
set "PYW="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%I in ('where python 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY goto no_python

REM pythonw lives next to python.exe and avoids a console window.
REM Some installers ship python.exe without pythonw.exe, so fall back to python.exe
REM (a console will flash briefly, but it still works).
for %%I in ("%PY%") do set "PYW=%%~dpIpythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

set "EDGE="
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

REM already running? -> just open the window, do not start a second server
"%PY%" -c "import socket,sys;s=socket.socket();s.settimeout(0.3);sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>&1
if not errorlevel 1 goto open_window

REM cold start: server.py opens the window itself, this script exits at once
if not exist "logs" mkdir "logs"
start "" "%PYW%" server.py --port %PORT%
exit /b 0

:open_window
if defined EDGE (
    start "" "%EDGE%" --app=http://127.0.0.1:%PORT%/ --window-size=1200,880
    exit /b 0
)
if defined CHROME (
    start "" "%CHROME%" --app=http://127.0.0.1:%PORT%/ --window-size=1200,880
    exit /b 0
)
start "" http://127.0.0.1:%PORT%/
exit /b 0

REM Python not found: the usual case is someone received the zip but has no Python
:no_python
echo.
echo   ============================================================
echo    Python was not found on this computer.
echo.
echo    GHTrend needs Python 3.9 or newer to run.
echo.
echo    1) Download it here:  https://www.python.org/downloads/
echo    2) Run the installer and CHECK the box
echo         [x] Add python.exe to PATH
echo    3) Double-click this file again.
echo   ============================================================
echo.
pause
exit /b 1
