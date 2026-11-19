"""Уведомления: на сайте (колокольчик), в личку Discord-бота, в Telegram и в канал Discord.

Сохранение уведомления в базу происходит сразу, в той же транзакции, что и действие.
Отправка во внешние сервисы идёт в фоновом потоке через очередь, чтобы медленный
Discord/Telegram не тормозил сайт. Ошибки отправки пишутся в консоль и не ломают действие.
"""

import html
import queue
import threading

from flask import current_app

from .db import utcnow
from .telegram_bot import format_notification

# События, на которые можно подписаться в профиле
EVENTS = {
    "assigned": "Мне назначили задачу",
    "mention": "Меня упомянули через @",
    "comment": "Новый комментарий в моих задачах",
    "status": "Мою задачу переместили",
    "review": "Задача отправлена на проверку",
    "new_card": "Создана новая карточка",
}
DEFAULT_EVENTS = {
    "tester": "assigned,mention,comment,status,review",
    "developer": "assigned,mention,comment,status",
    "admin": "assigned,mention,comment,status",
    "pending": "assigned,mention,comment,status",
}

# События для общего канала Discord (включаются в «Настройках»)
CHANNEL_EVENTS = {
    "created": "Создана карточка",
    "review": "Отправлена на проверку",
    "returned": "Возвращена в работу",
    "closed": "Закрыта",
}


class Dispatcher:
    """Фоновая очередь отправки во внешние сервисы."""

    def __init__(self, app):
        self.app = app
        self.discord = None
        self.telegram = None
        self.sent = []  # в тестах сюда складываются «отправленные» сообщения
        self.queue = queue.Queue(maxsize=1000)
        self._worker = None

    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True, name="notify-worker")
            self._worker.start()

    def put(self, channel, target, text):
        if self.app.config.get("NOTIFY_CAPTURE"):
            self.sent.append((channel, target, text))
            return
        if channel == "discord_dm" and not self.discord:
            return
        if channel == "discord_channel" and not (self.discord and self.discord.channel_id):
            return
        if channel == "telegram" and not self.telegram:
            return
        try:
            self.queue.put_nowait((channel, target, text))
        except queue.Full:
            print("[Уведомления] Очередь переполнена, сообщение пропущено")

    def _run(self):
        while True:
            channel, target, text = self.queue.get()
            try:
                if channel == "discord_dm":
                    self.discord.send_dm(target, text)
                elif channel == "discord_channel":
                    self.discord.send_channel(text)
                elif channel == "telegram":
                    if isinstance(text, tuple):
                        self.telegram.send(target, text[0], text[1])
                    else:
                        self.telegram.send(target, text)
            except Exception as err:
                print(f"[Уведомления] Не удалось отправить ({channel}): {err}")


def _dispatcher():
    return current_app.extensions["vektor_notify"]


def card_link(card_id):
    base = current_app.config.get("PUBLIC_URL", "").rstrip("/")
    return f"{base}/card/{card_id}" if base else f"/card/{card_id}"


def notify_users(db, event, card_id, card_title, actor, user_rows, text):
    """Создаёт уведомления для пользователей (кроме автора действия) с учётом их настроек."""
    seen = set()
    link = card_link(card_id)
    for user in user_rows:
        if user is None or user["id"] in seen or (actor and user["id"] == actor["id"]):
            continue
        seen.add(user["id"])
        if not user["is_active"] or user["role"] == "pending":
            continue
        if event not in (user["notify_events"] or "").split(","):
            continue
        db.execute(
            "INSERT INTO notifications (user_id, card_id, event, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], card_id, event, text, utcnow()),
        )
        plain = f"{text}\n#{card_id} «{card_title}»\n{link}"
        if user["notify_discord"] and user["discord_id"]:
            _dispatcher().put("discord_dm", user["discord_id"], plain)
        if user["notify_telegram"] and user["telegram_chat_id"]:
            _dispatcher().put("telegram", user["telegram_chat_id"],
                              format_notification(event, text, card_id, card_title, link))
    return seen


def notify_channel(db, event, card_id, card_title, text):
    enabled = db.execute("SELECT value FROM settings WHERE key = 'channel_events'").fetchone()
    if not enabled or event not in enabled["value"].split(","):
        return
    _dispatcher().put("discord_channel", None, f"**{text}**\n#{card_id} «{card_title}»\n<{card_link(card_id)}>")


def users_by_ids(db, ids):
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return db.execute(f"SELECT * FROM users WHERE id IN ({marks})", tuple(ids)).fetchall()


def subscribers(db, event, board_id):
    """Подписанные на событие участники доски (и администраторы сайта)."""
    return db.execute(
        "SELECT * FROM users u WHERE u.is_active = 1 AND u.role != 'pending' "
        "AND (',' || u.notify_events || ',') LIKE ? "
        "AND (u.role = 'admin' OR EXISTS (SELECT 1 FROM board_members m WHERE m.board_id = ? AND m.user_id = u.id))",
        (f"%,{event},%", board_id),
    ).fetchall()


def send_test(user):
    """Тестовое сообщение пользователю во все подключённые каналы."""
    text = "Тестовое уведомление Vektor RP: всё работает ✅"
    channels = []
    if user["discord_id"] and _dispatcher().discord:
        _dispatcher().put("discord_dm", user["discord_id"], text)
        channels.append("Discord")
    if user["telegram_chat_id"] and _dispatcher().telegram:
        _dispatcher().put("telegram", user["telegram_chat_id"], (
            "✅ <b>Тестовое уведомление</b>\n━━━━━━━━━━━━━━\n\n"
            f"Всё работает, {html.escape(user['display_name'])}! Уведомления Vektor RP будут приходить сюда.", None))
        channels.append("Telegram")
    return channels
