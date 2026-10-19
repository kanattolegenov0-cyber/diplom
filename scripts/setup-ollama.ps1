[CmdletBinding()]
param(
    [ValidateSet("qwen2.5:3b", "qwen2.5:1.5b")]
    [string]$Model = "qwen2.5:3b"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Run this helper on Windows."
}

$OllamaCommand = Get-Command ollama -CommandType Application -ErrorAction SilentlyContinue
$OllamaPath = $null
if ($OllamaCommand) {
    $OllamaPath = $OllamaCommand.Source
} elseif ($env:LOCALAPPDATA) {
    $InstalledPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $InstalledPath -PathType Leaf) {
        $OllamaPath = $InstalledPath
    }
}

if (-not $OllamaPath) {
    Write-Host "Ollama was not found."
    Write-Host "1. Download and install Ollama from its official Windows page:"
    Write-Host "   https://ollama.com/download/windows"
    Write-Host "2. Launch Ollama from the Windows Start menu."
    Write-Host "3. Run setup-ollama.bat again."
    exit 1
}

$VersionUrl = "http://127.0.0.1:11434/api/version"

function Test-OllamaReady {
    try {
        $Response = Invoke-RestMethod -Uri $VersionUrl -Method Get -TimeoutSec 2
        return ($null -ne $Response.PSObject.Properties["version"] -and
                -not [string]::IsNullOrWhiteSpace([string]$Response.version))
    } catch {
        return $false
    }
}

# Always prepare a local server, even if this shell has a remote OLLAMA_HOST.
# Restore that shell setting afterwards; never write environment/user profiles.
$PreviousOllamaHost = [Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Process")
try {
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    if (-not (Test-OllamaReady)) {
        Write-Host "Starting Ollama on 127.0.0.1:11434..."
        $ServerProcess = Start-Process -FilePath $OllamaPath -ArgumentList "serve" -WindowStyle Hidden -PassThru
        $Ready = $false
        $Deadline = (Get-Date).AddSeconds(30)
        do {
            if (Test-OllamaReady) {
                $Ready = $true
                break
            }
            $ServerProcess.Refresh()
            if ($ServerProcess.HasExited) {
                break
            }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $Deadline)

        if (-not $Ready) {
            throw "Ollama did not become ready on port 11434. Open Ollama from the Start menu, or run 'ollama serve' to see its error, then try again."
        }
    }

    Write-Host "Preparing $Model. Ollama will download missing model files; this can take several minutes."
    Write-Host "Keep this window open until the download finishes. No API key is required."
    & $OllamaPath pull $Model
    if ($LASTEXITCODE -ne 0) {
        throw "Ollama could not download $Model (exit $LASTEXITCODE). Check the network and available disk space, then run this helper again."
    }

    Write-Host "Model ready: $Model"
    Write-Host "In Ayan Settings, select Ollama, enter $Model as the model, and save."
    Write-Host "Ollama must stay running for local conversations."
} finally {
    if ($null -eq $PreviousOllamaHost) {
        Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue
    } else {
        $env:OLLAMA_HOST = $PreviousOllamaHost
    }
}
