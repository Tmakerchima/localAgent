param(
    [string]$Workspace = (Get-Location).Path,
    [int]$Port = 8765,
    [switch]$AllowRisky,
    [switch]$NoBrowser,
    [string]$AllowedOrigin
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$uiScript = Join-Path $projectRoot 'scripts\start-ui.ps1'
if (-not (Test-Path -LiteralPath $uiScript)) {
    throw "Missing startup script: $uiScript"
}

$arguments = @('-Workspace', $Workspace, '-Port', $Port)
if ($AllowRisky) { $arguments += '-AllowRisky' }
if ($NoBrowser) { $arguments += '-NoBrowser' }
if (-not [string]::IsNullOrWhiteSpace($AllowedOrigin)) {
    $arguments += @('-AllowedOrigin', $AllowedOrigin)
}

Write-Host "Starting Ollama, model warm-up, Local Agent UI, and workspace: $Workspace"
& powershell.exe -ExecutionPolicy Bypass -File $uiScript @arguments
exit $LASTEXITCODE
