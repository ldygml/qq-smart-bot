@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable_group_mention.ps1"
if errorlevel 1 (
    echo.
    echo Failed to enable the mention requirement. Read the error above.
) else (
    echo.
    echo Group chat now requires an @ mention.
)
pause
