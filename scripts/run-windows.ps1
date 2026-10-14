[CmdletBinding()]
param(
    [switch]$Web,
    [ValidateRange(0, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Invoke-Checked {
    param([string]$Executable, [string[]]$CommandArguments)
    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed with exit code $LASTEXITCODE."
    }
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    if (Test-Path (Join-Path $ProjectRoot ".venv")) {
        throw "The existing .venv is not a Windows environment. Rename it and run this script again."
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Invoke-Checked "py" @("-3.12", "-m", "venv", ".venv")
    } else {
        Invoke-Checked "python" @("-c", "import sys; assert sys.version_info[:2] == (3, 12), 'Install Python 3.12 from python.org'")
        Invoke-Checked "python" @("-m", "venv", ".venv")
    }
}

Invoke-Checked $Python @("-c", "import sys; assert sys.version_info[:2] == (3, 12), 'This project uses Python 3.12; recreate .venv with that version'")
if (-not $env:AYAN_DATA_DIR) {
    $env:AYAN_DATA_DIR = Join-Path $env:LOCALAPPDATA "Ayan"
}
if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:AYAN_DATA_DIR "browsers"
}
Invoke-Checked $Python @("-m", "pip", "install", "-r", "requirements.txt", "-r", "requirements-desktop.txt")
$LaunchArguments = @("desktop.py", "--port", "$Port")
if ($Web) {
    $LaunchArguments += "--web"
}
Invoke-Checked $Python $LaunchArguments
