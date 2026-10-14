@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-windows.ps1" %*
set "ayan_exit=%errorlevel%"
if not "%ayan_exit%"=="0" (
    echo.
    echo Ayan could not start. Review the error above.
    pause
)
exit /b %ayan_exit%
