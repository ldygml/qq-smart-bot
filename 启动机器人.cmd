@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Read the error above.
) else (
    echo.
    echo Startup complete. This window can be closed.
)
pause
