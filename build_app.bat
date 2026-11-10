@echo off
REM ===========================================================
REM  GHTrend - Build Windows desktop app (Electron + Python)
REM  ASCII ONLY. cmd.exe parses .bat as GBK; non-ASCII chars
REM  would corrupt the commands. Keep this file pure ASCII.
REM ===========================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   GHTrend  Build
echo ============================================
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js not found in PATH.
  echo         Install Node.js 18+ then run this again.
  goto :fail
)

REM --- locate a python that has PyInstaller installed ---
set "PY="
if exist "%~dp0.venv-build\Scripts\python.exe" set "PY=%~dp0.venv-build\Scripts\python.exe"
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY set "PY=%%i"
  )
)
if not defined PY (
  echo [ERROR] Python not found.
  goto :fail
)
echo Using Python: %PY%
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PyInstaller is not installed for this Python.
  echo         Run:  "%PY%" -m pip install pyinstaller
  goto :fail
)

echo.
echo [1/3] Building Python backend ...
echo --------------------------------------------
if not exist "%~dp0electron\build\icon.ico" (
  "%PY%" "%~dp0tools\make_icon.py"
)
"%PY%" -m PyInstaller --noconfirm --clean --name ghtrend-server --onedir --noconsole ^
  --icon "%~dp0electron\build\icon.ico" ^
  --add-data "%~dp0web;web" ^
  --distpath "%~dp0backend-dist" ^
  --workpath "%~dp0build\pyinstaller" ^
  --specpath "%~dp0build" ^
  "%~dp0server.py"
if errorlevel 1 goto :fail

echo.
echo [2/3] Checking Electron dependencies ...
echo --------------------------------------------
cd /d "%~dp0electron"
if not exist "node_modules\electron\dist\electron.exe" (
  call npm install --no-audit --no-fund
  if errorlevel 1 goto :fail
)

echo.
echo [3/3] Packaging desktop app ...
echo --------------------------------------------
call npm run dist
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   DONE
echo   Output folder: %~dp0dist
echo ============================================
dir /b "%~dp0dist\*.exe" 2>nul
echo.
pause
exit /b 0

:fail
echo.
echo [FAILED] See the messages above.
echo.
pause
exit /b 1
