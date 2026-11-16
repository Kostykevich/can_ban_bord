"""JSON API доски. Каждое изменение проверяет права на сервере.

Коды ответов: 400 — ошибка в данных, 401 — не вошёл, 403 — нет прав,
404 — не найдено, 409 — конфликт (карточку уже изменил кто-то другой).
"""

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, g, jsonify, request

from . import audit as A
from . import models as M
from . import notify as N
from . import permissions as P
from .auth import USERNAME_RE
from .constants import (FREQUENCIES, LIMITS, MAX_ATTACHMENTS_PER_CARD,
                        MAX_IMAGE_BYTES)
from .db import get_db, utcnow

bp = Blueprint("api", __name__, url_prefix="/api")

FIELD_LABELS = {
    "title": "Название",
    "description": "Описание",
    "steps": "Шаги воспроизведения",
    "expected": "Ожидаемый результат",
    "actual": "Фактический результат",
    "build_version": "Версия сборки",
    "coords": "Координаты",
    "static_id": "Static ID",
    "occurred_at": "Время происшествия",
    "frequency": "Частота",
    "video_url": "Видео",
    "server_id": "Сервер",
    "priority_id": "Приоритет",
    "category_id": "Категория",
    "tags": "Теги",
}
TEXT_FIELDS = ["title", "description", "steps", "expected", "actual", "build_version",
               "coords", "static_id", "occurred_at", "video_url"]
LONG_FIELDS = {"description", "steps", "expected", "actual"}
OPTION_FIELDS = {"priority_id": "priority", "category_id": "category", "server_id": "server"}


# ---------------------------------------------------------------- помощники

def fail(message, status=400, fields=None):
    payload = {"error": message}
    if fields:
        payload["fields"] = fields
    return jsonify(payload), status


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def require_worker():
    if not P.is_worker(g.user):
        return fail("Недостаточно прав.", 403)
    return None


def load_card(card_id):
    return get_db().execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def touch(db, card_id):
    db.execute("UPDATE cards SET updated_at = ?, version = version + 1 WHERE id = ?", (utcnow(), card_id))


def clean_text(value, limit):
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\x00", "").strip()
    return text[:limit + 1]


def validate_card(db, data, current=None):
    """Проверяет поля карточки. current — текущая строка при редактировании."""
    values, errors = {}, {}
    for field in TEXT_FIELDS:
        if field not in data and current is not None:
            continue
        text = clean_text(data.get(field), LIMITS[field])
        if len(text) > LIMITS[field]:
            errors[field] = f"Не длиннее {LIMITS[field]} символов."
        values[field] = text
    for field in ("title", "description"):
        if field in values and not values[field]:
            errors[field] = "Обязательное поле."
    if values.get("video_url") and not re.match(r"^https?://\S+$", values["video_url"]):
        errors["video_url"] = "Ссылка должна начинаться с http:// или https://"

    if "frequency" in data or current is None:
        freq = data.get("frequency") or "unknown"
        if freq not in FREQUENCIES:
            errors["frequency"] = "Неизвестное значение."
        values["frequency"] = freq

    for field, kind in OPTION_FIELDS.items():
        if field not in data:
            continue
        raw = data.get(field)
        if current is not None and str(raw) == str(current[field]):
            values[field] = current[field]  # оставить прежнее, даже если его отключили
            continue
        opt = M.get_option(db, raw, kind)
        if opt is None:
            errors[field] = "Выберите значение из списка."
        else:
            values[field] = opt["id"]

    if "tags" in data:
        tags = data.get("tags")
        if not isinstance(tags, list):
            errors["tags"] = "Неверный формат тегов."
        else:
            bad = [t for t in tags if M.normalize_tag(t) is None]
            if bad:
                errors["tags"] = "Тег: до 30 символов, буквы, цифры, пробел и - + . #"
            values["tags"] = [M.normalize_tag(t) for t in tags if M.normalize_tag(t)]
    return values, errors


def default_option(db, kind, name):
    row = db.execute(
        "SELECT id FROM options WHERE kind = ? AND is_active = 1 ORDER BY (name != ?), position LIMIT 1",
        (kind, name),
    ).fetchone()
    return row["id"] if row else None


def user_can_be_assigned(db, user_id):
    row = db.execute(
        "SELECT * FROM users WHERE id = ? AND is_active = 1 AND role IN ('developer', 'admin', 'tester')",
        (user_id,),
    ).fetchone()
    return row


def card_permissions(db, card_row):
    user = g.user
    columns = M.list_columns(db)
    current = next((c for c in columns if c["id"] == card_row["column_id"]), None)
    moves = []
    for col in columns:
        if col["id"] == card_row["column_id"] or not P.can_enter_column(user, col):
            continue
        rules = P.transition_rules(current, col) if current else {"needs_comment": False,
                                                                     "needs_resolution": col["kind"] == "done",
                                                                     "is_return": False}
        moves.append({"column_id": col["id"], **rules})
    return {
        "editable": sorted(P.editable_fields(user, card_row["author_id"])),
        "moves": moves,
        "can_take": P.can_assign(user, card_row["assignee_id"], user["id"]) and card_row["assignee_id"] != user["id"],
        "can_release": card_row["assignee_id"] == user["id"] and P.can_assign(user, user["id"], None),
        "can_assign_any": P.is_admin(user),
        "can_comment": P.is_worker(user),
        "can_upload": P.is_worker(user),
        "can_archive": P.is_admin(user),
    }


def full_card_response(db, card_id, status=200):
    card = M.card_full(db, card_id)
    card["permissions"] = card_permissions(db, load_card(card_id))
    for att in card["attachments"]:
        att["can_delete"] = P.can_delete_attachment(g.user, att["uploader_id"])
    return jsonify(card), status


def mention_notify(db, card_id, title, texts, old_texts=()):
    """Уведомляет только тех, кого упомянули впервые (не повторно при правке)."""
    new_users = M.find_mentions(db, *texts)
    old_ids = {u["id"] for u in M.find_mentions(db, *old_texts)} if old_texts else set()
    targets = [u for u in new_users if u["id"] not in old_ids]
    return N.notify_users(db, "mention", card_id, title, g.user, targets,
                          f"{g.user['display_name']} упомянул(а) вас")


# ---------------------------------------------------------------- доска

@bp.get("/board")
def board():
    db = get_db()
    columns = M.list_columns(db)
    for col in columns:
        col["can_enter"] = P.can_enter_column(g.user, col)
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (g.user["id"],)
    ).fetchone()[0]
    return jsonify(
        cards=M.board_cards(db),
        columns=columns,
        options=M.options_grouped(db),
        tags=M.list_tags(db),
        users=M.list_active_users(db),
        settings=M.get_settings(db),
        stamp=M.board_stamp(db),
        unread=unread,
    )


@bp.get("/board/stamp")
def board_stamp():
    db = get_db()
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (g.user["id"],)
    ).fetchone()[0]
    return jsonify(stamp=M.board_stamp(db), unread=unread)


@bp.get("/archive")
def archive():
    return jsonify(cards=M.archive_cards(get_db()))


@bp.get("/similar")
def similar():
    q = (request.args.get("q") or "").strip()[:150]
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) >= 3][:6]
    exclude = request.args.get("exclude", type=int)
    if not words:
        return jsonify(cards=[])
    db = get_db()
    clause = " OR ".join("lower(c.title) LIKE ?" for _ in words)
    params = [f"%{w}%" for w in words]
    rows = db.execute(
        "SELECT c.id, c.title, c.column_id, col.name AS column_name, col.kind FROM cards c "
        "JOIN board_columns col ON col.id = c.column_id "
        f"WHERE c.archived_at IS NULL AND ({clause}) AND c.id != ? "
        "ORDER BY (col.kind = 'done'), c.id DESC LIMIT 30",
        (*params, exclude or 0),
    ).fetchall()
    # Сортируем по числу совпавших слов
    scored = sorted(rows, key=lambda r: -sum(w in r["title"].lower() for w in words))[:5]
    return jsonify(cards=[{"id": r["id"], "title": r["title"], "column": r["column_name"],
                           "closed": r["kind"] == "done"} for r in scored])


# ---------------------------------------------------------------- карточки

@bp.get("/cards/<int:card_id>")
def card_detail(card_id):
    db = get_db()
    if load_card(card_id) is None:
        return fail("Карточка не найдена.", 404)
    return full_card_response(db, card_id)


@bp.post("/cards")
def card_create():
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    data = body()
    values, errors = validate_card(db, data)
    assignee_id = data.get("assignee_id") or None
    if assignee_id is not None:
        try:
            assignee_id = int(assignee_id)
        except (TypeError, ValueError):
            errors["assignee_id"] = "Неверный исполнитель."
        else:
            if not P.can_assign(g.user, None, assignee_id):
                errors["assignee_id"] = "Вы не можете назначить этого исполнителя."
            elif user_can_be_assigned(db, assignee_id) is None:
                errors["assignee_id"] = "Пользователь не найден."
    if errors:
        return fail("Проверьте поля формы.", 400, errors)

    column = M.first_column(db)
    if column is None:
        return fail("На доске нет ни одной колонки. Попросите администратора создать её.", 409)
    now = utcnow()
    cur = db.execute(
        "INSERT INTO cards (title, description, steps, expected, actual, column_id, priority_id, "
        "category_id, server_id, build_version, coords, static_id, occurred_at, frequency, video_url, "
        "author_id, assignee_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            values["title"], values["description"], values.get("steps", ""), values.get("expected", ""),
            values.get("actual", ""), column["id"],
            values.get("priority_id") or default_option(db, "priority", "Средний"),
            values.get("category_id") or default_option(db, "category", "Прочее"),
            values.get("server_id") or default_option(db, "server", "Тестовый"),
            values.get("build_version", ""), values.get("coords", ""), values.get("static_id", ""),
            values.get("occurred_at", ""), values["frequency"], values.get("video_url", ""),
            g.user["id"], assignee_id, now, now,
        ),
    )
    card_id = cur.lastrowid
    M.log(db, card_id, g.user["id"], "created", new=column["name"])
    if values.get("tags"):
        M.set_card_tags(db, card_id, values["tags"], g.user["id"])
    if assignee_id:
        M.log(db, card_id, g.user["id"], "assigned", "assignee", None, M.display_name(db, assignee_id))

    title = values["title"]
    who = g.user["display_name"]
    notified = mention_notify(db, card_id, title, [values["description"], values.get("steps", "")])
    if assignee_id:
        N.notify_users(db, "assigned", card_id, title, g.user, N.users_by_ids(db, [assignee_id]),
                       f"{who} назначил(а) вам задачу")
    N.notify_users(db, "new_card", card_id, title, g.user,
                   [u for u in N.subscribers(db, "new_card") if u["id"] not in notified],
                   f"{who} создал(а) карточку")
    N.notify_channel(db, "created", card_id, title, f"🆕 Новая карточка от {who}")
    db.commit()
    A.details(f"#{card_id} «{title}»")
    g.audit_card_id = card_id
    return full_card_response(db, card_id, 201)


@bp.patch("/cards/<int:card_id>")
def card_update(card_id):
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    data = body()
    if str(data.get("version")) != str(card["version"]):
        return fail("Карточку только что изменил кто-то другой. Ваш текст остался в форме — "
                    "скопируйте его, откройте карточку заново и внесите правки.", 409)
    values, errors = validate_card(db, data, current=card)
    if errors:
        return fail("Проверьте поля формы.", 400, errors)

    current_tags = [t["name"] for t in M.tags_for(db, [card_id])[card_id]]
    changed = {}
    for field, value in values.items():
        old = current_tags if field == "tags" else card[field]
        if field == "tags":
            if sorted(x.lower() for x in value) != sorted(x.lower() for x in old):
                changed[field] = value
        elif value != old:
            changed[field] = value
    if not changed:
        return full_card_response(db, card_id)

    allowed = P.editable_fields(g.user, card["author_id"])
    forbidden = [FIELD_LABELS.get(f, f) for f in changed if f not in allowed]
    if forbidden:
        return fail("Нет прав менять: " + ", ".join(forbidden) + ".", 403)
    A.details(f"«{card['title']}»: " + ", ".join(FIELD_LABELS.get(f, f).lower() for f in changed))

    for field, value in changed.items():
        if field == "tags":
            old, new = M.set_card_tags(db, card_id, value, g.user["id"])
            M.log(db, card_id, g.user["id"], "edited", "tags", ", ".join(old) or "—", ", ".join(new) or "—")
            continue
        db.execute(f"UPDATE cards SET {field} = ? WHERE id = ?", (value, card_id))  # имя поля из белого списка
        if field in OPTION_FIELDS:
            M.log(db, card_id, g.user["id"], "edited", field, M.option_name(db, card[field]), M.option_name(db, value))
        elif field == "frequency":
            M.log(db, card_id, g.user["id"], "edited", field, FREQUENCIES.get(card[field]), FREQUENCIES.get(value))
        elif field in LONG_FIELDS:
            M.log(db, card_id, g.user["id"], "edited", field)
        else:
            M.log(db, card_id, g.user["id"], "edited", field, card[field] or "—", value or "—")
    touch(db, card_id)

    title = changed.get("title", card["title"])
    if "description" in changed or "steps" in changed:
        mention_notify(db, card_id, title,
                       [changed.get("description", card["description"]), changed.get("steps", card["steps"])],
                       [card["description"], card["steps"]])
    db.commit()
    return full_card_response(db, card_id)


@bp.post("/cards/<int:card_id>/move")
def card_move(card_id):
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    if card["archived_at"]:
        return fail("Карточка в архиве. Сначала восстановите её.", 409)
    data = body()
    target = M.get_column(db, data.get("column_id"))
    if target is None:
        return fail("Колонка не найдена.", 404)
    if data.get("from_column_id") is not None and str(data["from_column_id"]) != str(card["column_id"]):
        return fail("Карточку уже переместил кто-то другой. Доска обновлена.", 409)
    if target["id"] == card["column_id"]:
        return full_card_response(db, card_id)
    if not P.can_enter_column(g.user, target):
        return fail(f"Ваша роль не может переносить карточки в «{target['name']}».", 403)

    source = M.get_column(db, card["column_id"])
    rules = P.transition_rules(source, target)
    comment = clean_text(data.get("comment"), LIMITS["comment"])
    if len(comment) > LIMITS["comment"]:
        return fail(f"Комментарий не длиннее {LIMITS['comment']} символов.", 400, {"comment": "Слишком длинно."})
    if rules["needs_comment"] and not comment:
        return fail("Напишите, почему задача возвращается.", 400, {"comment": "Обязательное поле."})
    resolution = None
    if rules["needs_resolution"]:
        resolution = M.get_option(db, data.get("resolution_id"), "resolution")
        if resolution is None:
            return fail("Выберите резолюцию.", 400, {"resolution_id": "Обязательное поле."})

    now = utcnow()
    updates = {"column_id": target["id"]}
    if target["kind"] == "done":
        updates.update(resolution_id=resolution["id"], closed_at=now)
    elif source["kind"] == "done":
        updates.update(resolution_id=None, closed_at=None)
    if rules["is_return"]:
        updates["reopen_count"] = card["reopen_count"] + 1
    auto_assigned = False
    if target["kind"] == "work" and card["assignee_id"] is None and g.user["role"] == "developer":
        updates["assignee_id"] = g.user["id"]
        auto_assigned = True
    sets = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE cards SET {sets} WHERE id = ?", (*updates.values(), card_id))
    touch(db, card_id)

    M.log(db, card_id, g.user["id"], "moved", "column", source["name"], target["name"])
    A.details(f"«{card['title']}»: {source['name']} → {target['name']}"
              + (f", резолюция: {resolution['name']}" if resolution else "")
              + (f", комментарий: {comment[:120]}" if comment else ""))
    if resolution:
        M.log(db, card_id, g.user["id"], "resolved", "resolution", None, resolution["name"])
    if auto_assigned:
        M.log(db, card_id, g.user["id"], "assigned", "assignee", None, g.user["display_name"])
    mentioned = set()
    if comment:
        db.execute("INSERT INTO comments (card_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
                   (card_id, g.user["id"], comment, now))
        mentioned = mention_notify(db, card_id, card["title"], [comment])

    who = g.user["display_name"]
    title = card["title"]
    if rules["is_return"]:
        text, channel_event = f"{who} вернул(а) задачу в «{target['name']}»: {comment[:200]}", "returned"
    elif target["kind"] == "done":
        text, channel_event = f"{who} закрыл(а) задачу: {resolution['name']}", "closed"
    elif target["kind"] == "review":
        text, channel_event = f"{who} отправил(а) задачу на проверку", "review"
    else:
        text, channel_event = f"{who} переместил(а) задачу в «{target['name']}»", None
    owners = [u for u in N.users_by_ids(db, [card["author_id"], card["assignee_id"]]) if u["id"] not in mentioned]
    done = N.notify_users(db, "status", card_id, title, g.user, owners, text)
    if target["kind"] == "review":
        N.notify_users(db, "review", card_id, title, g.user,
                       [u for u in N.subscribers(db, "review") if u["id"] not in done | mentioned], text)
    if channel_event:
        icon = {"returned": "↩️", "closed": "✅", "review": "🔎"}[channel_event]
        N.notify_channel(db, channel_event, card_id, title, f"{icon} {text}")
    db.commit()
    return full_card_response(db, card_id)


@bp.post("/cards/<int:card_id>/assign")
def card_assign(card_id):
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    raw = body().get("assignee_id")
    try:
        new_id = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return fail("Неверный исполнитель.")
    if new_id == card["assignee_id"]:
        return full_card_response(db, card_id)
    if not P.can_assign(g.user, card["assignee_id"], new_id):
        return fail("Вы не можете изменить исполнителя этой задачи.", 403)
    if new_id is not None and user_can_be_assigned(db, new_id) is None:
        return fail("Пользователь не найден или отключён.", 404)
    db.execute("UPDATE cards SET assignee_id = ? WHERE id = ?", (new_id, card_id))
    touch(db, card_id)
    M.log(db, card_id, g.user["id"], "assigned", "assignee",
          M.display_name(db, card["assignee_id"]), M.display_name(db, new_id))
    A.details(f"«{card['title']}»: {M.display_name(db, card['assignee_id'])} → {M.display_name(db, new_id)}")
    if new_id is not None:
        text = ("взял(а) задачу себе" if new_id == g.user["id"] else "назначил(а) вам задачу")
        N.notify_users(db, "assigned", card_id, card["title"], g.user, N.users_by_ids(db, [new_id]),
                       f"{g.user['display_name']} {text}")
        if new_id == g.user["id"]:
            N.notify_users(db, "status", card_id, card["title"], g.user, N.users_by_ids(db, [card["author_id"]]),
                           f"{g.user['display_name']} взял(а) вашу задачу в работу")
    db.commit()
    return full_card_response(db, card_id)


@bp.post("/cards/<int:card_id>/comments")
def comment_create(card_id):
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    text = clean_text(body().get("body"), LIMITS["comment"])
    if not text:
        return fail("Комментарий не может быть пустым.", 400, {"body": "Обязательное поле."})
    if len(text) > LIMITS["comment"]:
        return fail(f"Комментарий не длиннее {LIMITS['comment']} символов.", 400, {"body": "Слишком длинно."})
    db.execute("INSERT INTO comments (card_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
               (card_id, g.user["id"], text, utcnow()))
    db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (utcnow(), card_id))
    mentioned = mention_notify(db, card_id, card["title"], [text])
    owners = [u for u in N.users_by_ids(db, [card["author_id"], card["assignee_id"]]) if u["id"] not in mentioned]
    N.notify_users(db, "comment", card_id, card["title"], g.user, owners,
                   f"{g.user['display_name']}: {text[:200]}")
    db.commit()
    A.details(f"«{card['title']}»: {text[:150]}")
    return full_card_response(db, card_id, 201)


# ---------------------------------------------------------------- вложения

SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]


def sniff_image(head):
    """Определяет тип по содержимому файла, а не по имени и MIME от браузера."""
    for magic, mime, ext in SIGNATURES:
        if head.startswith(magic):
            return mime, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


@bp.post("/cards/<int:card_id>/attachments")
def attachment_upload(card_id):
    denied = require_worker()
    if denied:
        return denied
    db = get_db()
    if load_card(card_id) is None:
        return fail("Карточка не найдена.", 404)
    count = db.execute("SELECT COUNT(*) FROM attachments WHERE card_id = ?", (card_id,)).fetchone()[0]
    if count >= MAX_ATTACHMENTS_PER_CARD:
        return fail(f"Не больше {MAX_ATTACHMENTS_PER_CARD} изображений на карточку.", 400)
    file = request.files.get("file")
    if file is None:
        return fail("Файл не получен.")
    data = file.stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return fail("Файл больше 5 МБ.", 413)
    if not data:
        return fail("Пустой файл.")
    mime, ext = sniff_image(data[:16])
    if mime is None:
        return fail("Можно загружать только PNG, JPG, GIF или WebP.", 415)
    stored = secrets.token_hex(16) + ext
    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, stored), "wb") as fh:
        fh.write(data)
    original = re.sub(r"[^\w.\- ()]", "_", (file.filename or "image")[:100]) or "image"
    db.execute(
        "INSERT INTO attachments (card_id, uploader_id, stored_name, original_name, mime, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_id, g.user["id"], stored, original, mime, len(data), utcnow()),
    )
    M.log(db, card_id, g.user["id"], "attachment", None, None, original)
    A.details(original)
    db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (utcnow(), card_id))
    db.commit()
    return full_card_response(db, card_id, 201)


@bp.delete("/attachments/<int:att_id>")
def attachment_delete(att_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if att is None:
        return fail("Файл не найден.", 404)
    if not P.can_delete_attachment(g.user, att["uploader_id"]):
        return fail("Удалить файл может только тот, кто его загрузил, или администратор.", 403)
    db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    M.log(db, att["card_id"], g.user["id"], "attachment_deleted", None, att["original_name"], None)
    A.details(att["original_name"])
    g.audit_card_id = att["card_id"]
    db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (utcnow(), att["card_id"]))
    db.commit()
    _remove_file(att["stored_name"])
    return full_card_response(db, att["card_id"])


def _remove_file(stored_name):
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- архив (только админ)

@bp.post("/cards/<int:card_id>/archive")
def card_archive(card_id):
    if not P.is_admin(g.user):
        return fail("Архивировать может только администратор.", 403)
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    if not card["archived_at"]:
        db.execute("UPDATE cards SET archived_at = ? WHERE id = ?", (utcnow(), card_id))
        touch(db, card_id)
        M.log(db, card_id, g.user["id"], "archived")
        db.commit()
    return full_card_response(db, card_id)


@bp.post("/cards/<int:card_id>/restore")
def card_restore(card_id):
    if not P.is_admin(g.user):
        return fail("Восстанавливать может только администратор.", 403)
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    if card["archived_at"]:
        db.execute("UPDATE cards SET archived_at = NULL WHERE id = ?", (card_id,))
        touch(db, card_id)
        M.log(db, card_id, g.user["id"], "restored")
        db.commit()
    return full_card_response(db, card_id)


@bp.delete("/cards/<int:card_id>")
def card_delete(card_id):
    if not P.is_admin(g.user):
        return fail("Удалять может только администратор.", 403)
    db = get_db()
    card = load_card(card_id)
    if card is None:
        return fail("Карточка не найдена.", 404)
    if not card["archived_at"]:
        return fail("Удалить навсегда можно только карточку из архива. Сначала заархивируйте её.", 409)
    files = [r["stored_name"] for r in db.execute("SELECT stored_name FROM attachments WHERE card_id = ?", (card_id,))]
    db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    db.commit()
    A.details(f"«{card['title']}»")
    for name in files:
        _remove_file(name)
    return jsonify(ok=True)


# ---------------------------------------------------------------- уведомления на сайте

@bp.get("/notifications")
def notifications():
    db = get_db()
    rows = db.execute(
        "SELECT n.*, c.title AS card_title FROM notifications n LEFT JOIN cards c ON c.id = n.card_id "
        "WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 40",
        (g.user["id"],),
    ).fetchall()
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                        (g.user["id"],)).fetchone()[0]
    return jsonify(unread=unread, items=[
        {"id": r["id"], "card_id": r["card_id"], "card_title": r["card_title"], "event": r["event"],
         "text": r["text"], "created_at": r["created_at"], "read": r["read_at"] is not None}
        for r in rows
    ])


@bp.post("/notifications/read")
def notifications_read():
    db = get_db()
    data = body()
    if data.get("all"):
        db.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                   (utcnow(), g.user["id"]))
    else:
        ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()][:100]
        if ids:
            marks = ",".join("?" * len(ids))
            db.execute(f"UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL "
                       f"AND id IN ({marks})", (utcnow(), g.user["id"], *ids))
    # Старые прочитанные уведомления (старше 60 дней) чистим, чтобы база не разрасталась
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?", (old,))
    db.commit()
    return notifications()


# ---------------------------------------------------------------- профиль

@bp.patch("/profile")
def profile_update():
    db = get_db()
    data = body()
    errors = {}
    updates = {}
    if "display_name" in data:
        name = clean_text(data.get("display_name"), LIMITS["display_name"])
        if not name or len(name) > LIMITS["display_name"]:
            errors["display_name"] = f"От 1 до {LIMITS['display_name']} символов."
        updates["display_name"] = name
    if "notify_events" in data:
        events = data.get("notify_events")
        if not isinstance(events, list) or any(e not in N.EVENTS for e in events):
            errors["notify_events"] = "Неизвестное событие."
        else:
            updates["notify_events"] = ",".join(e for e in N.EVENTS if e in events)
    for flag in ("notify_discord", "notify_telegram"):
        if flag in data:
            updates[flag] = 1 if data.get(flag) else 0
    new_username = None
    if "username" in data:
        new_username = (data.get("username") or "").strip()
        if not USERNAME_RE.match(new_username):
            errors["username"] = "2–32 символа: латиница, цифры, _ . -"
        elif M.username_taken(db, new_username, g.user["id"]):
            errors["username"] = "Этот логин занят (сейчас или раньше был у другого участника)."
    if errors:
        return fail("Проверьте поля.", 400, errors)
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), g.user["id"]))
        if updates.get("display_name") and updates["display_name"] != g.user["display_name"]:
            A.details(f"имя: {g.user['display_name']} → {updates['display_name']}")
        if "notify_events" in updates or "notify_discord" in updates or "notify_telegram" in updates:
            A.details("настройки уведомлений")
    if new_username and M.change_username(db, g.user["id"], new_username):
        A.details(f"логин: @{g.user['username']} → @{new_username}")
    db.commit()
    return jsonify(ok=True, username=M.get_user(db, g.user["id"])["username"])


MAX_AVATAR_BYTES = 2 * 1024 * 1024


@bp.post("/profile/avatar")
def avatar_upload():
    file = request.files.get("file")
    if file is None:
        return fail("Файл не получен.")
    data = file.stream.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        return fail("Аватарка больше 2 МБ.", 413)
    mime, ext = sniff_image(data[:16])
    if mime is None:
        return fail("Можно загружать только PNG, JPG, GIF или WebP.", 415)
    folder = current_app.config["AVATAR_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    stored = secrets.token_hex(16) + ext
    with open(os.path.join(folder, stored), "wb") as fh:
        fh.write(data)
    db = get_db()
    old = g.user["avatar"]
    db.execute("UPDATE users SET avatar = ? WHERE id = ?", (stored, g.user["id"]))
    db.commit()
    _remove_avatar(old)
    return jsonify(avatar=M.avatar_url(stored))


@bp.delete("/profile/avatar")
def avatar_delete():
    db = get_db()
    old = g.user["avatar"]
    db.execute("UPDATE users SET avatar = NULL WHERE id = ?", (g.user["id"],))
    db.commit()
    _remove_avatar(old)
    return jsonify(avatar=None)


def _remove_avatar(stored):
    if not stored:
        return
    try:
        os.remove(os.path.join(current_app.config["AVATAR_FOLDER"], stored))
    except OSError:
        pass


@bp.post("/profile/telegram/link")
def telegram_link():
    bot = current_app.extensions["vektor_notify"].telegram
    if bot is None:
        return fail("Telegram-бот не настроен на сервере.", 409)
    if not bot.username:
        return fail("Telegram-бот ещё подключается. Попробуйте через несколько секунд.", 409)
    code = secrets.token_urlsafe(16)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = get_db()
    db.execute("UPDATE users SET telegram_link_code = ?, telegram_link_expires = ? WHERE id = ?",
               (code, expires, g.user["id"]))
    db.commit()
    return jsonify(url=f"https://t.me/{bot.username}?start={code}", expires_at=expires)


@bp.delete("/profile/telegram")
def telegram_unlink():
    db = get_db()
    db.execute("UPDATE users SET telegram_chat_id = NULL, telegram_link_code = NULL WHERE id = ?", (g.user["id"],))
    db.commit()
    return jsonify(ok=True)


@bp.get("/profile/telegram/status")
def telegram_status():
    row = get_db().execute("SELECT telegram_chat_id FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    return jsonify(linked=bool(row["telegram_chat_id"]))


@bp.delete("/profile/discord")
def discord_unlink():
    db = get_db()
    user = g.user
    if not user["password_hash"]:
        return fail("Нельзя отвязать Discord: это ваш единственный способ входа. "
                    "Попросите администратора задать вам пароль.", 409)
    db.execute("UPDATE users SET discord_id = NULL WHERE id = ?", (user["id"],))
    db.commit()
    return jsonify(ok=True)


@bp.post("/profile/test-notify")
def profile_test_notify():
    channels = N.send_test(g.user)
    if not channels:
        return fail("Нет подключённых каналов: привяжите Discord или Telegram.", 409)
    return jsonify(channels=channels)


@bp.post("/profile/password")
def profile_password():
    from werkzeug.security import check_password_hash, generate_password_hash

    from .auth import MIN_PASSWORD
    data = body()
    user = g.user
    if user["password_hash"] and not check_password_hash(user["password_hash"], data.get("current") or ""):
        return fail("Текущий пароль неверный.", 400, {"current": "Неверный пароль."})
    new = data.get("new") or ""
    if len(new) < MIN_PASSWORD or len(new) > 200:
        return fail(f"Пароль — от {MIN_PASSWORD} символов.", 400, {"new": "Слишком короткий."})
    if not user["username"]:
        return fail("У аккаунта нет логина.", 409)
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
    db.commit()
    return jsonify(ok=True)
