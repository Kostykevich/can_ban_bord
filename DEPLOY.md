# Перенос на VDS (Windows Server)

Схема: **интернет → Caddy (HTTPS, порт 443) → Waitress (127.0.0.1:8000) → приложение**.
Waitress слушает только локальный адрес, поэтому напрямую из интернета он недоступен.
Caddy сам получает и продлевает бесплатный сертификат Let's Encrypt.

> Встроенный сервер Flask (`app.py`) и `debug=True` на сервере **не используются**.
> Секреты не присылайте никому в чат — вводите их только в `.env` на сервере.

## Что копировать на сервер

| Копировать | Не копировать |
|---|---|
| `vektor\`, `templates\`, `static\`, `scripts\`, `tests\` | `.venv\` — на сервере создаётся заново |
| `app.py`, `serve.py`, `requirements.txt`, `.env.example`, `*.md` | `.env` — на сервере создаётся свой, с другими ключами |
| `instance\kanban.db` и `instance\uploads\` — **только если** хотите перенести локальные карточки | `__pycache__\`, `instance\secret_key`, `instance\backups\` |

Проще всего через git: залейте проект в **приватный** репозиторий (`.gitignore` уже исключает секреты и базу)
и на сервере выполните `git clone`.

## 1. Подготовка сервера

1. Привяжите домен (например `kanban.vektor-rp.ru`): A-запись → IP вашего VDS.
2. Установите Python 3.12 с https://www.python.org/downloads/windows/ — отметьте **Add python.exe to PATH**.
3. Установите Git для Windows (если будете использовать git).
4. Брандмауэр: откройте входящие **80** и **443** (PowerShell от администратора):

```powershell
New-NetFirewallRule -DisplayName "HTTP (Caddy)" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
New-NetFirewallRule -DisplayName "HTTPS (Caddy)" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

Порт 8000 **не открывайте**.

## 2. Код и зависимости

```powershell
cd C:\
git clone https://github.com/ВАШ_АККАУНТ/vektor-kanban.git
cd C:\vektor-kanban
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Если переносите локальные данные — скопируйте `instance\kanban.db` и `instance\uploads\` в `C:\vektor-kanban\instance\`.

## 3. Настройки `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Заполните:

```
VEKTOR_SECRET_KEY=<новый ключ: python -c "import secrets; print(secrets.token_hex(32))">
PUBLIC_URL=https://kanban.vektor-rp.ru
DISCORD_REDIRECT_URI=https://kanban.vektor-rp.ru/auth/discord/callback
HTTPS=1
TRUST_PROXY=1
ALLOWED_HOSTS=kanban.vektor-rp.ru
HOST=127.0.0.1
PORT=8000
```

плюс `DISCORD_*` и `TELEGRAM_BOT_TOKEN`. В Discord Developer Portal добавьте новый Redirect с https-адресом.
**Остановите бота на своём ПК**, иначе Telegram не даст работать двум копиям.

Если база новая — создайте администратора:

```powershell
.\.venv\Scripts\python.exe scripts\create_admin.py
```

Проверьте запуск:

```powershell
.\.venv\Scripts\python.exe serve.py
```

В браузере на самом сервере откройте http://127.0.0.1:8000 — должна быть страница входа. `Ctrl + C` — остановить.

## 4. HTTPS через Caddy

1. Скачайте `caddy_windows_amd64.exe` с https://caddyserver.com/download, переименуйте в `caddy.exe`,
   положите в `C:\caddy\`.
2. Создайте `C:\caddy\Caddyfile`:

```
kanban.vektor-rp.ru {
    encode gzip
    request_body {
        max_size 7MB
    }
    reverse_proxy 127.0.0.1:8000
}
```

3. Проверка: `C:\caddy\caddy.exe run --config C:\caddy\Caddyfile` → откройте https://kanban.vektor-rp.ru.

## 5. Автозапуск (Планировщик заданий)

Создайте две задачи (PowerShell от администратора). Они стартуют при включении сервера,
даже если никто не вошёл в систему, и перезапускаются при сбое:

```powershell
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
$trigger = New-ScheduledTaskTrigger -AtStartup

$app = New-ScheduledTaskAction -Execute "C:\vektor-kanban\.venv\Scripts\python.exe" -Argument "serve.py" -WorkingDirectory "C:\vektor-kanban"
Register-ScheduledTask -TaskName "Vektor Kanban" -Action $app -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest

$caddy = New-ScheduledTaskAction -Execute "C:\caddy\caddy.exe" -Argument "run --config C:\caddy\Caddyfile" -WorkingDirectory "C:\caddy"
Register-ScheduledTask -TaskName "Caddy" -Action $caddy -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest

Start-ScheduledTask -TaskName "Vektor Kanban"
Start-ScheduledTask -TaskName "Caddy"
```

## 6. Резервное копирование

Ежедневная копия в 04:00 (хранятся последние 30):

```powershell
$backup = New-ScheduledTaskAction -Execute "C:\vektor-kanban\.venv\Scripts\python.exe" -Argument "scripts\backup_db.py" -WorkingDirectory "C:\vektor-kanban"
Register-ScheduledTask -TaskName "Vektor Backup" -Action $backup -Trigger (New-ScheduledTaskTrigger -Daily -At 4am) -User "SYSTEM"
```

Время от времени скачивайте папку `instance\backups` и `instance\uploads` к себе: копия на том же сервере не спасёт, если сервер пропадёт.

## 7. Обновление без потери данных

```powershell
cd C:\vektor-kanban
.\.venv\Scripts\python.exe scripts\backup_db.py
Stop-ScheduledTask -TaskName "Vektor Kanban"
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Start-ScheduledTask -TaskName "Vektor Kanban"
```

Миграции базы применяются сами при запуске, перед ними автоматически делается копия.

## 8. Проверка после запуска

- https://ваш-домен открывается, в адресной строке замок.
- Без входа доска недоступна, редирект на страницу входа.
- Вход по логину и через Discord работает.
- Карточка создаётся, переносится, после перезагрузки остаётся на месте.
- В профиле «Отправить тестовое уведомление» доходит в Discord и Telegram.
- `http://IP-сервера:8000` снаружи **не** открывается.

## Что проверить дополнительно перед открытием команде

- Пароли администраторов длинные и нигде не записаны в открытом виде.
- `.env` есть только на сервере, не в git.
- Windows Server обновляется, RDP закрыт сильным паролем (лучше — только с ваших IP).
- Защита от перебора паролей базовая: она в памяти и сбрасывается при перезапуске.
  Полной гарантии безопасности она не даёт.
