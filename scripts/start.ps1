param(
    [string]$Workspace = (Get-Location).Path,
    [switch]$AllowRisky
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaExe = Join-Path $projectRoot '.runtime\ollama\ollama.exe'
$modelDir = Join-Path $projectRoot '.data\ollama\models'

if (-not (Test-Path -LiteralPath $ollamaExe)) {
    throw 'Ollama is not installed for this project. Run .\scripts\setup.ps1 first.'
}

$env:OLLAMA_MODELS = $modelDir
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_FLASH_ATTENTION = '1'

try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
} catch {
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -WorkingDirectory $projectRoot | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
            break
        } catch {
            if ($attempt -eq 29) { throw 'Ollama did not become ready.' }
        }
    }
}

$arguments = @((Join-Path $projectRoot 'agent.py'), '--workspace', $Workspace)
if ($AllowRisky) { $arguments += '--allow-risky' }
& py @arguments

