"""Telegram-бот для личных уведомлений.

Как пользователь подключает уведомления:
1. В профиле на сайте нажимает «Подключить Telegram» — сайт создаёт одноразовый код.
2. Переходит по ссылке t.me/<бот>?start=<код> и нажимает «Start».
3. Бот получает /start <код>, находит пользователя и запоминает его chat_id.

Бот работает через long polling (getUpdates) в фоновом потоке — не нужен
публичный адрес и HTTPS-вебхук, поэтому работает и на локальном ПК.
"""

import html
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .db import connect, utcnow


class TelegramError(Exception):
    pass


class TelegramBot:
    def __init__(self, token, db_path, public_url=""):
        self.token = token
        self.db_path = db_path
        self.public_url = public_url.rstrip("/")
        self.username = None
        self.online = False

    def _call(self, method, params=None, timeout=15):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        if not body.get("ok"):
            raise TelegramError(body.get("description", "ошибка Telegram"))
        return body["result"]

    def send(self, chat_id, html_text):
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": html_text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    # ------------------------------------------------------------ приём команд

    def start_polling(self, log=print):
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
                params = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", params, timeout=35)
                self.online = True
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
                if "401" in text or "Unauthorized" in text:
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
                log(f"[Telegram] {err.__class__.__name__}. Повтор через {delay} с")
                time.sleep(delay)
                delay = min(delay * 2, 120)

    def handle_update(self, update):
        """Обрабатывает одно сообщение. Возвращает текст ответа (для тестов)."""
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not text:
            return None
        chat_id = str(chat["id"])
        reply = self._reply_for(chat_id, text)
        if reply:
            try:
                self.send(chat_id, reply)
            except TelegramError:
                pass
        return reply

    def _reply_for(self, chat_id, text):
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        site = f"\n\nСайт: {html.escape(self.public_url)}" if self.public_url else ""

        if command == "/start" and arg.strip():
            code = arg.strip()
            conn = connect(self.db_path)
            try:
                with conn:
                    user = conn.execute(
                        "SELECT id, display_name FROM users WHERE telegram_link_code = ? "
                        "AND telegram_link_expires > ? AND is_active = 1",
                        (code, utcnow()),
                    ).fetchone()
                    if user is None:
                        return ("Код не найден или устарел. Откройте профиль на сайте и нажмите "
                                "«Подключить Telegram» ещё раз." + site)
                    # Один Telegram — один аккаунт на сайте
                    conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ?",
                                 (chat_id,))
                    conn.execute(
                        "UPDATE users SET telegram_chat_id = ?, telegram_link_code = NULL, "
                        "telegram_link_expires = NULL, notify_telegram = 1 WHERE id = ?",
                        (chat_id, user["id"]),
                    )
            finally:
                conn.close()
            return (f"Готово, <b>{html.escape(user['display_name'])}</b>! Уведомления Vektor RP "
                    "подключены. Какие события присылать — настраивается в профиле на сайте.\n\n"
                    "Отключить: /stop")

        if command == "/stop":
            conn = connect(self.db_path)
            try:
                with conn:
                    cur = conn.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ?",
                                       (chat_id,))
            finally:
                conn.close()
            if cur.rowcount:
                return "Уведомления отключены. Подключить снова можно в профиле на сайте."
            return "Этот чат не был подключён к аккаунту." + site

        return ("Это бот уведомлений канбан-доски <b>Vektor RP</b>.\n\n"
                "Чтобы подключить уведомления, откройте свой профиль на сайте и нажмите "
                "«Подключить Telegram».\n\n/stop — отключить уведомления" + site)
