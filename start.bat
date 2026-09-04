@echo off
setlocal
title Start ContextKit
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" -Action start -OpenBrowser
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo ContextKit did not start. See the message above.
    pause
)
exit /b %RESULT%
