@echo off
chcp 65001 >nul
cd /d "%~dp0"
title GHTrend - GitHub Star Trends

set "PYTHON="
where python >nul 2>&1 && set "PYTHON=python"
if exist "C:\Users\34867\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PYTHON=C:\Users\34867\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

if not defined PYTHON (
    echo.
    echo   [ERROR] Python not found.
    echo   Install Python 3.9+ from https://www.python.org/downloads/
    echo   Remember to check "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

"%PYTHON%" ghtrend.py

if errorlevel 1 pause
