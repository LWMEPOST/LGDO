[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultName,

    [Parameter(Mandatory = $true)]
    [string]$PagePath,

    [switch]$PrintOnly
)

$ErrorActionPreference = 'Stop'
function Write-Stderr {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $json = & python -m app.obsidian link --vault-name $VaultName --page-path $PagePath
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Stderr "Unable to build the Obsidian link. The Python command exited with code $exitCode."
    exit $exitCode
}

try {
    $uri = [string](($json | ConvertFrom-Json).url)
} catch {
    Write-Stderr "Unable to parse the Obsidian link returned by Python. Expected an obsidian:// URL. $($_.Exception.Message)"
    exit 2
}

if ([string]::IsNullOrWhiteSpace($uri)) {
    Write-Stderr 'Python returned no URL. Expected a nonempty obsidian:// URL.'
    exit 2
}

try {
    $parsedUri = [System.Uri]$uri
} catch {
    Write-Stderr "Python returned an invalid URL '$uri'. Expected an absolute obsidian:// URL."
    exit 2
}
if (-not $parsedUri.IsAbsoluteUri -or -not [System.StringComparer]::Ordinal.Equals($parsedUri.Scheme, 'obsidian')) {
    Write-Stderr "Python returned an unsupported URL '$uri'. Expected an absolute obsidian:// URL."
    exit 2
}

if ($PrintOnly) {
    Write-Output $uri
    exit 0
}

try {
    Start-Process -FilePath $uri -ErrorAction Stop | Out-Null
} catch {
    Write-Stderr "Unable to open Obsidian URI '$uri'. Confirm that Obsidian is installed and the obsidian:// protocol is registered. $($_.Exception.Message)"
    exit 1
}
