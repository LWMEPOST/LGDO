[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultName,

    [Parameter(Mandatory = $true)]
    [string]$PagePath,

    [switch]$PrintOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $json = & python -m app.obsidian link --vault-name $VaultName --page-path $PagePath
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Error "Unable to build the Obsidian link. The Python command exited with code $exitCode."
    exit $exitCode
}

try {
    $uri = [string](($json | ConvertFrom-Json).url)
} catch {
    Write-Error "Unable to parse the Obsidian link returned by the Python command: $($_.Exception.Message)"
    exit 1
}

if ($PrintOnly) {
    Write-Output $uri
    exit 0
}

try {
    Start-Process -FilePath $uri -ErrorAction Stop | Out-Null
} catch {
    Write-Error "Unable to open Obsidian URI '$uri'. Confirm that Obsidian is installed and the obsidian:// protocol is registered. $($_.Exception.Message)"
    exit 1
}
