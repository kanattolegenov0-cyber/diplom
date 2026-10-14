@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-windows.ps1" %*
set "ayan_exit=%errorlevel%"
if not "%ayan_exit%"=="0" (
    echo.
    echo Ayan build failed. Review the error above.
    pause
)
exit /b %ayan_exit%
