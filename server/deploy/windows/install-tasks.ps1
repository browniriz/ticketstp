param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($InstallRoot)
$python = Join-Path $root ".venv\Scripts\python.exe"
$maintenance = Join-Path $root "scripts\maintenance.py"
$logs = Join-Path $root "logs"
$backups = Join-Path $root "backups"
New-Item -ItemType Directory -Force $logs,$backups | Out-Null
foreach ($path in @($python, $maintenance)) { if (-not (Test-Path $path)) { throw "Missing: $path" } }
$workerScript = Join-Path $root "scripts\run_workers.py"
if (-not (Test-Path $workerScript)) { throw "Missing: $workerScript" }
$workerAction = New-ScheduledTaskAction -Execute $python -Argument "`"$workerScript`"" -WorkingDirectory $root
$workerTrigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "TicketsbotWorkers" -Action $workerAction -Trigger $workerTrigger -RunLevel Highest -Description "Ticketsbot outbox workers (separate from API)"
$backupScript = Join-Path $root "deploy\windows\backup.ps1"
$backupAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -File `"$backupScript`" -InstallRoot `"$root`"" -WorkingDirectory $root
$backupTrigger = New-ScheduledTaskTrigger -Daily -At 03:00
Register-ScheduledTask -TaskName "TicketsbotBackup" -Action $backupAction -Trigger $backupTrigger -RunLevel Highest -Description "Ticketsbot coherent backup; quiesce API/workers before execution"
Write-Host "Tasks registered but not started. Backup task quiesces API and workers before snapshotting."
