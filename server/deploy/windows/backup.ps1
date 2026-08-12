param(
  [Parameter(Mandatory=$true)][string]$InstallRoot,
  [string]$ServiceName = "TicketsbotApi"
)
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
$python = Join-Path $root ".venv\Scripts\python.exe"
$maintenance = Join-Path $root "scripts\maintenance.py"
$backups = Join-Path $root "backups"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $backups,$logs | Out-Null
$destination = Join-Path $backups ("ticketsbot-{0}.sqlite" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$wasRunning = $service -and $service.Status -eq "Running"
try {
  if ($wasRunning) { Stop-Service $ServiceName; $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30)) }
  Stop-ScheduledTask -TaskName "TicketsbotWorkers" -ErrorAction SilentlyContinue
  & $python $maintenance backup $destination *>> (Join-Path $logs "backup.log")
  if ($LASTEXITCODE -ne 0) { throw "Backup command failed with exit code $LASTEXITCODE" }
  & $python $maintenance cleanup-quarantine *>> (Join-Path $logs "cleanup.log")
  if ($LASTEXITCODE -ne 0) { throw "Quarantine cleanup failed with exit code $LASTEXITCODE" }
} finally {
  if ($wasRunning) { Start-Service $ServiceName }
  Start-ScheduledTask -TaskName "TicketsbotWorkers" -ErrorAction SilentlyContinue
}
