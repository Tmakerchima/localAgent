param(
    [string]$Workspace,
    [int]$Port = 8765,
    [switch]$AllowRisky,
    [switch]$NoBrowser,
    [string]$AllowedOrigin
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaExe = Join-Path $projectRoot '.runtime\ollama\ollama.exe'
$modelDir = Join-Path $projectRoot '.data\ollama\models'

if ([string]::IsNullOrWhiteSpace($Workspace)) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Choose the project folder for this Local Agent session.'
    $dialog.SelectedPath = (Get-Location).Path
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw 'No workspace folder was selected.'
    }
    $Workspace = $dialog.SelectedPath
}
$Workspace = [IO.Path]::GetFullPath($Workspace)
if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
    throw "Workspace does not exist: $Workspace"
}

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

$url = "http://127.0.0.1:$Port"
if (-not $NoBrowser) { Start-Process $url }
$arguments = @((Join-Path $projectRoot 'web_server.py'), '--workspace', $Workspace, '--port', $Port)
if ($AllowRisky) { $arguments += '--allow-risky' }
if (-not [string]::IsNullOrWhiteSpace($AllowedOrigin)) {
    $arguments += @('--allowed-origin', $AllowedOrigin.TrimEnd('/'))
}
& py @arguments
