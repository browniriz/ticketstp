param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
Set-Location $root
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv is required" }
uv sync --frozen
New-Item -ItemType Directory -Force (Join-Path $root "logs"),(Join-Path $root "backups"),(Join-Path $root "media") | Out-Null
Write-Host "Prepared pinned environment from uv.lock. No service or task was installed or run."
