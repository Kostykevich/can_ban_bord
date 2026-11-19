<#
  Диагностика: почему сайт не открывается по домену (а по 127.0.0.1:8000 открывается).
  Проверяет по цепочке: задачи -> процессы -> порты -> сайт -> Caddy/HTTPS -> сертификат -> DNS -> брандмауэр.

  Запуск (PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА), из папки проекта:
      powershell -ExecutionPolicy Bypass -File deploy\check-site.ps1
  Другой домен:  ... -File deploy\check-site.ps1 -Domain kanban.vektor-rp.ru

  Секреты из .env на экран не выводятся.
#>
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$CaddyDir = "C:\caddy",
    [string]$Domain = ""
)

$ErrorActionPreference = "Continue"
$script:problems = 0
function Ok($t)   { Write-Host "  [OK]  $t" -ForegroundColor Green }
function Bad($t)  { $script:problems++; Write-Host "  [!!]  $t" -ForegroundColor Red }
function Warn($t) { Write-Host "  [?]   $t" -ForegroundColor Yellow }
function Head($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }

# ---------- домен из .env ----------
$envFile = Join-Path $ProjectDir ".env"
if (-not $Domain -and (Test-Path $envFile)) {
    $line = Get-Content $envFile -Encoding UTF8 | Where-Object { $_ -match '^\s*ALLOWED_HOSTS\s*=' } | Select-Object -First 1
    if ($line) { $Domain = (($line -split '=', 2)[1] -split ',')[0].Trim() }
}
if (-not $Domain) { Write-Host "Не удалось узнать домен. Запустите с -Domain ваш.домен" -ForegroundColor Red; exit 1 }
Write-Host "Домен: $Domain"
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Write-Host "Сервер включён с: $boot"

# ---------- 1. Задачи ----------
Head "1. Задачи Планировщика"
foreach ($n in "Vektor Kanban", "Caddy", "Vektor Watchdog", "Vektor AutoDeploy") {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if (-not $t) {
        if ($n -like "Vektor [WA]*") { Warn "Задачи «$n» нет. Перезапустите deploy\install-tasks.ps1" }
        else { Bad "Задачи «$n» нет. Запустите deploy\install-tasks.ps1" }
        continue
    }
    $i = Get-ScheduledTaskInfo -TaskName $n
    $msg = "$n : $($t.State), последний запуск $($i.LastRunTime), код $($i.LastTaskResult)"
    if ($n -notlike "Vektor [WA]*" -and $t.State -ne "Running") { Bad "$msg (задача НЕ работает)" } else { Ok $msg }
    if ($t.State -eq "Disabled") { Bad "$n отключена. Включить: Enable-ScheduledTask -TaskName `"$n`"" }
    if (-not ($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskBootTrigger" -and $_.Enabled })) {
        Bad "$n не запускается при включении сервера. Перезапустите deploy\install-tasks.ps1"
    }
}

# ---------- 2. Процессы ----------
Head "2. Процессы"
$caddy = Get-Process caddy -ErrorAction SilentlyContinue
if ($caddy) { Ok "caddy.exe работает (PID $($caddy.Id -join ', '), запущен $($caddy[0].StartTime))" }
else { Bad "caddy.exe НЕ запущен. Именно поэтому домен не открывается. Запуск: Start-ScheduledTask -TaskName Caddy" }
$py = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*serve.py*" }
if ($py) { Ok "Сайт (serve.py) работает (PID $($py.ProcessId -join ', '))" } else { Bad "serve.py не запущен" }

# ---------- 3. Порты ----------
Head "3. Кто слушает порты 80 / 443 / 8000"
foreach ($p in 80, 443, 8000) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { Bad "Порт $p никто не слушает"; continue }
    $proc = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($c.OwningProcess -eq 4) { $proc = "System (обычно IIS/HTTP.sys)" }
    $text = "Порт $p ($($c.LocalAddress)): $proc"
    if ($p -ne 8000 -and $proc -ne "caddy") { Bad "$text. Порт занят НЕ Caddy (IIS? см. DEPLOY.md, шаг 4)" } else { Ok $text }
}

# ---------- 4. Сам сайт ----------
Head "4. Сайт внутри сервера"
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
    Ok "http://127.0.0.1:8000/health -> $($r.StatusCode)"
} catch { Bad "http://127.0.0.1:8000/health не отвечает: $($_.Exception.Message)" }

# ---------- 5. Caddy: HTTPS через сам сервер ----------
Head "5. HTTPS через Caddy (запрос к 127.0.0.1:443 с именем домена)"
try {
    $tcp = New-Object Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", 443)
    $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, { $true })
    $ssl.AuthenticateAsClient($Domain)
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
    $days = [int]($cert.NotAfter - (Get-Date)).TotalDays
    $who = "выдан: $($cert.Issuer -replace '^.*?CN=([^,]+).*$', '$1'), действует до $($cert.NotAfter) (осталось $days дн.)"
    if ($cert.Subject -notlike "*$Domain*" -and ($cert.DnsNameList.Unicode -notcontains $Domain)) { Bad "Сертификат не для $Domain ($($cert.Subject)), $who" }
    elseif ($days -lt 0) { Bad "Сертификат ПРОСРОЧЕН, $who" }
    elseif ($days -lt 7) { Warn "Сертификат скоро истечёт, $who. Caddy не может его продлить: см. лог Caddy ниже" }
    else { Ok "Сертификат в порядке, $who" }
    $ssl.Dispose(); $tcp.Close()
} catch { Bad "Caddy не отдаёт HTTPS для $Domain : $($_.Exception.InnerException.Message) $($_.Exception.Message)" }

$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    $code = & curl.exe -s -k -o NUL -w "%{http_code}" --max-time 10 --resolve "${Domain}:443:127.0.0.1" "https://$Domain/health"
    if ($code -eq "200") { Ok "https://$Domain/health через Caddy -> 200" }
    elseif ($code -eq "502") { Bad "Caddy отвечает 502: не видит сайт на 127.0.0.1:8000" }
    else { Bad "https://$Domain/health через Caddy -> код '$code'" }
}

# ---------- 6. DNS ----------
Head "6. DNS: куда указывает домен"
$myIps = @()
try { $myIps += (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim() } catch { Warn "Не удалось узнать внешний IP сервера" }
$myIps += (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" }).IPAddress
if ($myIps) { Write-Host "  IP этого сервера: $(($myIps | Select-Object -Unique) -join ', ')" }
foreach ($dns in "8.8.8.8", "1.1.1.1", "77.88.8.8") {
    try {
        $ans = (Resolve-DnsName $Domain -Server $dns -Type A -DnsOnly -ErrorAction Stop | Where-Object { $_.Type -eq "A" }).IPAddress
        if (-not $ans) { Bad "DNS $dns : у домена нет A-записи"; continue }
        if ($ans | Where-Object { $myIps -contains $_ }) { Ok "DNS $dns : $($ans -join ', ')" }
        else { Bad "DNS $dns : $($ans -join ', ') — это НЕ IP сервера. Проверьте A-запись у регистратора и не истёк ли домен" }
    } catch { Bad "DNS $dns : домен не находится ($($_.Exception.Message)). Возможно, домен истёк или удалена запись" }
}
try {
    $aaaa = Resolve-DnsName $Domain -Server 8.8.8.8 -Type AAAA -DnsOnly -ErrorAction Stop | Where-Object { $_.Type -eq "AAAA" }
    if ($aaaa) { Warn "У домена есть IPv6 (AAAA): $($aaaa.IPAddress -join ', '). Если IPv6 сервера не настроен, удалите AAAA-запись" }
} catch { }

# ---------- 7. Брандмауэр ----------
Head "7. Брандмауэр Windows"
foreach ($p in 80, 443) {
    $rule = Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq "$p" } |
        Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq "True" -and $_.Direction -eq "Inbound" -and $_.Action -eq "Allow" }
    if ($rule) { Ok "Порт $p открыт правилом «$($rule[0].DisplayName)»" } else { Bad "Нет разрешающего правила для порта $p (DEPLOY.md, шаг 4)" }
}
Write-Host "  Брандмауэр в панели хостинга отсюда не проверить. Если всё выше [OK], проверьте его."

# ---------- 8. Логи ----------
Head "8. Последние ошибки Caddy"
$caddyLog = Join-Path $CaddyDir "logs\caddy.log"
if (Test-Path $caddyLog) {
    $err = Get-Content $caddyLog -Tail 400 -Encoding UTF8 | Where-Object { $_ -match '"level":"(error|fatal|warn)"|panic|exit' } | Select-Object -Last 10
    if ($err) { $err | ForEach-Object { Write-Host "  $_" } } else { Ok "Ошибок в последних строках нет" }
} else { Warn "Нет $caddyLog : старые задачи не сохраняли вывод Caddy. Перезапустите deploy\install-tasks.ps1" }
$wd = Join-Path $ProjectDir "instance\logs\watchdog.log"
$dl = Join-Path $ProjectDir "instance\logs\deploy.log"
if (Test-Path $dl) {
    Write-Host "  Автодеплой (последние события):"
    Get-Content $dl -Tail 300 -Encoding UTF8 | Where-Object { $_ -match '^\d{4}-\d\d-\d\d ' } | Select-Object -Last 5 | ForEach-Object { Write-Host "    $_" }
}
if (Test-Path $wd) { Write-Host "  Сторож (последние записи):"; Get-Content $wd -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host "    $_" } }

Write-Host ""
if ($script:problems -eq 0) {
    Write-Host "Со стороны сервера всё в порядке. Если с ПК сайт всё равно не открывается: брандмауэр в панели хостинга, блокировка у провайдера или DNS-кэш (ipconfig /flushdns на ПК)." -ForegroundColor Green
} else {
    Write-Host "Найдено проблем: $($script:problems). Смотрите строки [!!] сверху вниз, первая обычно и есть причина." -ForegroundColor Red
}
