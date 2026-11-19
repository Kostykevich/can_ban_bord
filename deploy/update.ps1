<#
  Обновление сайта на VDS без потери данных:
    1) резервная копия базы;
    2) git pull — новая версия кода (сайт пока работает на старой);
    3) зависимости — только если изменился requirements.txt;
    4) автотесты на новой версии — если упали, откат, сайт не трогаем;
    5) перезапуск сайта и проверка — если не поднялся, откат на прошлую версию.

  Вручную (PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА), из папки проекта:
      powershell -ExecutionPolicy Bypass -File deploy\update.ps1
  Автоматически его вызывает deploy\auto-deploy.ps1 (задача «Vektor AutoDeploy») с ключом -Auto.

  Код выхода: 0 — обновлено; 1 — не обновлено, сайт на старой версии;
              2 — новая версия сломана, выполнен откат; 3 — откат не помог, нужен человек.
#>
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Auto
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectDir
$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
if ($Auto) { try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { } }

# задача Планировщика работает от SYSTEM, а папка принадлежит Administrator — без этого git откажется работать
function Invoke-Git { & git -c safe.directory=* @args }
function Say($text, $color = "Cyan") { Write-Host "$(Get-Date -Format 'HH:mm:ss') $text" -ForegroundColor $color }

function Stop-Site {
    Stop-ScheduledTask -TaskName "Vektor Kanban" -ErrorAction SilentlyContinue
    # cmd.exe из задачи может оставить python — завершаем только процесс этого проекта
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*serve.py*" -and $_.ExecutablePath -like "$ProjectDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

function Start-SiteAndWait {
    Start-ScheduledTask -TaskName "Vektor Kanban"
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        try {
            if ((Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2).StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

function Install-Requirements {
    & $python -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой" }
}

$old = (Invoke-Git rev-parse HEAD).Trim()

function Restore-Old($reason, $reqChanged) {
    Say "$reason Откат на прошлую версию $($old.Substring(0, 7))..." Yellow
    Invoke-Git reset -q --keep $old
    if ($LASTEXITCODE -ne 0) { Say "git reset не удался. Нужна ручная проверка." Red; exit 3 }
    if ($reqChanged) { Stop-Site; Install-Requirements }
    Stop-Site
    if (Start-SiteAndWait) { Say "Откат выполнен, сайт работает на прошлой версии." Yellow; exit 2 }
    Say "Сайт не поднялся даже после отката. Лог: $ProjectDir\instance\logs\server.log" Red
    exit 3
}

Say "1/5 Резервная копия базы..."
& $python scripts\backup_db.py update
if ($LASTEXITCODE -ne 0) { Say "Копия не создана — обновление остановлено." Red; exit 1 }

Say "2/5 Загрузка новой версии (git pull)..."
Invoke-Git pull -q --ff-only
if ($LASTEXITCODE -ne 0) { Say "git pull не удался. Сайт работает на старой версии." Red; exit 1 }
$new = (Invoke-Git rev-parse HEAD).Trim()
if ($new -eq $old) {
    Say "Новой версии нет." Green
    if (-not $Auto) {
        $alive = $false
        try { $alive = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3).StatusCode -eq 200 } catch { }
        if (-not $alive) { Stop-Site; if (Start-SiteAndWait) { Say "Сайт запущен." Green } else { Say "Сайт не ответил." Red; exit 1 } }
    }
    exit 0
}
Say "   $($old.Substring(0, 7)) -> $($new.Substring(0, 7)): $(Invoke-Git log -1 --format=%s)"

Say "3/5 Зависимости..."
Invoke-Git diff --quiet $old $new -- requirements.txt
$reqChanged = $LASTEXITCODE -ne 0
if ($reqChanged) {
    # на Windows нельзя заменить файлы библиотек, пока их держит работающий сайт
    Stop-Site
    try { Install-Requirements } catch { Restore-Old "Не удалось установить зависимости." $true }
} else { Say "   requirements.txt не менялся, пропускаю." }

Say "4/5 Автотесты на новой версии..."
& $python -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { Restore-Old "Тесты не прошли — новая версия НЕ выложена." $reqChanged }

Say "5/5 Перезапуск сайта..."
Stop-Site
if (Start-SiteAndWait) { Say "Готово! Сайт обновлён до $($new.Substring(0, 7)) и работает." Green; exit 0 }
Restore-Old "Новая версия не запустилась (лог: instance\logs\server.log)." $reqChanged
