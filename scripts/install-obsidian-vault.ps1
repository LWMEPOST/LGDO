[CmdletBinding()]
param(
    [string]$VaultPath = 'vault',

    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($VaultPath)) {
    [Console]::Error.WriteLine('VaultPath must not be empty or whitespace.')
    exit 2
}
if ([System.IO.Path]::IsPathRooted($VaultPath)) {
    $resolvedVaultPath = [System.IO.Path]::GetFullPath($VaultPath)
} else {
    $resolvedVaultPath = [System.IO.Path]::GetFullPath((Join-Path $root $VaultPath))
}
if ([System.StringComparer]::OrdinalIgnoreCase.Equals($resolvedVaultPath, $root)) {
    [Console]::Error.WriteLine("VaultPath must not resolve to the repository root: $root")
    exit 2
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
