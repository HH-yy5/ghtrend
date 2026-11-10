@echo off
REM Chinese-named convenience wrapper. Content must stay pure ASCII:
REM cmd.exe reads .bat as GBK, so any UTF-8 Chinese here would break.
call "%~dp0launch.bat"
exit /b %errorlevel%
