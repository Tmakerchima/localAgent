param([switch]$ConfirmRollback)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $projectRoot '.local-agent\desktop-tools.json'

if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw 'No desktop-tool installation manifest was found. Existing software will not be guessed or removed.'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $ConfirmRollback) {
    $manifest | ConvertTo-Json -Depth 5
    throw 'Preview only. Add -ConfirmRollback to uninstall only packages recorded as installed by this Agent.'
}

foreach ($package in @($manifest.packages) | Where-Object installedByAgent | Sort-Object installedAt -Descending) {
    winget uninstall --id $package.id --exact --silent --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Failed to uninstall $($package.id)" }
}
Remove-Item -LiteralPath $manifestPath -Force
Write-Host 'Desktop tools were rolled back according to the installation manifest.'
