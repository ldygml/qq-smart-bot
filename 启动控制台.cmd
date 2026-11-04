@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0control.ps1"
if errorlevel 1 (
    echo.
    echo Control plane failed to start. Read the error above.
) else (
    echo.
    echo Control plane is ready in your browser.
)
pause
