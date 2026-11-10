@echo off
REM ============================================================
REM  GHTrend daily report - run by Windows Task Scheduler
REM  IMPORTANT: keep this file pure ASCII.
REM  cmd.exe parses .bat in the OEM codepage (GBK on zh-CN), so
REM  UTF-8 Chinese in comments corrupts the script. Chinese
REM  output is produced by Python, not by this file.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

set "PYTHON="
if exist "C:\Users\34867\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PYTHON=C:\Users\34867\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PYTHON set "PYTHON=python"

echo. >> "logs\daily.log"
echo ==== %DATE% %TIME% START ==== >> "logs\daily.log"

"%PYTHON%" ghtrend.py daily --report --notify >> "logs\daily.log" 2>&1

if errorlevel 1 (
    echo ==== %DATE% %TIME% FAILED code=%errorlevel% ==== >> "logs\daily.log"
    exit /b 1
)

echo ==== %DATE% %TIME% DONE ==== >> "logs\daily.log"
exit /b 0
