"""Чтение и запись данных: настройки, колонки, списки, карточки, теги, история."""

import re
import secrets
from datetime import datetime, timedelta, timezone

from .db import utcnow

MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.\-]{2,32})")


# ---------------------------------------------------------------- настройки

def get_settings(db):
    return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def bump_config(db):
    """Меняет «версию настроек», чтобы открытые вкладки перечитали доску."""
    set_setting(db, "config_version", utcnow() + secrets.token_hex(4))


def done_cutoff(db):
    days = int(get_settings(db).get("done_visible_days", "14") or 14)
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- колонки и списки

def list_columns(db):
    rows = db.execute("SELECT * FROM board_columns ORDER BY position, id").fetchall()
    return [column_dict(r) for r in rows]


def column_dict(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "kind": r["kind"],
        "move_roles": [x for x in r["move_roles"].split(",") if x],
        "wip_limit": r["wip_limit"],
        "position": r["position"],
    }


def get_column(db, column_id):
    row = db.execute("SELECT * FROM board_columns WHERE id = ?", (column_id,)).fetchone()
    return column_dict(row) if row else None


def first_column(db):
    row = db.execute(
        "SELECT * FROM board_columns ORDER BY (kind != 'backlog'), position, id LIMIT 1"
    ).fetchone()
    return column_dict(row) if row else None


def list_options(db, kind=None):
    if kind:
        rows = db.execute(
            "SELECT * FROM options WHERE kind = ? ORDER BY position, id", (kind,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM options ORDER BY kind, position, id").fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "name": r["name"],
            "position": r["position"],
            "is_active": bool(r["is_active"]),
        }
        for r in rows
    ]


def options_grouped(db):
    grouped = {"priority": [], "category": [], "resolution": [], "server": []}
    for opt in list_options(db):
        grouped[opt["kind"]].append(opt)
    # Уровень приоритета (1–4 полоски): чем выше в списке, тем важнее
    pr = grouped["priority"]
    n = len(pr)
    for i, opt in enumerate(pr):
        opt["level"] = max(1, round(4 * (n - i) / n)) if n else 1
    return grouped


def get_option(db, option_id, kind, active_only=True):
    if option_id in (None, ""):
        return None
    try:
        option_id = int(option_id)
    except (TypeError, ValueError):
        return None
    sql = "SELECT * FROM options WHERE id = ? AND kind = ?"
    if active_only:
        sql += " AND is_active = 1"
    return db.execute(sql, (option_id, kind)).fetchone()


def option_name(db, option_id):
    if option_id is None:
        return "—"
    row = db.execute("SELECT name FROM options WHERE id = ?", (option_id,)).fetchone()
    return row["name"] if row else "—"


def column_name(db, column_id):
    row = db.execute("SELECT name FROM board_columns WHERE id = ?", (column_id,)).fetchone()
    return row["name"] if row else "—"


# ---------------------------------------------------------------- пользователи

def user_brief(row, prefix=""):
    """Короткая информация о пользователе для фронтенда (без секретов)."""
    uid = row[prefix + "id"] if prefix else row["id"]
    if uid is None:
        return None
    return {
        "id": uid,
        "name": row[prefix + "display_name"],
        "username": row[prefix + "username"],
        "avatar": avatar_url(row[prefix + "avatar"]),
        **({"role": row["role"]} if not prefix else {}),
    }


def avatar_url(stored):
    return f"/avatars/{stored}" if stored else None


def list_active_users(db):
    rows = db.execute(
        "SELECT id, username, display_name, role, avatar FROM users "
        "WHERE is_active = 1 AND role != 'pending' ORDER BY display_name COLLATE NOCASE"
    ).fetchall()
    aliases = {}
    for r in db.execute("SELECT user_id, username FROM username_aliases"):
        aliases.setdefault(r["user_id"], []).append(r["username"])
    users = []
    for r in rows:
        u = user_brief(r)
        u["aliases"] = aliases.get(r["id"], [])  # старые логины — для старых @упоминаний
        users.append(u)
    return users


def username_taken(db, username, except_user_id=None):
    """Логин занят, если он у кого-то сейчас или был раньше у ДРУГОГО человека."""
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row and row["id"] != except_user_id:
        return True
    row = db.execute("SELECT user_id FROM username_aliases WHERE username = ?", (username,)).fetchone()
    return bool(row and row["user_id"] != except_user_id)


def change_username(db, user_id, new_username):
    """Меняет логин. Старый логин остаётся псевдонимом: @старый продолжает указывать на этого человека."""
    user = get_user(db, user_id)
    old = user["username"]
    if old == new_username:
        return False
    if old.lower() != new_username.lower():
        db.execute(
            "INSERT OR IGNORE INTO username_aliases (username, user_id, created_at) VALUES (?, ?, ?)",
            (old, user_id, utcnow()),
        )
    # Вернул себе свой прежний логин — он больше не псевдоним
    db.execute("DELETE FROM username_aliases WHERE username = ? AND user_id = ?", (new_username, user_id))
    db.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
    return True


def get_user(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def display_name(db, user_id):
    if user_id is None:
        return "никто"
    row = db.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["display_name"] if row else "удалённый пользователь"


def find_mentions(db, *texts):
    """Ищет @логины в тексте и возвращает строки активных пользователей."""
    names = set()
    for text in texts:
        names.update(m.lower() for m in MENTION_RE.findall(text or ""))
    if not names:
        return []
    marks = ",".join("?" * len(names))
    return db.execute(
        f"SELECT * FROM users WHERE is_active = 1 AND role != 'pending' AND ("
        f" lower(username) IN ({marks})"
        f" OR id IN (SELECT user_id FROM username_aliases WHERE lower(username) IN ({marks})))",
        tuple(names) * 2,
    ).fetchall()


# ---------------------------------------------------------------- карточки

CARD_SELECT = """
    SELECT c.*,
           a.id AS a_id, a.display_name AS a_display_name, a.username AS a_username, a.avatar AS a_avatar,
           s.id AS s_id, s.display_name AS s_display_name, s.username AS s_username, s.avatar AS s_avatar,
           (SELECT COUNT(*) FROM comments cm WHERE cm.card_id = c.id) AS comment_count,
           (SELECT COUNT(*) FROM attachments at WHERE at.card_id = c.id) AS attachment_count
    FROM cards c
    LEFT JOIN users a ON a.id = c.author_id
    LEFT JOIN users s ON s.id = c.assignee_id
"""


def tags_for(db, card_ids):
    if not card_ids:
        return {}
    result = {cid: [] for cid in card_ids}
    marks = ",".join("?" * len(card_ids))
    rows = db.execute(
        f"SELECT ct.card_id, t.id, t.name FROM card_tags ct JOIN tags t ON t.id = ct.tag_id "
        f"WHERE ct.card_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
        tuple(card_ids),
    ).fetchall()
    for r in rows:
        result[r["card_id"]].append({"id": r["id"], "name": r["name"]})
    return result


def card_summary(row, tags, short=True):
    desc = row["description"] or ""
    return {
        "id": row["id"],
        "title": row["title"],
        "description": (desc[:280] + "…") if short and len(desc) > 280 else desc,
        "column_id": row["column_id"],
        "priority_id": row["priority_id"],
        "category_id": row["category_id"],
        "server_id": row["server_id"],
        "resolution_id": row["resolution_id"],
        "author": user_brief(row, "a_"),
        "assignee": user_brief(row, "s_"),
        "tags": tags,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "archived_at": row["archived_at"],
        "comment_count": row["comment_count"],
        "attachment_count": row["attachment_count"],
        "reopen_count": row["reopen_count"],
        "version": row["version"],
    }


def board_cards(db):
    rows = db.execute(
        CARD_SELECT
        + " JOIN board_columns col ON col.id = c.column_id"
        " WHERE c.archived_at IS NULL AND (col.kind != 'done' OR c.closed_at >= ?)",
        (done_cutoff(db),),
    ).fetchall()
    tags = tags_for(db, [r["id"] for r in rows])
    return [card_summary(r, tags[r["id"]]) for r in rows]


def archive_cards(db):
    rows = db.execute(
        CARD_SELECT
        + " JOIN board_columns col ON col.id = c.column_id"
        " WHERE c.archived_at IS NOT NULL OR (col.kind = 'done' AND c.closed_at < ?)"
        " ORDER BY COALESCE(c.archived_at, c.closed_at) DESC LIMIT 500",
        (done_cutoff(db),),
    ).fetchall()
    tags = tags_for(db, [r["id"] for r in rows])
    return [card_summary(r, tags[r["id"]]) for r in rows]


def get_card_row(db, card_id):
    return db.execute(CARD_SELECT + " WHERE c.id = ?", (card_id,)).fetchone()


def card_full(db, card_id):
    row = get_card_row(db, card_id)
    if row is None:
        return None
    card = card_summary(row, tags_for(db, [card_id])[card_id], short=False)
    for field in ("steps", "expected", "actual", "build_version", "coords",
                  "static_id", "occurred_at", "frequency", "video_url"):
        card[field] = row[field]

    card["comments"] = [
        {
            "id": r["id"],
            "body": r["body"],
            "created_at": r["created_at"],
            "author": user_brief(r, "u_"),
        }
        for r in db.execute(
            "SELECT cm.*, u.id AS u_id, u.display_name AS u_display_name, u.username AS u_username, u.avatar AS u_avatar "
            "FROM comments cm LEFT JOIN users u ON u.id = cm.author_id "
            "WHERE cm.card_id = ? ORDER BY cm.id",
            (card_id,),
        )
    ]
    card["history"] = [
        {
            "id": r["id"],
            "action": r["action"],
            "field": r["field"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "created_at": r["created_at"],
            "user": user_brief(r, "u_"),
        }
        for r in db.execute(
            "SELECT h.*, u.id AS u_id, u.display_name AS u_display_name, u.username AS u_username, u.avatar AS u_avatar "
            "FROM card_history h LEFT JOIN users u ON u.id = h.user_id "
            "WHERE h.card_id = ? ORDER BY h.id",
            (card_id,),
        )
    ]
    card["attachments"] = [
        {
            "id": r["id"],
            "url": f"/attachments/{r['stored_name']}",
            "name": r["original_name"],
            "size": r["size"],
            "uploader_id": r["uploader_id"],
            "created_at": r["created_at"],
        }
        for r in db.execute(
            "SELECT * FROM attachments WHERE card_id = ? ORDER BY id", (card_id,)
        )
    ]
    return card


def log(db, card_id, user_id, action, field=None, old=None, new=None):
    db.execute(
        "INSERT INTO card_history (card_id, user_id, action, field, old_value, new_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_id, user_id, action, field,
         None if old is None else str(old)[:300],
         None if new is None else str(new)[:300],
         utcnow()),
    )


# ---------------------------------------------------------------- теги

TAG_RE = re.compile(r"^[\w\-+.# ]{1,30}$", re.UNICODE)


def normalize_tag(name):
    name = " ".join(str(name or "").strip().lstrip("#").split())
    return name if name and TAG_RE.match(name) else None


def set_card_tags(db, card_id, names, user_id):
    """Заменяет теги карточки. Несуществующие теги создаются. Возвращает (было, стало)."""
    clean = []
    for n in names:
        tag = normalize_tag(n)
        if tag and tag.lower() not in {c.lower() for c in clean}:
            clean.append(tag)
    clean = clean[:10]

    old = [t["name"] for t in tags_for(db, [card_id])[card_id]]
    db.execute("DELETE FROM card_tags WHERE card_id = ?", (card_id,))
    for name in clean:
        row = db.execute("SELECT id, name FROM tags WHERE name = ?", (name,)).fetchone()
        if row is None:
            cur = db.execute(
                "INSERT INTO tags (name, created_by, created_at) VALUES (?, ?, ?)",
                (name, user_id, utcnow()),
            )
            tag_id = cur.lastrowid
        else:
            tag_id = row["id"]
        db.execute("INSERT OR IGNORE INTO card_tags (card_id, tag_id) VALUES (?, ?)", (card_id, tag_id))
    new = [t["name"] for t in tags_for(db, [card_id])[card_id]]
    return old, new


def list_tags(db):
    return [
        {"id": r["id"], "name": r["name"], "count": r["cnt"]}
        for r in db.execute(
            "SELECT t.id, t.name, COUNT(ct.card_id) AS cnt FROM tags t "
            "LEFT JOIN card_tags ct ON ct.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        )
    ]


# ---------------------------------------------------------------- отметка изменений

def board_stamp(db):
    """Короткая строка, которая меняется при любом изменении доски."""
    r = db.execute(
        "SELECT (SELECT COALESCE(MAX(updated_at), '') FROM cards) AS u,"
        " (SELECT COUNT(*) FROM cards) AS n,"
        " (SELECT COALESCE(MAX(id), 0) FROM comments) AS cm,"
        " (SELECT COALESCE(MAX(id), 0) FROM attachments) AS at,"
        " (SELECT COUNT(*) FROM attachments) AS atn,"
        " (SELECT COALESCE(MAX(id), 0) FROM card_history) AS h,"
        " (SELECT COALESCE(value, '') FROM settings WHERE key = 'config_version') AS cfg"
    ).fetchone()
    return "|".join(str(r[k]) for k in ("u", "n", "cm", "at", "atn", "h", "cfg"))
