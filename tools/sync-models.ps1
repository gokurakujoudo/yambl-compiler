[CmdletBinding()]
param([string]$Source = (Join-Path $PSScriptRoot '../../yambl/lang'))
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $Source).Path
$targetRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../src/yambl_compiler/lang')).Path
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'yambl-root.lclcfg') -PathType Leaf)) {
    throw 'Source must be the built-in model repository.'
}
foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse | Where-Object { $_.Extension -in '.yambl', '.lclcfg' }) {
    $destination = Join-Path $targetRoot ([IO.Path]::GetRelativePath($sourceRoot, $file.FullName))
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $destination
}
Write-Output "Synced built-in models from $sourceRoot"
