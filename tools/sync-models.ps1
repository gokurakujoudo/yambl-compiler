[CmdletBinding()]
param([string]$Source = (Join-Path $PSScriptRoot '../../yambl/models'))
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $Source).Path
$targetRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../src/yambl_compiler/models')).Path
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'yambl-root.lclcfg') -PathType Leaf)) {
    throw 'Source must be the built-in model repository.'
}
foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -File | Where-Object { $_.Extension -in '.yambl', '.lclcfg' }) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $targetRoot $file.Name)
}
Write-Output "Synced built-in models from $sourceRoot"
