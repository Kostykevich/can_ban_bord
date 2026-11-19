# Установка Vektor RP Kanban на VDS (Windows Server) — подробный гайд

Гайд для новичка: каждое действие расписано по шагам. Займёт 40–60 минут.
Подходит для Windows Server 2019 / 2022 / 2025.

## Как это будет устроено

```
Игроки и команда ──HTTPS──▶ Caddy (порты 80/443) ──▶ Waitress 127.0.0.1:8000 ──▶ сайт + SQLite
                                                       │
                                     Discord-бот, Telegram-бот (работают внутри сайта)
```

- **Waitress** — сервер, который запускает сайт. Слушает только внутренний адрес `127.0.0.1`,
  из интернета напрямую недоступен.
- **Caddy** — «привратник»: принимает запросы из интернета по HTTPS и передаёт их Waitress.
  Сам получает бесплатный сертификат Let's Encrypt и сам его продлевает.
- **Планировщик заданий Windows** запускает всё при включении сервера и перезапускает при сбое.

> 🔐 **Правила безопасности на весь гайд**
> - Пароли, токены и ключи вводите **только** в файл `.env` на сервере. Никому не отправляйте их в чат.
> - Встроенный сервер Flask (`app.py`) на VDS **не используется**, только `serve.py`.
> - Порт 8000 наружу **не открывается**.

## Что понадобится

| Что | Где взять |
|---|---|
| VDS с Windows Server, 2 ГБ ОЗУ или больше | у хостинга. Нужны IP-адрес, логин `Administrator` и пароль |
| Домен или поддомен, например `kanban.vektor-rp.ru` | у регистратора домена. Без домена HTTPS не получить |
| Доступ к репозиторию проекта на GitHub | ваш аккаунт GitHub |
| (необязательно) токены Discord и Telegram | см. README.md |

В командах ниже `kanban.vektor-rp.ru` — пример. Везде подставляйте свой домен.

---

## Шаг 1. Подключиться к серверу

1. На своём ПК нажмите `Win + R`, введите `mstsc`, нажмите Enter.
2. В поле «Компьютер» введите IP-адрес VDS → «Подключить».
3. Логин `Administrator` и пароль от хостинга. На предупреждение о сертификате ответьте «Да».

Дальше все действия выполняются **на сервере**, в окне удалённого рабочего стола.

**PowerShell от имени администратора** понадобится много раз. Чтобы его открыть:
`Пуск` → наберите `PowerShell` → правой кнопкой → **«Запуск от имени администратора»**.

## Шаг 2. Обновить Windows

`Пуск` → «Параметры» → «Обновление и безопасность» → «Проверить наличие обновлений».
Установите всё и перезагрузите сервер. Потом подключитесь заново (шаг 1).

## Шаг 3. Направить домен на сервер

В панели регистратора домена создайте **A-запись**:

| Тип | Имя | Значение |
|---|---|---|
| A | `kanban` (или `@` для основного домена) | IP-адрес VDS |

Через 5–30 минут проверьте в PowerShell:

```powershell
Resolve-DnsName kanban.vektor-rp.ru
```

В ответе должен быть IP вашего VDS. Пока его нет, шаг 10 (HTTPS) не получится. Остальное можно делать.

## Шаг 4. Открыть порты 80 и 443

В PowerShell от администратора:

```powershell
New-NetFirewallRule -DisplayName "Vektor HTTP (Caddy)"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
New-NetFirewallRule -DisplayName "Vektor HTTPS (Caddy)" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

> У некоторых хостингов есть ещё и свой брандмауэр в панели управления. Откройте 80 и 443 и там.

Проверьте, не занят ли порт 80 сервером IIS (бывает установлен заранее):

```powershell
Get-NetTCPConnection -LocalPort 80,443 -State Listen -ErrorAction SilentlyContinue
```

Если что-то нашлось, а IIS вам не нужен, отключите его:

```powershell
Stop-Service W3SVC; Set-Service W3SVC -StartupType Disabled
```

## Шаг 5. Установить Python и Git

**Python 3.12:**

1. Откройте браузер на сервере → https://www.python.org/downloads/windows/ →
   «Windows installer (64-bit)» версии **3.12.x**.
2. Запустите установщик. **Обязательно отметьте галочку «Add python.exe to PATH»** внизу первого окна.
3. «Install Now» → дождитесь «Setup was successful».

**Git:** https://git-scm.com/download/win → «64-bit Git for Windows Setup» → установка со всеми настройками
по умолчанию (просто «Next» до конца).

**Закройте и откройте PowerShell заново**, чтобы он увидел новые программы, и проверьте:

```powershell
py -3.12 --version
git --version
```

Должны появиться номера версий. Если пишет «не является командой», перезапустите PowerShell или сервер.

## Шаг 6. Скачать проект и установить зависимости

Проект будет лежать в `C:\vektor-kanban`. В PowerShell **от администратора**:

```powershell
cd C:\
git clone https://github.com/ВАШ_АККАУНТ/ВАШ_РЕПОЗИТОРИЙ.git vektor-kanban
cd C:\vektor-kanban
```

> Репозиторий приватный? Git откроет окно входа в GitHub. Войдите через браузер, это безопасно.

Создайте виртуальное окружение и установите зависимости:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> `.venv` с вашего ПК **не копируется**: на сервере он создаётся заново этой командой.

## Шаг 7. Настроить `.env`

```powershell
Copy-Item deploy\vds.env.example .env
notepad .env
```

> 💡 В `deploy\vds.env.example` уже стоят все серверные настройки и есть пометки, что куда вписывать.
> **IP сервера в `.env` не пишется**, только домен. `HOST=127.0.0.1` и `PORT=8000` не меняйте.

Сгенерируйте **новый** секретный ключ (не тот, что на вашем ПК):

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Заполните `.env` так (домен — свой):

```ini
VEKTOR_SECRET_KEY=<вставьте сгенерированный ключ>
PUBLIC_URL=https://kanban.vektor-rp.ru

# Вход через Discord (если нужен)
DISCORD_CLIENT_ID=<из Discord Developer Portal>
DISCORD_CLIENT_SECRET=<из Discord Developer Portal>
DISCORD_REDIRECT_URI=https://kanban.vektor-rp.ru/auth/discord/callback

# Discord-бот (если нужен)
DISCORD_BOT_TOKEN=<токен бота>
DISCORD_CHANNEL_ID=<ID канала для общих уведомлений>

# Telegram-бот (если нужен)
TELEGRAM_BOT_TOKEN=<токен от @BotFather>

# Обязательно для сервера
HTTPS=1
TRUST_PROXY=1
ALLOWED_HOSTS=kanban.vektor-rp.ru
HOST=127.0.0.1
PORT=8000
```

Сохраните (`Ctrl + S`) и закройте Блокнот.

| Параметр | Зачем |
|---|---|
| `VEKTOR_SECRET_KEY` | подписывает сессии. Если ключ утечёт, кто-то сможет подделать вход. Если сменить ключ, всех разлогинит |
| `PUBLIC_URL` | адрес сайта для ссылок в уведомлениях Discord и Telegram |
| `HTTPS=1` | cookie сессии передаются только по HTTPS, включается HSTS |
| `TRUST_PROXY=1` | сайт верит заголовкам от Caddy (настоящий IP игрока, https) |
| `ALLOWED_HOSTS` | сайт отвечает только на ваш домен |
| `HOST=127.0.0.1` | Waitress недоступен из интернета напрямую, только через Caddy |

**Discord Developer Portal:** в вашем приложении → OAuth2 → Redirects → **добавьте**
`https://kanban.vektor-rp.ru/auth/discord/callback` и сохраните. Локальный адрес можно оставить.

> ⚠️ **Telegram:** один бот может работать только в одном месте. Перед запуском на сервере
> **остановите сайт на своём ПК** (или уберите `TELEGRAM_BOT_TOKEN` из локального `.env`).

## Шаг 8. Данные: новая база или перенос с ПК

**Вариант А — начать с чистой базы.** Создайте администратора:

```powershell
.\.venv\Scripts\python.exe scripts\create_admin.py
```

Скрипт спросит логин, имя и пароль. Пароль при вводе **не отображается**, это нормально.

**Вариант Б — перенести карточки с ПК.**

1. **На ПК** остановите сайт (`Ctrl + C`) и сделайте копию:
   `.\.venv\Scripts\python.exe scripts\backup_db.py`
2. Скопируйте с ПК на сервер (через удалённый рабочий стол работает обычное «Копировать → Вставить» файлов):
   - `instance\kanban.db` → `C:\vektor-kanban\instance\kanban.db`
   - папку `instance\uploads` → `C:\vektor-kanban\instance\uploads` (скриншоты)
   - папку `instance\avatars` → `C:\vektor-kanban\instance\avatars` (аватарки)
3. Файлы `kanban.db-wal` и `kanban.db-shm` копировать не нужно, если сайт на ПК был остановлен.
4. `instance\secret_key` и `instance\backups` **не копируйте**.

Пользователи и пароли перенесутся вместе с базой.

## Шаг 9. Пробный запуск

```powershell
cd C:\vektor-kanban
.\.venv\Scripts\python.exe serve.py
```

Должно появиться: `[Vektor] Сервер запущен: http://127.0.0.1:8000`, а если настроены боты,
то ещё `[Discord] Бот … в сети` и `[Telegram] Бот @… в сети`.

Откройте в браузере **на сервере** http://127.0.0.1:8000. Должна открыться страница входа.
Остановите сервер: `Ctrl + C`.

## Шаг 10. HTTPS через Caddy

В PowerShell от администратора:

```powershell
New-Item -ItemType Directory -Force C:\caddy
Invoke-WebRequest -UseBasicParsing -OutFile C:\caddy\caddy.exe "https://caddyserver.com/api/download?os=windows&arch=amd64"
Copy-Item C:\vektor-kanban\deploy\Caddyfile.example C:\caddy\Caddyfile
notepad C:\caddy\Caddyfile
```

В Блокноте замените `kanban.example.ru` на свой домен, сохраните.

Проверка (сайт из шага 9 должен быть запущен **во втором окне** PowerShell):

```powershell
C:\caddy\caddy.exe run --config C:\caddy\Caddyfile
```

Через 10–30 секунд Caddy получит сертификат. Откройте `https://kanban.vektor-rp.ru` **со своего ПК**:
должна открыться страница входа с замком в адресной строке. Остановите оба окна (`Ctrl + C`).

> Ошибка с сертификатом? Проверьте шаги 3 (домен указывает на сервер) и 4 (открыты 80 и 443).

## Шаг 11. Автозапуск

Скрипт сам создаст задачи Планировщика:

| Задача | Когда запускается | Что делает |
|---|---|---|
| Vektor Kanban | при включении сервера | сайт |
| Caddy | при включении сервера | HTTPS |
| Vektor Watchdog | при включении + каждые 5 минут | поднимает сайт и Caddy, если упали |
| Vektor AutoDeploy | при включении + каждую минуту | выкладывает новую версию из GitHub |
| Vektor Backup | каждый день в 04:00 | резервная копия базы |

Все задачи работают от SYSTEM: входить на сервер по RDP после перезагрузки не нужно.
В PowerShell **от администратора**:

```powershell
cd C:\vektor-kanban
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1
```

В конце должно быть `Ждём запуска сайта... готово!`.

Проверка: **перезагрузите сервер** и через 1–2 минуты откройте `https://kanban.vektor-rp.ru`
со своего ПК, не подключаясь к серверу по RDP. Сайт должен работать.

Управление вручную (PowerShell от администратора):

```powershell
Get-ScheduledTask -TaskName "Vektor*", "Caddy" | Format-Table TaskName, State   # состояние
Stop-ScheduledTask  -TaskName "Vektor Kanban"                                     # остановить сайт
Start-ScheduledTask -TaskName "Vektor Kanban"                                     # запустить сайт
Get-Content C:\vektor-kanban\instance\logs\server.log -Tail 50 -Encoding UTF8     # последние строки лога
```

## Шаг 12. Проверка перед тем, как дать ссылку команде

- [ ] `https://ваш-домен` открывается, в адресной строке замок.
- [ ] `http://ваш-домен` автоматически переходит на `https://`.
- [ ] `http://IP-сервера:8000` со своего ПК **не** открывается.
- [ ] Без входа доска недоступна, перекидывает на страницу входа.
- [ ] Вход по логину работает. Через Discord тоже, если настроен.
- [ ] Карточка создаётся и переносится, после обновления страницы всё на месте.
- [ ] Скриншот через Ctrl + V загружается.
- [ ] Профиль → «Отправить тестовое уведомление» доходит в Discord и Telegram.
- [ ] После перезагрузки сервера сайт поднимается сам.
- [ ] В `C:\vektor-kanban\instance\backups` появляются копии (первая — в 04:00 или после ручного запуска
      `.\.venv\Scripts\python.exe scripts\backup_db.py`).

---

## Обновление сайта (когда вышла новая версия)

**Автоматически (по умолчанию).** Запушьте изменения в GitHub, и в течение минуты сервер обновится сам.
Задача «Vektor AutoDeploy» по порядку:

1. делает резервную копию базы (`kanban-update-…`, хранятся последние 30 отдельно от ежедневных);
2. скачивает новую версию, пока сайт продолжает работать на старой;
3. обновляет зависимости, если изменился `requirements.txt`;
4. прогоняет автотесты. Если они упали, новая версия **не выкладывается**;
5. перезапускает сайт (перерыв несколько секунд). Если сайт не поднялся, автоматически откатывается на прошлую версию.

Журнал: `C:\vektor-kanban\instance\logs\deploy.log`. Версию, которую выложить не удалось, автодеплой больше
не пробует. Исправьте ошибку и запушьте новый коммит.

> ⚠️ Всё, что попадает в ветку `main`, через минуту оказывается на сервере. Не пушьте в `main` недоделанное.
> Не правьте файлы проекта прямо на сервере: автодеплой увидит изменения и остановится (запись в журнале).

```powershell
Get-Content C:\vektor-kanban\instance\logs\deploy.log -Tail 30 -Encoding UTF8   # что делал автодеплой
Disable-ScheduledTask -TaskName "Vektor AutoDeploy"                            # выключить на время
Enable-ScheduledTask  -TaskName "Vektor AutoDeploy"                            # включить обратно
```

**Вручную** (то же самое, но сразу, с выводом на экран). PowerShell от администратора:

```powershell
cd C:\vektor-kanban
powershell -ExecutionPolicy Bypass -File deploy\update.ps1
```

Миграции базы применяются при запуске сайта автоматически, и перед ними тоже делается копия.

## Резервные копии

- Каждый день в 04:00 копия базы сохраняется в `C:\vektor-kanban\instance\backups`, хранятся последние 30.
- Перед каждой миграцией базы делается отдельная копия `kanban-before-v…`.
- **Раз в неделю скачивайте к себе** папки `instance\backups`, `instance\uploads` и `instance\avatars`.
  Копия на том же сервере не спасёт, если сервер сломается или хостинг его удалит.

**Восстановление из копии:**

```powershell
Stop-ScheduledTask -TaskName "Vektor Kanban"
cd C:\vektor-kanban\instance
Rename-Item kanban.db "kanban-broken-$(Get-Date -Format yyyyMMdd-HHmm).db"
Remove-Item kanban.db-wal, kanban.db-shm -ErrorAction SilentlyContinue
Copy-Item backups\kanban-daily-ГГГГММДД-ЧЧММСС.db kanban.db
Start-ScheduledTask -TaskName "Vektor Kanban"
```

## Если что-то не работает

**Сайт не открывается по домену** (а по `http://127.0.0.1:8000` на сервере открывается). Запустите диагностику,
она проверит всю цепочку и пометит сломанное звено `[!!]`:

```powershell
cd C:ektor-kanban
powershell -ExecutionPolicy Bypass -File deploy\check-site.ps1
```

С версии, где появился этот скрипт, `install-tasks.ps1` создаёт ещё задачу **«Vektor Watchdog»**. Она каждые 5 минут
поднимает Caddy и сайт, если они упали, и пишет об этом в `instance\logs\watchdog.log`. Вывод Caddy теперь
сохраняется в `C:\caddy\logs\caddy.log`.

| Что видите | Что сделать |
|---|---|
| `502 Bad Gateway` | Сайт не запущен. `Start-ScheduledTask -TaskName "Vektor Kanban"`, затем смотрите `instance\logs\server.log` |
| Браузер: «Не удаётся получить доступ к сайту» | Не запущен Caddy, закрыты порты 80/443 (шаг 4) или домен не указывает на сервер (шаг 3) |
| Ошибка сертификата | DNS ещё не обновился или порт 80 занят IIS (шаг 4). Лог: `C:\caddy\logs` |
| `400 Bad Request` при входе на сайт | Домен не совпадает с `ALLOWED_HOSTS` в `.env` |
| После входа снова страница входа | В `.env` стоит `HTTPS=1`, а сайт открыт по `http://`. Открывайте по `https://` |
| Любая ошибка Discord (вход, бот, уведомления) | Сначала запустите `.\.venv\Scripts\python.exe scripts\check_discord.py`: он покажет точную причину |
| «Сервер сайта не может связаться с Discord» | Хостинг блокирует Discord (частая ситуация с VDS в РФ). Варианты: VDS вне РФ или HTTP-прокси в `.env`: `DISCORD_PROXY=http://логин:пароль@адрес:порт`, затем перезапуск сайта |
| «Discord отклонил ключи приложения» | Неверный Client Secret. Developer Portal → OAuth2 → Reset Secret → вставьте в `.env` без пробелов → перезапуск |
| «Discord не принял код входа» | `DISCORD_REDIRECT_URI` не совпадает с Redirect в Developer Portal (http/https, слеш в конце, домен) |
| Discord: `Invalid OAuth2 redirect_uri` | Адрес в `DISCORD_REDIRECT_URI` не совпадает **символ в символ** с Redirect в Discord Developer Portal |
| Discord-бот не пишет в личку | У человека нет общего сервера с ботом, закрыты ЛС от участников сервера или не привязан Discord в профиле |
| В логе `Telegram … 409` | Бот запущен где-то ещё (например на вашем ПК). Остановите вторую копию |
| В логе `неверный DISCORD_BOT_TOKEN` | Токен устарел. Developer Portal → Bot → Reset Token → обновите `.env` и перезапустите сайт |
| Забыли пароль администратора | `.\.venv\Scripts\python.exe scripts\create_admin.py` → введите свой логин → ответьте «да» |
| После изменения `.env` ничего не поменялось | Перезапустите сайт: `Stop-ScheduledTask` и `Start-ScheduledTask` для «Vektor Kanban» |

## Безопасность сервера — что проверить дополнительно

- Включите автоматические обновления Windows.
- Пароль `Administrator` длинный и уникальный. По возможности разрешите RDP (порт 3389)
  только со своих IP: «Брандмауэр Защитника Windows» → «Правила для входящих» → «Удалённый рабочий стол» → «Область».
- `.env` существует только на сервере и никогда не попадает в git (он в `.gitignore`).
- Пароли администраторов сайта длинные, логины не очевидные.
- Ограничение попыток входа на сайт — базовое: хранится в памяти и сбрасывается при перезапуске.
  Полной защиты от всех атак оно не даёт.
