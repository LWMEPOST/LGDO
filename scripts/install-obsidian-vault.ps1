[CmdletBinding()]
param(
    [string]$VaultPath = 'vault',

    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($VaultPath)) {
    $resolvedVaultPath = [System.IO.Path]::GetFullPath($VaultPath)
} else {
    $resolvedVaultPath = [System.IO.Path]::GetFullPath((Join-Path $root $VaultPath))
}

$pythonArgs = @('-m', 'app.obsidian', 'install', '--vault', $resolvedVaultPath)
if ($Refresh) {
    $pythonArgs += '--refresh'
}

Push-Location $root
try {
    & python @pythonArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
