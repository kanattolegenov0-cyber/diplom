@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-whisper.ps1" %*
set "ayan_exit=%errorlevel%"
echo.
if not "%ayan_exit%"=="0" echo Whisper setup failed. Review the error above.
pause
exit /b %ayan_exit%
