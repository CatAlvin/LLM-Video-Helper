@echo off
setlocal
title Restart ContextKit
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" -Action restart
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo ContextKit did not restart. See the message above.
    pause
)
exit /b %RESULT%
