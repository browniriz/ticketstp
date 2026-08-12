param(
  [Parameter(Mandatory=$true)][string]$InstallRoot,
  [string]$ServiceName = "TicketsbotApi"
)
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
$python = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"
$logs = Join-Path $root "logs"
$backups = Join-Path $root "backups"
foreach ($path in @($python, $envFile)) { if (-not (Test-Path $path)) { throw "Missing required absolute path: $path" } }
New-Item -ItemType Directory -Force $logs,$backups | Out-Null
$command = '"{0}" -m uvicorn ticketsbot.app:app --host 127.0.0.1 --port 8010' -f $python
# Requires NSSM in PATH. Deliberately installs only the API process; workers use a separate task.
nssm install $ServiceName $python "-m uvicorn ticketsbot.app:app --host 127.0.0.1 --port 8010"
nssm set $ServiceName AppDirectory $root
nssm set $ServiceName AppEnvironmentExtra "TICKETSBOT_WORKERS_ENABLED=false"
nssm set $ServiceName AppStdout (Join-Path $logs "api.log")
nssm set $ServiceName AppStderr (Join-Path $logs "api-error.log")
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName Start SERVICE_AUTO_START
Write-Host "Installed $ServiceName on 127.0.0.1:8010. Review .env, then start explicitly."
