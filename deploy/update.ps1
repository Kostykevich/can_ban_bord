<#
  Обновление сайта на VDS без потери данных:
    1) резервная копия базы;
    2) остановка сайта;
    3) git pull — новая версия кода;
    4) установка зависимостей;
    5) запуск и проверка.

  Запуск (PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА), из папки проекта:
      powershell -ExecutionPolicy Bypass -File deploy\update.ps1
#>
param([string]$ProjectDir = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"
Set-Location $ProjectDir
$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "1/5 Резервная копия базы..." -ForegroundColor Cyan
& $python scripts\backup_db.py
if ($LASTEXITCODE -ne 0) { Write-Host "Копия не создана — обновление остановлено." -ForegroundColor Red; exit 1 }

Write-Host "2/5 Остановка сайта..." -ForegroundColor Cyan
Stop-ScheduledTask -TaskName "Vektor Kanban" -ErrorAction SilentlyContinue
# cmd.exe из задачи может оставить python — завершаем только процесс этого проекта
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*serve.py*" -and $_.ExecutablePath -like "$ProjectDir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Write-Host "3/5 Загрузка новой версии (git pull)..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "git pull не удался. Сайт запускается на старой версии." -ForegroundColor Red
    Start-ScheduledTask -TaskName "Vektor Kanban"
    exit 1
}

Write-Host "4/5 Зависимости..." -ForegroundColor Cyan
& $python -m pip install --disable-pip-version-check -q -r requirements.txt

Write-Host "5/5 Запуск..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName "Vektor Kanban"
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        if ((Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2).StatusCode -eq 200) {
            Write-Host "Готово! Сайт обновлён и работает." -ForegroundColor Green
            exit 0
        }
    } catch { }
}
Write-Host "Сайт не ответил. Лог: $ProjectDir\instance\logs\server.log" -ForegroundColor Yellow
