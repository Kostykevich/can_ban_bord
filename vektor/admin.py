"""API администратора: пользователи, колонки, списки (приоритеты/категории/…), теги, настройки."""

from flask import Blueprint, g, jsonify
from werkzeug.security import generate_password_hash

from . import models as M
from .api import body, clean_text, fail
from .auth import MIN_PASSWORD, USERNAME_RE, admin_required
from .constants import COLUMN_KINDS, LIMITS, OPTION_KINDS, ROLES, WORK_ROLES
from .db import get_db, utcnow
from .notify import CHANNEL_EVENTS, DEFAULT_EVENTS

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@bp.before_request
@admin_required
def _only_admin():
    return None


def _changed(db):
    M.bump_config(db)
    db.commit()


# ---------------------------------------------------------------- пользователи

def _user_dict(r):
    return {
        "id": r["id"],
        "username": r["username"],
        "display_name": r["display_name"],
        "role": r["role"],
        "is_active": bool(r["is_active"]),
        "has_password": bool(r["password_hash"]),
        "discord_id": r["discord_id"],
        "telegram": bool(r["telegram_chat_id"]),
        "avatar": M.avatar_url(r["avatar"]),
        "created_at": r["created_at"],
        "last_login_at": r["last_login_at"],
    }


@bp.get("/users")
def users_list():
    rows = get_db().execute(
        "SELECT * FROM users ORDER BY (role != 'pending'), display_name COLLATE NOCASE"
    ).fetchall()
    return jsonify(users=[_user_dict(r) for r in rows])


@bp.post("/users")
def user_create():
    db = get_db()
    data = body()
    errors = {}
    username = (data.get("username") or "").strip()
    display = clean_text(data.get("display_name"), LIMITS["display_name"]) or username
    role = data.get("role")
    password = data.get("password") or ""
    discord_id = (str(data.get("discord_id") or "")).strip() or None
    if not USERNAME_RE.match(username):
        errors["username"] = "2–32 символа: латиница, цифры, _ . -"
    elif M.username_taken(db, username):
        errors["username"] = "Этот логин занят (сейчас или раньше был у другого участника)."
    if len(display) > LIMITS["display_name"]:
        errors["display_name"] = "Слишком длинное имя."
    if role not in WORK_ROLES:
        errors["role"] = "Выберите роль."
    if discord_id and not discord_id.isdigit():
        errors["discord_id"] = "Discord ID — только цифры."
    elif discord_id and db.execute("SELECT 1 FROM users WHERE discord_id = ?", (discord_id,)).fetchone():
        errors["discord_id"] = "Этот Discord уже привязан к другому пользователю."
    if not discord_id and len(password) < MIN_PASSWORD:
        errors["password"] = f"Пароль от {MIN_PASSWORD} символов (или укажите Discord ID)."
    elif password and len(password) < MIN_PASSWORD:
        errors["password"] = f"Пароль от {MIN_PASSWORD} символов."
    if errors:
        return fail("Проверьте поля.", 400, errors)
    db.execute(
        "INSERT INTO users (username, display_name, password_hash, discord_id, role, notify_events, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, display, generate_password_hash(password) if password else None,
         discord_id, role, DEFAULT_EVENTS[role], utcnow()),
    )
    _changed(db)
    return users_list(), 201


@bp.patch("/users/<int:user_id>")
def user_update(user_id):
    db = get_db()
    user = M.get_user(db, user_id)
    if user is None:
        return fail("Пользователь не найден.", 404)
    data = body()
    updates = {}
    if "role" in data:
        if data["role"] not in ROLES:
            return fail("Неизвестная роль.")
        if user_id == g.user["id"] and data["role"] != "admin":
            return fail("Нельзя снять роль администратора с самого себя.", 409)
        updates["role"] = data["role"]
        if user["role"] == "pending" and data["role"] in DEFAULT_EVENTS:
            updates["notify_events"] = DEFAULT_EVENTS[data["role"]]
    if "is_active" in data:
        if user_id == g.user["id"] and not data["is_active"]:
            return fail("Нельзя отключить самого себя.", 409)
        updates["is_active"] = 1 if data["is_active"] else 0
    if "display_name" in data:
        name = clean_text(data["display_name"], LIMITS["display_name"])
        if not name or len(name) > LIMITS["display_name"]:
            return fail("Имя от 1 до 50 символов.")
        updates["display_name"] = name
    new_username = None
    if "username" in data:
        new_username = (data.get("username") or "").strip()
        if not USERNAME_RE.match(new_username):
            return fail("Логин: 2–32 символа, латиница, цифры, _ . -", 400, {"username": "Неверный логин."})
        if M.username_taken(db, new_username, user_id):
            return fail("Этот логин занят (сейчас или раньше был у другого участника).", 409,
                        {"username": "Логин занят."})
    if "discord_id" in data:
        did = (str(data.get("discord_id") or "")).strip() or None
        if did and not did.isdigit():
            return fail("Discord ID — только цифры.")
        if did and db.execute("SELECT 1 FROM users WHERE discord_id = ? AND id != ?", (did, user_id)).fetchone():
            return fail("Этот Discord уже привязан к другому пользователю.", 409)
        updates["discord_id"] = did
    if new_username and M.change_username(db, user_id, new_username):
        _changed(db)
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), user_id))
        if updates.get("role") and user["role"] == "pending":
            db.execute(
                "INSERT INTO notifications (user_id, card_id, event, text, created_at) VALUES (?, NULL, 'access', ?, ?)",
                (user_id, f"Вам выдан доступ: {ROLES[updates['role']]}", utcnow()),
            )
        _changed(db)
    return users_list()


@bp.post("/users/<int:user_id>/password")
def user_password(user_id):
    db = get_db()
    if M.get_user(db, user_id) is None:
        return fail("Пользователь не найден.", 404)
    password = body().get("password") or ""
    if len(password) < MIN_PASSWORD or len(password) > 200:
        return fail(f"Пароль от {MIN_PASSWORD} символов.", 400, {"password": "Слишком короткий."})
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- колонки

def _column_payload(db, data, current=None):
    errors = {}
    values = {}
    if "name" in data or current is None:
        name = clean_text(data.get("name"), LIMITS["column_name"])
        if not name or len(name) > LIMITS["column_name"]:
            errors["name"] = f"От 1 до {LIMITS['column_name']} символов."
        values["name"] = name
    if "kind" in data or current is None:
        if data.get("kind") not in COLUMN_KINDS:
            errors["kind"] = "Выберите тип."
        values["kind"] = data.get("kind")
    if "move_roles" in data or current is None:
        roles = data.get("move_roles") or []
        if not isinstance(roles, list) or any(r not in ("tester", "developer") for r in roles):
            errors["move_roles"] = "Неверные роли."
        else:
            values["move_roles"] = ",".join(r for r in ("tester", "developer") if r in roles)
    if "wip_limit" in data:
        raw = data.get("wip_limit")
        if raw in (None, "", 0, "0"):
            values["wip_limit"] = None
        else:
            try:
                limit = int(raw)
                assert 1 <= limit <= 999
                values["wip_limit"] = limit
            except (TypeError, ValueError, AssertionError):
                errors["wip_limit"] = "Число от 1 до 999 или пусто."
    return values, errors


@bp.get("/columns")
def columns_list():
    db = get_db()
    cols = M.list_columns(db)
    for col in cols:
        col["card_count"] = db.execute("SELECT COUNT(*) FROM cards WHERE column_id = ?", (col["id"],)).fetchone()[0]
    return jsonify(columns=cols, kinds=COLUMN_KINDS)


@bp.post("/columns")
def column_create():
    db = get_db()
    values, errors = _column_payload(db, body())
    if errors:
        return fail("Проверьте поля.", 400, errors)
    pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM board_columns").fetchone()[0]
    db.execute(
        "INSERT INTO board_columns (name, kind, move_roles, wip_limit, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (values["name"], values["kind"], values["move_roles"], values.get("wip_limit"), pos, utcnow()),
    )
    _changed(db)
    return columns_list(), 201


@bp.patch("/columns/<int:column_id>")
def column_update(column_id):
    db = get_db()
    current = M.get_column(db, column_id)
    if current is None:
        return fail("Колонка не найдена.", 404)
    values, errors = _column_payload(db, body(), current)
    if errors:
        return fail("Проверьте поля.", 400, errors)
    if values.get("kind") and values["kind"] != current["kind"]:
        if current["kind"] == "done":
            # Карточки в колонке перестанут считаться закрытыми
            db.execute("UPDATE cards SET closed_at = NULL, resolution_id = NULL WHERE column_id = ?", (column_id,))
        if values["kind"] == "done":
            db.execute("UPDATE cards SET closed_at = ? WHERE column_id = ? AND closed_at IS NULL",
                       (utcnow(), column_id))
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE board_columns SET {sets} WHERE id = ?", (*values.values(), column_id))
        _changed(db)
    return columns_list()


@bp.post("/columns/<int:column_id>/move")
def column_move(column_id):
    return _reorder(get_db(), "board_columns", column_id, body().get("direction"), None) or columns_list()


@bp.delete("/columns/<int:column_id>")
def column_delete(column_id):
    db = get_db()
    current = M.get_column(db, column_id)
    if current is None:
        return fail("Колонка не найдена.", 404)
    if db.execute("SELECT COUNT(*) FROM board_columns").fetchone()[0] <= 1:
        return fail("Нельзя удалить последнюю колонку.", 409)
    count = db.execute("SELECT COUNT(*) FROM cards WHERE column_id = ?", (column_id,)).fetchone()[0]
    if count:
        target = M.get_column(db, body().get("target_column_id"))
        if target is None or target["id"] == column_id:
            return fail(f"В колонке {count} карточек — выберите, куда их перенести.", 400,
                        {"target_column_id": "Обязательное поле."})
        now = utcnow()
        cards = db.execute("SELECT id FROM cards WHERE column_id = ?", (column_id,)).fetchall()
        closing = "closed_at = COALESCE(closed_at, ?)" if target["kind"] == "done" else "closed_at = NULL, resolution_id = NULL"
        params = (target["id"], now, now, column_id) if target["kind"] == "done" else (target["id"], now, column_id)
        db.execute(f"UPDATE cards SET column_id = ?, {closing}, updated_at = ?, version = version + 1 "
                   "WHERE column_id = ?", params)
        for c in cards:
            M.log(db, c["id"], g.user["id"], "moved", "column", current["name"] + " (удалена)", target["name"])
    db.execute("DELETE FROM board_columns WHERE id = ?", (column_id,))
    _changed(db)
    return columns_list()


# ---------------------------------------------------------------- списки

@bp.get("/options")
def options_list():
    db = get_db()
    grouped = M.options_grouped(db)
    for items in grouped.values():
        for opt in items:
            opt["usage"] = db.execute(
                "SELECT COUNT(*) FROM cards WHERE priority_id = ? OR category_id = ? OR server_id = ? OR resolution_id = ?",
                (opt["id"],) * 4,
            ).fetchone()[0]
    return jsonify(options=grouped, kinds=OPTION_KINDS)


@bp.post("/options")
def option_create():
    db = get_db()
    data = body()
    kind = data.get("kind")
    name = clean_text(data.get("name"), LIMITS["option_name"])
    if kind not in OPTION_KINDS:
        return fail("Неизвестный список.")
    if not name or len(name) > LIMITS["option_name"]:
        return fail(f"Название от 1 до {LIMITS['option_name']} символов.", 400, {"name": "Неверная длина."})
    if db.execute("SELECT 1 FROM options WHERE kind = ? AND name = ?", (kind, name)).fetchone():
        return fail("Такое значение уже есть.", 409, {"name": "Уже есть."})
    pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM options WHERE kind = ?", (kind,)).fetchone()[0]
    db.execute("INSERT INTO options (kind, name, position, created_at) VALUES (?, ?, ?, ?)", (kind, name, pos, utcnow()))
    _changed(db)
    return options_list(), 201


def _active_count(db, kind):
    return db.execute("SELECT COUNT(*) FROM options WHERE kind = ? AND is_active = 1", (kind,)).fetchone()[0]


@bp.patch("/options/<int:option_id>")
def option_update(option_id):
    db = get_db()
    opt = db.execute("SELECT * FROM options WHERE id = ?", (option_id,)).fetchone()
    if opt is None:
        return fail("Значение не найдено.", 404)
    data = body()
    if "name" in data:
        name = clean_text(data["name"], LIMITS["option_name"])
        if not name or len(name) > LIMITS["option_name"]:
            return fail("Неверная длина названия.")
        if db.execute("SELECT 1 FROM options WHERE kind = ? AND name = ? AND id != ?",
                      (opt["kind"], name, option_id)).fetchone():
            return fail("Такое значение уже есть.", 409)
        db.execute("UPDATE options SET name = ? WHERE id = ?", (name, option_id))
    if "is_active" in data:
        active = 1 if data["is_active"] else 0
        if not active and opt["is_active"] and _active_count(db, opt["kind"]) <= 1:
            return fail("Должно остаться хотя бы одно активное значение.", 409)
        db.execute("UPDATE options SET is_active = ? WHERE id = ?", (active, option_id))
    _changed(db)
    return options_list()


@bp.post("/options/<int:option_id>/move")
def option_move(option_id):
    db = get_db()
    opt = db.execute("SELECT kind FROM options WHERE id = ?", (option_id,)).fetchone()
    if opt is None:
        return fail("Значение не найдено.", 404)
    return _reorder(db, "options", option_id, body().get("direction"), opt["kind"]) or options_list()


@bp.delete("/options/<int:option_id>")
def option_delete(option_id):
    db = get_db()
    opt = db.execute("SELECT * FROM options WHERE id = ?", (option_id,)).fetchone()
    if opt is None:
        return fail("Значение не найдено.", 404)
    used = db.execute(
        "SELECT COUNT(*) FROM cards WHERE priority_id = ? OR category_id = ? OR server_id = ? OR resolution_id = ?",
        (option_id,) * 4,
    ).fetchone()[0]
    if used:
        return fail(f"Используется в {used} карточках. Отключите значение — оно пропадёт из форм, "
                    "но останется у старых карточек.", 409)
    if opt["is_active"] and _active_count(db, opt["kind"]) <= 1:
        return fail("Должно остаться хотя бы одно активное значение.", 409)
    db.execute("DELETE FROM options WHERE id = ?", (option_id,))
    _changed(db)
    return options_list()


def _reorder(db, table, item_id, direction, kind):
    """Меняет местами элемент с соседом сверху (-1) или снизу (+1)."""
    if direction not in (-1, 1):
        return fail("Неверное направление.")
    where, params = ("WHERE kind = ?", (kind,)) if kind else ("", ())
    rows = db.execute(f"SELECT id FROM {table} {where} ORDER BY position, id", params).fetchall()
    ids = [r["id"] for r in rows]
    if item_id not in ids:
        return fail("Не найдено.", 404)
    i = ids.index(item_id)
    j = i + direction
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, rid in enumerate(ids):
            db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, rid))
        _changed(db)
    return None


# ---------------------------------------------------------------- теги

@bp.get("/tags")
def tags_list():
    return jsonify(tags=M.list_tags(get_db()))


@bp.patch("/tags/<int:tag_id>")
def tag_rename(tag_id):
    db = get_db()
    if db.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone() is None:
        return fail("Тег не найден.", 404)
    name = M.normalize_tag(body().get("name"))
    if not name:
        return fail("Тег: до 30 символов, буквы, цифры, пробел и - + . #")
    if db.execute("SELECT 1 FROM tags WHERE name = ? AND id != ?", (name, tag_id)).fetchone():
        return fail("Такой тег уже есть.", 409)
    db.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
    db.execute("UPDATE cards SET updated_at = ? WHERE id IN (SELECT card_id FROM card_tags WHERE tag_id = ?)",
               (utcnow(), tag_id))
    _changed(db)
    return tags_list()


@bp.delete("/tags/<int:tag_id>")
def tag_delete(tag_id):
    db = get_db()
    db.execute("UPDATE cards SET updated_at = ? WHERE id IN (SELECT card_id FROM card_tags WHERE tag_id = ?)",
               (utcnow(), tag_id))
    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    _changed(db)
    return tags_list()


# ---------------------------------------------------------------- общие настройки

@bp.get("/settings")
def settings_get():
    s = M.get_settings(get_db())
    return jsonify(
        board_title=s.get("board_title", ""),
        board_subtitle=s.get("board_subtitle", ""),
        done_visible_days=int(s.get("done_visible_days", "14") or 14),
        channel_events=[e for e in s.get("channel_events", "").split(",") if e],
        channel_event_labels=CHANNEL_EVENTS,
    )


@bp.patch("/settings")
def settings_update():
    db = get_db()
    data = body()
    errors = {}
    for key in ("board_title", "board_subtitle"):
        if key in data:
            text = clean_text(data[key], LIMITS[key])
            if len(text) > LIMITS[key] or (key == "board_title" and not text):
                errors[key] = f"До {LIMITS[key]} символов."
            else:
                M.set_setting(db, key, text)
    if "done_visible_days" in data:
        try:
            days = int(data["done_visible_days"])
            assert 1 <= days <= 365
            M.set_setting(db, "done_visible_days", days)
        except (TypeError, ValueError, AssertionError):
            errors["done_visible_days"] = "Число от 1 до 365."
    if "channel_events" in data:
        events = data["channel_events"]
        if not isinstance(events, list) or any(e not in CHANNEL_EVENTS for e in events):
            errors["channel_events"] = "Неизвестное событие."
        else:
            M.set_setting(db, "channel_events", ",".join(e for e in CHANNEL_EVENTS if e in events))
    if errors:
        db.rollback()
        return fail("Проверьте поля.", 400, errors)
    _changed(db)
    return settings_get()
