param(
    [switch]$SkipModelPull
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime\ollama'
$modelDir = Join-Path $projectRoot '.data\ollama\models'
$archivePath = Join-Path $projectRoot '.runtime\ollama-windows-amd64.zip'
$ollamaExe = Join-Path $runtimeDir 'ollama.exe'
$downloadUrl = 'https://ollama.com/download/ollama-windows-amd64.zip'
$requiredFreeGB = 11
$modelName = 'qwen3.5:9b'

$driveName = ([System.IO.Path]::GetPathRoot($projectRoot)).TrimEnd('\').TrimEnd(':')
$drive = Get-PSDrive -Name $driveName
$freeGB = [math]::Round($drive.Free / 1GB, 2)
Write-Host "Project drive free space: $freeGB GB"
if ($freeGB -lt $requiredFreeGB) {
    throw "At least $requiredFreeGB GB free space is required before installing Ollama and qwen3.5:9b."
}

New-Item -ItemType Directory -Force -Path $runtimeDir, $modelDir | Out-Null
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $modelDir, 'User')
$env:OLLAMA_MODELS = $modelDir
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_HOST = '127.0.0.1:11434'

if (-not (Test-Path -LiteralPath $ollamaExe)) {
    Write-Host 'Downloading the official standalone Ollama runtime to the project drive...'
    & curl.exe -L --fail --progress-bar --output $archivePath $downloadUrl
    if ($LASTEXITCODE -ne 0) { throw 'Ollama runtime download failed.' }
    Write-Host 'Extracting Ollama...'
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDir -Force
    Remove-Item -LiteralPath $archivePath -Force
}

$serverReady = $false
try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
    $serverReady = $true
} catch {
    $serverReady = $false
}

if (-not $serverReady) {
    Write-Host 'Starting the project-local Ollama server...'
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -WorkingDirectory $projectRoot | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
            $serverReady = $true
            break
        } catch {
            $serverReady = $false
        }
    }
}

if (-not $serverReady) {
    throw 'Ollama did not become ready on http://127.0.0.1:11434.'
}

if (-not $SkipModelPull) {
    Write-Host 'Pulling official Qwen 3.5 9B (about 6.6 GB) from Ollama...'
    & $ollamaExe pull $modelName
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed.' }
}

Write-Host ''
Write-Host 'Setup complete.'
Write-Host "Ollama: $ollamaExe"
Write-Host "Models: $modelDir"
Write-Host 'Start the agent with: .\scripts\start.ps1'

