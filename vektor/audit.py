"""Журнал действий: кто, что, когда и с какого IP сделал на сайте. Виден только администратору.

Как пишется:
- каждое успешное изменение через API записывается автоматически (after_request);
- подробности (что именно изменилось) view-функции кладут в g.audit_details;
- вход, выход и неудачные попытки входа записываются явно в auth.py.
"""

from flask import g, request

from .db import get_db, utcnow

# Действие -> (группа, подпись). Группы используются в фильтре журнала.
ACTIONS = {
    # вход
    "auth.login": ("auth", "Вошёл на сайт"),
    "auth.login_failed": ("auth", "Неудачная попытка входа"),
    "auth.logout": ("auth", "Вышел"),
    "auth.discord_register": ("auth", "Зарегистрировался через Discord"),
    "auth.discord_link": ("auth", "Привязал Discord"),
    "telegram.linked": ("profile", "Подключил Telegram"),
    # карточки
    "api.card_create": ("cards", "Создал карточку"),
    "api.card_update": ("cards", "Изменил карточку"),
    "api.card_move": ("cards", "Перенёс карточку"),
    "api.card_assign": ("cards", "Сменил исполнителя"),
    "api.comment_create": ("cards", "Написал комментарий"),
    "api.attachment_upload": ("cards", "Загрузил скриншот"),
    "api.attachment_delete": ("cards", "Удалил скриншот"),
    "api.card_archive": ("cards", "Отправил карточку в архив"),
    "api.card_restore": ("cards", "Восстановил карточку из архива"),
    "api.card_delete": ("cards", "Удалил карточку навсегда"),
    # профиль
    "api.profile_update": ("profile", "Изменил профиль"),
    "api.avatar_upload": ("profile", "Сменил аватарку"),
    "api.avatar_delete": ("profile", "Убрал аватарку"),
    "api.telegram_link": ("profile", "Запросил подключение Telegram"),
    "api.telegram_unlink": ("profile", "Отключил Telegram"),
    "api.discord_unlink": ("profile", "Отвязал Discord"),
    "api.profile_password": ("profile", "Сменил свой пароль"),
    # администрирование
    "admin.user_create": ("admin", "Создал пользователя"),
    "admin.user_update": ("admin", "Изменил пользователя"),
    "admin.user_password": ("admin", "Задал пароль пользователю"),
    "admin.column_create": ("admin", "Создал колонку"),
    "admin.column_update": ("admin", "Изменил колонку"),
    "admin.column_move": ("admin", "Переставил колонку"),
    "admin.column_delete": ("admin", "Удалил колонку"),
    "admin.option_create": ("admin", "Добавил значение списка"),
    "admin.option_update": ("admin", "Изменил значение списка"),
    "admin.option_move": ("admin", "Переставил значение списка"),
    "admin.option_delete": ("admin", "Удалил значение списка"),
    "admin.tag_rename": ("admin", "Переименовал тег"),
    "admin.tag_delete": ("admin", "Удалил тег"),
    "admin.settings_update": ("admin", "Изменил настройки доски"),
}

GROUPS = {
    "auth": "Входы",
    "cards": "Карточки и комментарии",
    "profile": "Профили",
    "admin": "Администрирование",
}


def client_ip():
    return (request.remote_addr or "")[:64]


def write(db, action, details="", card_id=None, user_id=None):
    """Добавляет запись в журнал. Коммит делает вызывающий код."""
    if user_id is None and getattr(g, "user", None) is not None:
        user_id = g.user["id"]
    db.execute(
        "INSERT INTO audit_log (user_id, action, details, card_id, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, str(details or "")[:500], card_id, client_ip(), utcnow()),
    )


def details(text):
    """View-функции вызывают это, чтобы уточнить, что именно сделано."""
    g.audit_details = (getattr(g, "audit_details", "") + ("; " if getattr(g, "audit_details", "") else "") + text)[:500]


def after_request(resp):
    """Автоматически записывает каждое успешное изменение через API."""
    if request.method in ("GET", "HEAD", "OPTIONS") or resp.status_code >= 400:
        return resp
    action = request.endpoint or ""
    if action not in ACTIONS or getattr(g, "user", None) is None:
        return resp
    card_id = (request.view_args or {}).get("card_id") or getattr(g, "audit_card_id", None)
    try:
        db = get_db()
        write(db, action, getattr(g, "audit_details", ""), card_id)
        db.commit()
    except Exception as err:  # журнал не должен ломать само действие
        print(f"[Журнал] Не удалось записать действие {action}: {err}")
    return resp
