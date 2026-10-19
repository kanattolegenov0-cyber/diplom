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
$DependenciesReady = $false
for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
    Write-Host "Installing Whisper packages (attempt $Attempt/3; network timeout 120 seconds)..."
    & $Python -m pip --disable-pip-version-check install --timeout 120 --retries 8 -r requirements-whisper.txt
    if ($LASTEXITCODE -eq 0) {
        $DependenciesReady = $true
        break
    }
    if ($Attempt -lt 3) {
        Write-Warning "Package server did not finish the download. Pip will retry from its cache in 5 seconds."
        Start-Sleep -Seconds 5
    }
}
if (-not $DependenciesReady) {
    throw "Whisper packages could not be downloaded after 3 attempts. Check files.pythonhosted.org access and rerun setup-whisper.bat; installed packages and pip cache are retained."
}
& $Python -m app.setup_transcription --model $Model
if ($LASTEXITCODE -ne 0) { throw "Whisper model setup failed. Check the connection and free disk/RAM, then retry." }
Write-Host "Ready. Start Ayan and check Settings > speech recognition. No API key is needed."
