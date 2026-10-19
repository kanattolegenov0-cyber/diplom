[CmdletBinding()]
param([ValidateSet("small", "medium", "large-v3")][string]$Model = "medium")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Run start-windows.bat once, then close Ayan and run this setup again."
}
if (-not $env:AYAN_DATA_DIR) {
    $env:AYAN_DATA_DIR = Join-Path $env:LOCALAPPDATA "Ayan"
}
Write-Host "Close Ayan before continuing. Model: $Model. First download can take several minutes."
& $Python -m pip install -r requirements-whisper.txt
if ($LASTEXITCODE -ne 0) { throw "Whisper dependency installation failed." }
& $Python -m app.setup_transcription --model $Model
if ($LASTEXITCODE -ne 0) { throw "Whisper model setup failed. Check the connection and free disk/RAM, then retry." }
Write-Host "Ready. Start Ayan and check Settings > speech recognition. No API key is needed."
