@echo off
setlocal
title Stop ContextKit
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" -Action stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo ContextKit did not stop. See the message above.
    pause
)
exit /b %RESULT%
