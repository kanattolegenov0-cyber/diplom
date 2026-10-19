[CmdletBinding()]
param()

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

Invoke-Checked $Python @("-c", "import sys; assert sys.version_info[:2] == (3, 12), 'Build with Python 3.12; recreate .venv with that version'")
if (-not $env:AYAN_DATA_DIR) {
    $env:AYAN_DATA_DIR = Join-Path $env:LOCALAPPDATA "Ayan"
}
if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:AYAN_DATA_DIR "browsers"
}
Invoke-Checked $Python @("-m", "pip", "install", "-r", "requirements.txt", "-r", "requirements-desktop.txt")
Invoke-Checked $Python @("desktop.py", "--install-browser")

$BuildArguments = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onedir", "--noconsole",
    "--name", "Ayan",
    "--distpath", "dist",
    "--workpath", "build\pyinstaller",
    "--specpath", "build",
    "--add-data", "app/static;app/static",
    "--collect-all", "playwright",
    "--collect-all", "yt_dlp",
    "--collect-all", "webview",
    "--collect-all", "speech_recognition",
    "--collect-all", "edge_tts",
    "--collect-all", "aiohttp",
    "--collect-all", "certifi",
    "--collect-all", "httpx",
    "--collect-all", "vosk",
    "--collect-all", "requests",
    "--collect-all", "cffi",
    "--hidden-import", "_cffi_backend",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.loops.asyncio",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.http.h11_impl",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan.on",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "pyaudio",
    "desktop.py"
)
# Include optional local recognition only when explicitly installed.
& $Python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('faster_whisper') else 1)"
if ($LASTEXITCODE -eq 0) {
    $BuildArguments = $BuildArguments[0..($BuildArguments.Count - 2)] + @(
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2",
        "--collect-all", "av", "--collect-all", "tokenizers",
        "--collect-all", "huggingface_hub", "--collect-all", "onnxruntime", "desktop.py"
    )
}
Invoke-Checked $Python $BuildArguments

$Executable = Join-Path $ProjectRoot "dist\Ayan\Ayan.exe"
$CheckProcess = Start-Process -FilePath $Executable -ArgumentList "--check" -Wait -PassThru
if ($CheckProcess.ExitCode -ne 0) {
    throw "Packaged app check failed. Read $env:AYAN_DATA_DIR\desktop.log for details."
}
Write-Host "Built and checked: $Executable"
Write-Host "Distribute the entire dist\Ayan folder, including _internal."
