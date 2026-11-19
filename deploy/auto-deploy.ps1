<#
  Автодеплой: задача «Vektor AutoDeploy» запускает этот скрипт каждую минуту (создаёт install-tasks.ps1).
  Если в GitHub появилась новая версия, вызывает deploy\update.ps1 -Auto:
  копия базы, тесты, перезапуск, а при ошибке — откат на прошлую версию.

  Журнал: instance\logs\deploy.log
  Версия, которую выложить не удалось, запоминается в instance\logs\deploy.skip и больше не пробуется,
  пока не появится следующий коммит. Отключить автодеплой: Disable-ScheduledTask -TaskName "Vektor AutoDeploy"
#>
param([string]$ProjectDir = (Split-Path -Parent $PSScriptRoot))

Set-Location $ProjectDir
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$logs = Join-Path $ProjectDir "instance\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs "deploy.log"
$skipFile = Join-Path $logs "deploy.skip"
function Log($t) { Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $t" -Encoding UTF8 }
function Invoke-Git { & git -c safe.directory=* @args }
function Skip($commit, $why) { Log "$why Версия $($commit.Substring(0, 7)) пропущена."; Set-Content -Path $skipFile -Value $commit }

$branch = (Invoke-Git rev-parse --abbrev-ref HEAD)
if ($LASTEXITCODE -ne 0) { Log "git не работает в $ProjectDir"; exit 1 }
Invoke-Git fetch -q origin $branch 2>$null
if ($LASTEXITCODE -ne 0) { exit 0 }  # нет связи с GitHub — попробуем через минуту

$local = (Invoke-Git rev-parse HEAD).Trim()
$remote = (Invoke-Git rev-parse "origin/$branch").Trim()
if ($local -eq $remote) { exit 0 }
if ((Test-Path $skipFile) -and ((Get-Content $skipFile -TotalCount 1).Trim() -eq $remote)) { exit 0 }

Invoke-Git merge-base --is-ancestor $local $remote
if ($LASTEXITCODE -ne 0) { Skip $remote "На сервере есть коммиты, которых нет в GitHub (история разошлась). Обновите вручную."; exit 1 }
if (Invoke-Git status --porcelain --untracked-files=no) {
    Skip $remote "На сервере вручную изменены файлы проекта (git status). Верните их: git checkout -- . и обновите вручную."
    exit 1
}

Log "Новая версия $($remote.Substring(0, 7)): $(Invoke-Git log -1 --format=%s $remote). Обновляю..."
$update = Join-Path $ProjectDir "deploy\update.ps1"
# отдельный процесс + перенаправление через cmd: вывод git/pip/тестов попадает в журнал как есть
cmd.exe /c "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$update`" -Auto >> `"$log`" 2>&1"
$code = $LASTEXITCODE
switch ($code) {
    0 { Log "Обновление выполнено."; Remove-Item $skipFile -ErrorAction SilentlyContinue }
    2 { Skip $remote "Новая версия не прошла проверку, выполнен откат." }
    3 { Skip $remote "ВНИМАНИЕ: откат не помог, сайт может не работать!" }
    default { Log "Обновление не выполнено (код $code), повторю через минуту." }
}
exit $code
