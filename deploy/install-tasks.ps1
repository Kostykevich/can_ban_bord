<#
  Создаёт задачи Планировщика Windows для VDS:
    - "Vektor Kanban" — сайт (Waitress), стартует при включении сервера;
    - "Caddy"         — HTTPS-прокси, стартует при включении сервера;
    - "Vektor Backup" — резервная копия базы каждый день в 04:00;
    - "Vektor Watchdog" — каждые 5 минут поднимает Caddy и сайт, если они упали;
    - "Vektor AutoDeploy" — каждую минуту проверяет GitHub и выкладывает новую версию.
  Задачи работают, даже если никто не вошёл в систему, и перезапускаются при сбое.

  Запуск (PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА), из папки проекта:
      powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1
  Без автодеплоя:  ... -File deploy\install-tasks.ps1 -NoAutoDeploy

  Повторный запуск безопасен: задачи пересоздаются.
#>
param(
    [switch]$NoAutoDeploy,
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$CaddyDir = "C:\caddy"
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запустите PowerShell от имени администратора." -ForegroundColor Red
    exit 1
}

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Не найден $python. Сначала создайте .venv и установите зависимости (см. DEPLOY.md, шаг 6)." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Host "Не найден файл .env. Создайте его (см. DEPLOY.md, шаг 7)." -ForegroundColor Red
    exit 1
}
$caddyExe = Join-Path $CaddyDir "caddy.exe"
$caddyFile = Join-Path $CaddyDir "Caddyfile"
if (-not (Test-Path $caddyExe) -or -not (Test-Path $caddyFile)) {
    Write-Host "Не найден $caddyExe или $caddyFile (см. DEPLOY.md, шаг 10)." -ForegroundColor Red
    exit 1
}

$logs = Join-Path $ProjectDir "instance\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
New-Item -ItemType Directory -Force (Join-Path $CaddyDir "logs") | Out-Null

$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$atStartup = New-ScheduledTaskTrigger -AtStartup

function Register-VektorTask($name, $action, $trigger, $taskSettings) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    # остановленная задача может оставить работающий процесс — он держал бы порт
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'caddy.exe'" |
        Where-Object { ($name -eq "Vektor Kanban" -and $_.CommandLine -like "*serve.py*" -and $_.ExecutablePath -like "$ProjectDir*") -or
                       ($name -eq "Caddy" -and $_.Name -eq "caddy.exe") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $taskSettings `
        -User "SYSTEM" -RunLevel Highest | Out-Null
    Write-Host "Задача создана: $name" -ForegroundColor Green
}

# Сайт: вывод пишется в instance\logs\server.log
$appCmd = "/c set PYTHONIOENCODING=utf-8&& `"$python`" serve.py >> `"$logs\server.log`" 2>&1"
$appAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $appCmd -WorkingDirectory $ProjectDir
Register-VektorTask "Vektor Kanban" $appAction $atStartup $settings

# Caddy
# Вывод Caddy (ошибки сертификата, падения) пишется в C:\caddy\logs\caddy.log
$caddyCmd = "/c `"`"$caddyExe`" run --config `"$caddyFile`" >> `"$CaddyDir\logs\caddy.log`" 2>&1`""
$caddyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $caddyCmd -WorkingDirectory $CaddyDir
Register-VektorTask "Caddy" $caddyAction $atStartup $settings

# Резервная копия раз в день
$backupCmd = "/c set PYTHONIOENCODING=utf-8&& `"$python`" scripts\backup_db.py >> `"$logs\backup.log`" 2>&1"
$backupAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $backupCmd -WorkingDirectory $ProjectDir
$daily = New-ScheduledTaskTrigger -Daily -At 4am
Register-VektorTask "Vektor Backup" $backupAction $daily (New-ScheduledTaskSettingsSet -StartWhenAvailable)

# Сторож каждые 5 минут: поднимает Caddy и сайт, если они упали
$watchCmd = "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\deploy\watchdog.ps1`""
$watchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $watchCmd -WorkingDirectory $ProjectDir
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
$watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
Register-VektorTask "Vektor Watchdog" $watchAction @($every5, $atStartup) $watchSettings

# Автодеплой каждую минуту: новая версия из GitHub -> тесты -> перезапуск (или откат)
if ($NoAutoDeploy) {
    if (Get-ScheduledTask -TaskName "Vektor AutoDeploy" -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName "Vektor AutoDeploy" -Confirm:$false
    }
    Write-Host "Автодеплой выключен (-NoAutoDeploy)" -ForegroundColor Yellow
} elseif (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git не найден в PATH — автодеплой не создан. Установите Git (DEPLOY.md, шаг 5) и запустите скрипт снова." -ForegroundColor Yellow
} else {
    $deployCmd = "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\deploy\auto-deploy.ps1`""
    $deployAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $deployCmd -WorkingDirectory $ProjectDir
    $every1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $deploySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-VektorTask "Vektor AutoDeploy" $deployAction @($every1, $atStartup) $deploySettings
}

Start-ScheduledTask -TaskName "Vektor Kanban"
Start-ScheduledTask -TaskName "Caddy"

Write-Host ""
Write-Host "Ждём запуска сайта..." -NoNewline
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($r.StatusCode -eq 200) { Write-Host " готово!" -ForegroundColor Green; exit 0 }
    } catch { Write-Host "." -NoNewline }
}
Write-Host ""
Write-Host "Сайт не ответил за 20 секунд. Посмотрите лог: $logs\server.log" -ForegroundColor Yellow
