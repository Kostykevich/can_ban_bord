"""API администратора сайта: пользователи, теги, общие настройки, журнал.
Колонки, списки, поля и участники досок — в board_admin.py."""

from flask import Blueprint, g, jsonify, request
from werkzeug.security import generate_password_hash

from . import audit as A
from . import models as M
from .api import body, clean_text, fail
from .auth import MIN_PASSWORD, USERNAME_RE, admin_required
from .constants import BOARD_ROLES, LIMITS, ROLES, WORK_ROLES
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
    db = get_db()
    rows = db.execute(
        "SELECT * FROM users ORDER BY (role != 'pending'), display_name COLLATE NOCASE"
    ).fetchall()
    users = []
    for r in rows:
        u = _user_dict(r)
        u["boards"] = [{**b, "role_label": BOARD_ROLES[b["role"]]} for b in M.user_boards(db, r["id"])]
        users.append(u)
    return jsonify(users=users)


def _add_to_first_board(db, user_id, role):
    """Новый участник сразу попадает на первую доску — иначе ему нечего открыть."""
    if role not in ("tester", "developer"):
        return
    if db.execute("SELECT 1 FROM board_members WHERE user_id = ?", (user_id,)).fetchone():
        return
    first = db.execute("SELECT id FROM boards ORDER BY position, id LIMIT 1").fetchone()
    if first:
        db.execute("INSERT INTO board_members (board_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                   (first["id"], user_id, role, utcnow()))


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
    cur = db.execute(
        "INSERT INTO users (username, display_name, password_hash, discord_id, role, notify_events, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, display, generate_password_hash(password) if password else None,
         discord_id, role, DEFAULT_EVENTS[role], utcnow()),
    )
    _add_to_first_board(db, cur.lastrowid, role)
    _changed(db)
    A.details(f"@{username} — {ROLES[role]}")
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
    changes = []
    if "role" in updates and updates["role"] != user["role"]:
        changes.append(f"роль: {ROLES[user['role']]} → {ROLES[updates['role']]}")
    if "is_active" in updates and updates["is_active"] != user["is_active"]:
        changes.append("включён" if updates["is_active"] else "отключён")
    if "display_name" in updates and updates["display_name"] != user["display_name"]:
        changes.append(f"имя: {user['display_name']} → {updates['display_name']}")
    if "discord_id" in updates and updates["discord_id"] != user["discord_id"]:
        changes.append(f"Discord ID: {user['discord_id'] or '—'} → {updates['discord_id'] or '—'}")
    if new_username and new_username != user["username"]:
        changes.append(f"логин: @{user['username']} → @{new_username}")
    A.details(f"@{user['username']}" + (": " + ", ".join(changes) if changes else ""))
    if new_username and M.change_username(db, user_id, new_username):
        _changed(db)
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), user_id))
        if updates.get("role") and user["role"] == "pending":
            _add_to_first_board(db, user_id, updates["role"])
            db.execute(
                "INSERT INTO notifications (user_id, card_id, event, text, created_at) VALUES (?, NULL, 'access', ?, ?)",
                (user_id, f"Вам выдан доступ: {ROLES[updates['role']]}", utcnow()),
            )
        _changed(db)
    return users_list()


@bp.post("/users/<int:user_id>/password")
def user_password(user_id):
    db = get_db()
    target = M.get_user(db, user_id)
    if target is None:
        return fail("Пользователь не найден.", 404)
    A.details(f"@{target['username']}")
    password = body().get("password") or ""
    if len(password) < MIN_PASSWORD or len(password) > 200:
        return fail(f"Пароль от {MIN_PASSWORD} символов.", 400, {"password": "Слишком короткий."})
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- теги

@bp.get("/tags")
def tags_list():
    return jsonify(tags=M.list_tags(get_db()))


@bp.patch("/tags/<int:tag_id>")
def tag_rename(tag_id):
    db = get_db()
    old = db.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if old is None:
        return fail("Тег не найден.", 404)
    name = M.normalize_tag(body().get("name"))
    if not name:
        return fail("Тег: до 30 символов, буквы, цифры, пробел и - + . #")
    if db.execute("SELECT 1 FROM tags WHERE name = ? AND id != ?", (name, tag_id)).fetchone():
        return fail("Такой тег уже есть.", 409)
    db.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
    A.details(f"#{old['name']} → #{name}")
    db.execute("UPDATE cards SET updated_at = ? WHERE id IN (SELECT card_id FROM card_tags WHERE tag_id = ?)",
               (utcnow(), tag_id))
    _changed(db)
    return tags_list()


@bp.delete("/tags/<int:tag_id>")
def tag_delete(tag_id):
    db = get_db()
    tag = db.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if tag:
        A.details(f"#{tag['name']}")
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
    before = M.get_settings(db)
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
    after = M.get_settings(db)
    labels = {"board_title": "заголовок", "board_subtitle": "подзаголовок",
              "done_visible_days": "дней показа закрытых", "channel_events": "события для канала Discord"}
    A.details(", ".join(f"{label}: {before.get(k, '') or '—'} → {after.get(k, '') or '—'}"
                        for k, label in labels.items() if before.get(k) != after.get(k)) or "без изменений")
    _changed(db)
    return settings_get()


# ---------------------------------------------------------------- журнал действий

@bp.get("/audit")
def audit_list():
    """Журнал: последние записи с фильтрами. before_id — для кнопки «Показать ещё»."""
    db = get_db()
    args = request.args
    where, params = [], []
    if args.get("user_id", type=int):
        where.append("l.user_id = ?")
        params.append(args.get("user_id", type=int))
    group = args.get("group")
    if group in A.GROUPS:
        actions = [k for k, (grp, _) in A.ACTIONS.items() if grp == group]
        where.append(f"l.action IN ({','.join('?' * len(actions))})")
        params.extend(actions)
    q = (args.get("q") or "").strip()[:100]
    if q:
        where.append("(l.details LIKE ? OR l.ip LIKE ? OR CAST(l.card_id AS TEXT) = ?)")
        params.extend([f"%{q}%", f"%{q}%", q.lstrip("#")])
    if args.get("before_id", type=int):
        where.append("l.id < ?")
        params.append(args.get("before_id", type=int))
    sql = ("SELECT l.*, u.display_name, u.username, u.avatar, c.title AS card_title FROM audit_log l "
           "LEFT JOIN users u ON u.id = l.user_id LEFT JOIN cards c ON c.id = l.card_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.execute(sql + " ORDER BY l.id DESC LIMIT 101", params).fetchall()
    items = [{
        "id": r["id"],
        "action": r["action"],
        "label": A.ACTIONS.get(r["action"], ("", r["action"]))[1],
        "group": A.ACTIONS.get(r["action"], ("other", ""))[0],
        "details": r["details"],
        "card_id": r["card_id"],
        "card_exists": r["card_title"] is not None,
        "ip": r["ip"],
        "created_at": r["created_at"],
        "user": ({"id": r["user_id"], "name": r["display_name"], "username": r["username"],
                  "avatar": M.avatar_url(r["avatar"])} if r["user_id"] else None),
    } for r in rows[:100]]
    return jsonify(items=items, has_more=len(rows) > 100, groups=A.GROUPS)
