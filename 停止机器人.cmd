@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if errorlevel 1 (
    echo.
    echo Shutdown failed. Read the error above.
) else (
    echo.
    echo Shutdown complete. This window can be closed.
)
pause
