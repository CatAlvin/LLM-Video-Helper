@echo off
setlocal
title ContextKit Manager
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" -Action menu
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo ContextKit Manager encountered an error. See the message above.
    pause
)
exit /b %RESULT%
