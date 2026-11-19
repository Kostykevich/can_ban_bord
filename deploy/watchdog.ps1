<#
  Сторож: запускается Планировщиком каждые 5 минут (задача «Vektor Watchdog», создаёт install-tasks.ps1).
  Если caddy.exe не работает, запускает задачу Caddy. Если сайт не отвечает 2 проверки подряд, перезапускает сайт.
  Нужен, потому что «перезапуск при сбое» в Планировщике не срабатывает, когда программа падает после старта.
  Журнал: instance\logs\watchdog.log
#>
param([string]$ProjectDir = (Split-Path -Parent $PSScriptRoot))

$log = Join-Path $ProjectDir "instance\logs\watchdog.log"
$state = Join-Path $ProjectDir "instance\logs\watchdog.fails"
function Log($t) { Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $t" -Encoding UTF8 }

# --- Caddy ---
if (-not (Get-Process caddy -ErrorAction SilentlyContinue)) {
    Log "caddy.exe не работает -> запускаю задачу Caddy"
    Stop-ScheduledTask -TaskName "Caddy" -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName "Caddy"
}

# --- Сайт ---
$alive = $false
try { $alive = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 10).StatusCode -eq 200 } catch { }
if ($alive) {
    if (Test-Path $state) { Remove-Item $state -ErrorAction SilentlyContinue; Log "сайт снова отвечает" }
    exit 0
}
$fails = 1
if (Test-Path $state) { $fails = [int](Get-Content $state) + 1 }
Set-Content -Path $state -Value $fails
if ($fails -lt 2) { Log "сайт не ответил (1-й раз), жду следующую проверку"; exit 0 }

Log "сайт не отвечает $fails проверки подряд -> перезапускаю «Vektor Kanban»"
Stop-ScheduledTask -TaskName "Vektor Kanban" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*serve.py*" -and $_.ExecutablePath -like "$ProjectDir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName "Vektor Kanban"
Remove-Item $state -ErrorAction SilentlyContinue
