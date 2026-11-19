"""Telegram-бот для личных уведомлений.

Как пользователь подключает уведомления:
1. В профиле на сайте нажимает «Подключить Telegram» — сайт создаёт одноразовый код.
2. Переходит по ссылке t.me/<бот>?start=<код> и нажимает «Start».
3. Бот получает /start <код>, находит пользователя и запоминает его chat_id.

Что умеет бот:
  /start    — приветствие и подключение аккаунта
  /settings — какие уведомления присылать (кнопки-переключатели прямо в чате)
  /status   — к какому аккаунту подключён чат
  /help     — справка
  /stop     — отключить уведомления

При запуске бот сам оформляет свой профиль: имя, описание и меню команд
(PROFILE ниже). Аватар ставится скриптом scripts/setup_telegram_bot.py.

Бот работает через long polling (getUpdates) в фоновом потоке — не нужен
публичный адрес и HTTPS-вебхук, поэтому работает и на локальном ПК.

Если хостинг блокирует api.telegram.org, укажите прокси в .env: TELEGRAM_PROXY
(или общий HTTPS_PROXY). Проверка связи: scripts/check_telegram.py
"""

import html
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import tls
from .constants import ROLES
from .db import connect, utcnow

BRAND = "Vektor RP"

# Профиль бота в Telegram: применяется при каждом запуске, если отличается
PROFILE = {
    "name": "Vektor RP · Уведомления",
    "short_description": "Уведомления канбан-доски Vektor RP: задачи, упоминания, комментарии и проверки.",
    "description": (
        "Бот уведомлений канбан-доски Vektor RP.\n\n"
        "🔔 Пришлёт сообщение, когда вам назначат задачу, упомянут через @, "
        "прокомментируют вашу карточку или отправят её на проверку.\n\n"
        "Чтобы подключиться, откройте профиль на сайте и нажмите «Подключить Telegram»."
    ),
    "commands": [
        ("settings", "⚙️ Какие уведомления присылать"),
        ("status", "👤 Мой подключённый аккаунт"),
        ("help", "❓ Справка"),
        ("stop", "🔕 Отключить уведомления"),
    ],
}

# События, на которые можно подписаться (те же, что в профиле на сайте — vektor/notify.py)
EVENT_LABELS = {
    "assigned": "👤 Мне назначили задачу",
    "mention": "📣 Меня упомянули через @",
    "comment": "💬 Комментарии в моих задачах",
    "status": "🔀 Мою задачу переместили",
    "review": "🔎 Задача на проверке",
    "new_card": "🆕 Новые карточки",
}

LINE = "━━━━━━━━━━━━━━"


class TelegramError(Exception):
    pass


def proxy_url():
    """Прокси для связи с Telegram, если хостинг его блокирует."""
    return (os.environ.get("TELEGRAM_PROXY") or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy") or "").strip()


def _opener():
    return tls.opener(proxy_url())


def button_url_ok(url):
    """Telegram принимает в кнопках только внешние http(s)-адреса — не localhost и не 127.0.0.1."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return (parts.scheme in ("http", "https") and "." in host
            and host not in ("127.0.0.1", "0.0.0.0") and not host.startswith(("192.168.", "10.")))


class TelegramBot:
    def __init__(self, token, db_path, public_url=""):
        self.token = token
        self.db_path = db_path
        self.public_url = public_url.rstrip("/")
        self.username = None
        self.online = False
        self.stopped = False      # неверный токен — бот больше не пытается подключиться
        self.last_error = ""      # последняя ошибка связи (без токена)
        self.started = False

    def status_text(self):
        """Понятное объяснение для сайта, почему бот сейчас не работает."""
        if self.stopped:
            return ("Telegram-бот выключен: Telegram не принял TELEGRAM_BOT_TOKEN. Администратору: "
                    "получите новый токен у @BotFather, вставьте в .env и перезапустите сайт.")
        if not self.started:
            return "Telegram-бот не запущен. Администратору: перезапустите сайт (serve.py)."
        if self.last_error:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in self.last_error:
                hint = (" Сертификат Telegram подменяется по пути — обычно это антивирус на сервере с проверкой "
                        "HTTPS или фильтр хостинга. Администратору: запустите scripts\\check_telegram.py — "
                        "он покажет, кто выдал сертификат, и что сделать.")
            elif "недоступен" in self.last_error or "timed out" in self.last_error.lower():
                hint = (" Похоже, хостинг блокирует Telegram — администратору нужно указать прокси "
                        "(TELEGRAM_PROXY в .env). Проверка: scripts\\check_telegram.py")
            elif "409" in self.last_error:
                hint = " Этот же бот запущен где-то ещё (например, на ПК). Остановите вторую копию."
            return f"Telegram-бот не может подключиться: {self.last_error}.{hint}"
        return "Telegram-бот ещё подключается. Попробуйте через несколько секунд."

    # ------------------------------------------------------------ Bot API

    def _request(self, method, data, content_type, timeout):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
        try:
            with _opener().open(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            try:
                body = json.loads(err.read())
            except Exception:
                body = {"description": str(err.code)}
            # В тексте ошибки нет токена — только описание от Telegram
            raise TelegramError(f"Telegram {err.code}: {body.get('description', '')}") from None
        except urllib.error.URLError as err:
            raise TelegramError(f"Telegram недоступен: {err.reason}") from None
        except (TimeoutError, OSError) as err:
            raise TelegramError(f"Telegram недоступен: {err}") from None
        if not body.get("ok"):
            raise TelegramError(body.get("description", "ошибка Telegram"))
        return body["result"]

    def _call(self, method, params=None, timeout=15):
        return self._request(method, json.dumps(params or {}).encode(), "application/json", timeout)

    def _call_files(self, method, fields, files, timeout=60):
        """Запрос с загрузкой файлов (multipart/form-data). files: {имя: (имя_файла, байты, mime)}."""
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        for key, (filename, content, mime) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                         f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request(method, b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout)

    def _markup(self, buttons):
        """buttons: [[(текст, url или 'cb:данные'), ...], ...] — ряды кнопок."""
        rows = []
        for row in buttons or []:
            out = []
            for text, target in row:
                if target.startswith("cb:"):
                    out.append({"text": text, "callback_data": target[3:]})
                elif button_url_ok(target):
                    out.append({"text": text, "url": target})
            if out:
                rows.append(out)
        return {"inline_keyboard": rows} if rows else None

    def send(self, chat_id, html_text, buttons=None):
        params = {
            "chat_id": chat_id,
            "text": html_text[:4000],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        markup = self._markup(buttons)
        if markup:
            params["reply_markup"] = markup
        try:
            return self._call("sendMessage", params)
        except TelegramError as err:
            # Если Telegram не принял кнопку — всё равно доставляем сам текст
            if markup and "400" in str(err) and "button" in str(err).lower():
                params.pop("reply_markup")
                return self._call("sendMessage", params)
            raise

    def _edit(self, chat_id, message_id, html_text, buttons=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": html_text[:4000],
                  "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        markup = self._markup(buttons)
        if markup:
            params["reply_markup"] = markup
        try:
            self._call("editMessageText", params)
        except TelegramError as err:
            if "not modified" not in str(err):
                raise

    # ------------------------------------------------------------ оформление профиля

    def apply_profile(self, log=print):
        """Имя, описания и меню команд. Меняет только то, что отличается (у Telegram жёсткие лимиты)."""
        try:
            if self._call("getMyName").get("name") != PROFILE["name"]:
                self._call("setMyName", {"name": PROFILE["name"]})
            if self._call("getMyDescription").get("description") != PROFILE["description"]:
                self._call("setMyDescription", {"description": PROFILE["description"]})
            if self._call("getMyShortDescription").get("short_description") != PROFILE["short_description"]:
                self._call("setMyShortDescription", {"short_description": PROFILE["short_description"]})
            want = [{"command": c, "description": d} for c, d in PROFILE["commands"]]
            if self._call("getMyCommands") != want:
                self._call("setMyCommands", {"commands": want})
                self._call("setChatMenuButton", {"menu_button": {"type": "commands"}})
            return True
        except TelegramError as err:
            log(f"[Telegram] Не удалось оформить профиль бота: {err}")
            return False

    def set_avatar(self, png_bytes):
        """Аватар бота (Bot API 9.x: setMyProfilePhoto)."""
        photo = json.dumps({"type": "static", "photo": "attach://avatar"})
        return self._call_files("setMyProfilePhoto", {"photo": photo},
                                {"avatar": ("avatar.png", png_bytes, "image/png")})

    # ------------------------------------------------------------ приём команд

    def start_polling(self, log=print):
        self.started = True
        thread = threading.Thread(target=self._poll_forever, args=(log,), daemon=True,
                                  name="telegram-poller")
        thread.start()
        return thread

    def _poll_forever(self, log):
        offset = None
        delay = 5
        while True:
            try:
                if self.username is None:
                    me = self._call("getMe")
                    self.username = me.get("username")
                    log(f"[Telegram] Бот @{self.username} в сети")
                    self.apply_profile(log)
                params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", params, timeout=35)
                self.online = True
                self.last_error = ""
                delay = 5
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        self.handle_update(update)
                    except Exception as err:
                        log(f"[Telegram] Ошибка обработки сообщения: {err}")
            except TelegramError as err:
                self.online = False
                text = str(err)
                self.last_error = text
                if "401" in text or "Unauthorized" in text or "404" in text:
                    self.stopped = True
                    log("[Telegram] Неверный TELEGRAM_BOT_TOKEN — бот остановлен")
                    return
                if "409" in text:
                    log("[Telegram] Этот бот уже запущен в другом месте (409). Повтор через 30 с")
                    time.sleep(30)
                    continue
                log(f"[Telegram] {text}. Повтор через {delay} с")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            except Exception as err:
                self.online = False
                self.last_error = err.__class__.__name__
                log(f"[Telegram] {err.__class__.__name__}. Повтор через {delay} с")
                time.sleep(delay)
                delay = min(delay * 2, 120)

    def handle_update(self, update):
        """Обрабатывает одно обновление. Возвращает текст ответа (для тестов)."""
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not text:
            return None
        chat_id = str(chat["id"])
        reply = self._reply_for(chat_id, text)
        if not reply:
            return None
        body, buttons = reply
        try:
            self.send(chat_id, body, buttons)
        except TelegramError:
            pass
        return body

    # ------------------------------------------------------------ данные

    def _db(self):
        return connect(self.db_path)

    def _user_by_chat(self, chat_id):
        conn = self._db()
        try:
            return conn.execute("SELECT * FROM users WHERE telegram_chat_id = ? AND is_active = 1",
                                (chat_id,)).fetchone()
        finally:
            conn.close()

    def _site_buttons(self, *extra_rows):
        rows = list(extra_rows)
        if self.public_url:
            rows.append([("🌐 Открыть доску", self.public_url + "/"),
                         ("👤 Профиль", self.public_url + "/profile")])
        return rows

    # ------------------------------------------------------------ ответы

    def _reply_for(self, chat_id, text):
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()

        if command == "/start" and arg.strip():
            return self._link(chat_id, arg.strip())
        if command == "/stop":
            return self._stop(chat_id)
        if command == "/settings":
            user = self._user_by_chat(chat_id)
            return self._settings_view(user) if user else self._not_linked()
        if command == "/status":
            return self._status(chat_id)
        if command == "/help":
            return self._help(), self._site_buttons()
        user = self._user_by_chat(chat_id)
        if user:
            return self._status(chat_id)
        return self._welcome()

    def _welcome(self):
        return (
            f"👋 <b>Привет! Это бот уведомлений {BRAND}</b>\n{LINE}\n\n"
            "Я пришлю сообщение, когда:\n"
            "  👤 вам назначат задачу\n"
            "  📣 вас упомянут через @\n"
            "  💬 прокомментируют вашу карточку\n"
            "  🔎 задачу отправят на проверку\n\n"
            "<b>Как подключиться</b>\n"
            "1. Откройте свой профиль на сайте\n"
            "2. Нажмите «Подключить Telegram»\n"
            "3. Вернитесь сюда и нажмите «Start»"
        ), self._site_buttons()

    def _not_linked(self):
        body, buttons = self._welcome()
        return "⚠️ <b>Этот чат ещё не подключён к аккаунту.</b>\n\n" + body.split("\n\n", 2)[-1], buttons

    def _help(self):
        return (
            f"❓ <b>Справка · {BRAND}</b>\n{LINE}\n\n"
            "/settings — какие уведомления присылать\n"
            "/status — подключённый аккаунт\n"
            "/stop — отключить уведомления\n\n"
            "Подключить аккаунт можно в профиле на сайте: кнопка «Подключить Telegram». "
            "Один Telegram подключается к одному аккаунту."
        )

    def _link(self, chat_id, code):
        conn = self._db()
        try:
            with conn:
                user = conn.execute(
                    "SELECT id, display_name FROM users WHERE telegram_link_code = ? "
                    "AND telegram_link_expires > ? AND is_active = 1",
                    (code, utcnow()),
                ).fetchone()
                if user is None:
                    return ("⏳ <b>Код не найден или устарел.</b>\n\n"
                            "Откройте профиль на сайте и нажмите «Подключить Telegram» ещё раз — "
                            "ссылка действует несколько минут."), self._site_buttons()
                # Один Telegram — один аккаунт на сайте
                conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (chat_id,))
                conn.execute(
                    "UPDATE users SET telegram_chat_id = ?, telegram_link_code = NULL, "
                    "telegram_link_expires = NULL, notify_telegram = 1 WHERE id = ?",
                    (chat_id, user["id"]),
                )
                conn.execute(
                    "INSERT INTO audit_log (user_id, action, details, created_at) VALUES (?, 'telegram.linked', ?, ?)",
                    (user["id"], f"чат {chat_id}", utcnow()),
                )
        finally:
            conn.close()
        return (
            f"✅ <b>Готово, {html.escape(user['display_name'])}!</b>\n{LINE}\n\n"
            f"Уведомления {BRAND} подключены — теперь они будут приходить сюда.\n\n"
            "⚙️ Выбрать события: /settings\n"
            "🔕 Отключить: /stop"
        ), self._site_buttons([("⚙️ Настроить уведомления", "cb:settings")])

    def _stop(self, chat_id):
        conn = self._db()
        try:
            with conn:
                cur = conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (chat_id,))
        finally:
            conn.close()
        if cur.rowcount:
            return ("🔕 <b>Уведомления отключены.</b>\n\n"
                    "Чат отвязан от аккаунта. Подключить снова можно в профиле на сайте."), self._site_buttons()
        return "ℹ️ Этот чат не был подключён к аккаунту.", self._site_buttons()

    def _status(self, chat_id):
        user = self._user_by_chat(chat_id)
        if user is None:
            return self._not_linked()
        events = [e for e in (user["notify_events"] or "").split(",") if e in EVENT_LABELS]
        state = "🟢 включены" if user["notify_telegram"] else "⏸ на паузе"
        lines = "\n".join(f"  {EVENT_LABELS[e]}" for e in events) or "  — ничего не выбрано"
        return (
            f"👤 <b>{html.escape(user['display_name'])}</b>  ·  @{html.escape(user['username'])}\n"
            f"Роль: {html.escape(ROLES.get(user['role'], user['role']))}\n{LINE}\n\n"
            f"Уведомления в Telegram: <b>{state}</b>\n\n"
            f"<b>Присылаю:</b>\n{lines}"
        ), self._site_buttons([("⚙️ Настроить", "cb:settings")])

    def _settings_view(self, user):
        events = set((user["notify_events"] or "").split(","))
        paused = not user["notify_telegram"]
        body = (
            f"⚙️ <b>Настройки уведомлений</b>\n{LINE}\n\n"
            "Нажмите на событие, чтобы включить ✅ или выключить ▫️ его.\n"
            "Эти же настройки — в профиле на сайте."
            + ("\n\n⏸ <b>Сейчас уведомления на паузе.</b>" if paused else "")
        )
        rows = [[(("✅ " if key in events else "▫️ ") + label.split(" ", 1)[1], f"cb:ev:{key}")]
                for key, label in EVENT_LABELS.items()]
        rows.append([("▶️ Возобновить" if paused else "⏸ Пауза", "cb:pause"), ("✔️ Готово", "cb:done")])
        return body, rows

    def _handle_callback(self, query):
        data = query.get("data") or ""
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        notice = ""
        user = self._user_by_chat(chat_id) if chat_id else None
        if user is None:
            body, buttons = self._not_linked()
            notice = "Чат не подключён"
        else:
            if data.startswith("ev:") and data[3:] in EVENT_LABELS:
                events = [e for e in (user["notify_events"] or "").split(",") if e in EVENT_LABELS]
                key = data[3:]
                events = [e for e in events if e != key] if key in events else events + [key]
                ordered = ",".join(e for e in EVENT_LABELS if e in events)
                self._update_user(user["id"], "notify_events", ordered)
                notice = ("Включено: " if key in events else "Выключено: ") + EVENT_LABELS[key].split(" ", 1)[1]
            elif data == "pause":
                self._update_user(user["id"], "notify_telegram", 0 if user["notify_telegram"] else 1)
                notice = "Пауза" if user["notify_telegram"] else "Уведомления снова включены"
            if data == "done":
                body, buttons = self._status(chat_id)
                notice = "Сохранено"
            else:
                body, buttons = self._settings_view(self._user_by_chat(chat_id))
        try:
            self._call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": notice})
            if message.get("message_id"):
                self._edit(chat_id, message["message_id"], body, buttons)
        except TelegramError:
            pass
        return body

    def _update_user(self, user_id, column, value):
        assert column in ("notify_events", "notify_telegram")
        conn = self._db()
        try:
            with conn:
                conn.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (value, user_id))
                conn.execute(
                    "INSERT INTO audit_log (user_id, action, details, created_at) VALUES (?, 'telegram.settings', ?, ?)",
                    (user_id, f"{column} = {value}", utcnow()),
                )
        finally:
            conn.close()


# ------------------------------------------------------------ сообщения-уведомления

EVENT_HEADERS = {
    "assigned": "👤 Вам назначили задачу",
    "mention": "📣 Вас упомянули",
    "comment": "💬 Новый комментарий",
    "status": "🔀 Задача сменила статус",
    "review": "🔎 Задача на проверке",
    "new_card": "🆕 Новая карточка",
}


def format_notification(event, text, card_id, card_title, link):
    """Красивое уведомление для Telegram: заголовок, суть, карточка, кнопка «Открыть»."""
    header = EVENT_HEADERS.get(event, "🔔 Уведомление")
    who, sep, rest = text.partition(": ")
    if event == "comment" and sep:
        body = f"<b>{html.escape(who)}</b> пишет:\n<blockquote>{html.escape(rest)}</blockquote>"
    elif sep and len(rest) > 0:
        # «… вернул(а) задачу в «…»: причина» — причину выделяем цитатой
        body = f"{html.escape(who)}\n<blockquote>{html.escape(rest)}</blockquote>"
    else:
        body = html.escape(text)
    card = f"📌 <b>#{card_id}</b> · {html.escape(card_title)}"
    message = f"<b>{header}</b>\n{LINE}\n\n{body}\n\n{card}"
    if button_url_ok(link):
        return message, [[(f"Открыть карточку #{card_id}  ↗", link)]]
    return message + f'\n\n<a href="{html.escape(link)}">Открыть карточку</a>', None
