"""HTML-страницы. Сами данные страницы получают через /api."""

import re

from flask import (Blueprint, abort, current_app, g, jsonify, render_template,
                   send_from_directory)

from . import models as M
from .auth import admin_required, csrf_token
from .constants import COLUMN_KINDS, FREQUENCIES, LIMITS, OPTION_KINDS, POLL_SECONDS, ROLES
from .db import get_db
from .notify import CHANNEL_EVENTS, EVENTS

bp = Blueprint("pages", __name__)

STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")


def boot_data(**extra):
    """Данные для JavaScript. Вставляются в страницу как JSON (не исполняемый код)."""
    user = g.user
    return {
        "user": {"id": user["id"], "name": user["display_name"], "username": user["username"],
                 "role": user["role"], "avatar": M.avatar_url(user["avatar"])},
        "csrf": csrf_token(),
        "roles": ROLES,
        "limits": LIMITS,
        "frequencies": FREQUENCIES,
        "poll_seconds": POLL_SECONDS,
        **extra,
    }


@bp.route("/")
def board():
    return render_template("board.html", boot=boot_data(open_card=None), section="Доска задач")


@bp.route("/card/<int:card_id>")
def card(card_id):
    if get_db().execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone() is None:
        abort(404)
    return render_template("board.html", boot=boot_data(open_card=card_id), section="Доска задач")


@bp.route("/profile")
def profile():
    user = g.user
    notifier = current_app.extensions["vektor_notify"]
    data = boot_data(
        profile={
            "display_name": user["display_name"],
            "username": user["username"],
            "has_password": bool(user["password_hash"]),
            "discord_linked": bool(user["discord_id"]),
            "telegram_linked": bool(user["telegram_chat_id"]),
            "notify_discord": bool(user["notify_discord"]),
            "notify_telegram": bool(user["notify_telegram"]),
            "notify_events": [e for e in (user["notify_events"] or "").split(",") if e],
            "avatar": M.avatar_url(user["avatar"]),
            "aliases": [r["username"] for r in get_db().execute(
                "SELECT username FROM username_aliases WHERE user_id = ? ORDER BY created_at", (user["id"],))],
        },
        events=EVENTS,
        discord_login=bool(current_app.config.get("DISCORD_CLIENT_ID")),
        discord_bot=notifier.discord is not None,
        telegram_bot=notifier.telegram is not None,
    )
    if user["role"] == "admin":
        data["audit_users"] = [{"id": r["id"], "name": r["display_name"], "username": r["username"]}
                               for r in get_db().execute("SELECT id, display_name, username FROM users "
                                                         "ORDER BY display_name COLLATE NOCASE")]
    return render_template("profile.html", boot=data, section="Профиль")


@bp.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", boot=boot_data(), section="Пользователи")


@bp.route("/admin/settings")
@admin_required
def admin_settings():
    data = boot_data(column_kinds=COLUMN_KINDS, option_kinds=OPTION_KINDS, channel_events=CHANNEL_EVENTS)
    return render_template("admin_settings.html", boot=data, section="Настройки")


@bp.route("/attachments/<name>")
def attachment(name):
    """Изображения отдаются только вошедшим пользователям и только из папки загрузок."""
    if not STORED_NAME_RE.match(name):
        abort(404)
    row = get_db().execute("SELECT mime FROM attachments WHERE stored_name = ?", (name,)).fetchone()
    if row is None:
        abort(404)
    resp = send_from_directory(current_app.config["UPLOAD_FOLDER"], name, mimetype=row["mime"], max_age=3600)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.route("/avatars/<name>")
def avatar(name):
    """Аватарки видят только вошедшие пользователи."""
    if not STORED_NAME_RE.match(name):
        abort(404)
    if get_db().execute("SELECT 1 FROM users WHERE avatar = ?", (name,)).fetchone() is None:
        abort(404)
    resp = send_from_directory(current_app.config["AVATAR_FOLDER"], name, max_age=86400)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@bp.route("/health")
def health():
    """Простая проверка, что сервер жив (для мониторинга)."""
    get_db().execute("SELECT 1")
    return jsonify(ok=True)


@bp.app_context_processor
def inject():
    unread = 0
    pending = 0
    if getattr(g, "user", None) is not None and g.user["role"] != "pending":
        db = get_db()
        unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                            (g.user["id"],)).fetchone()[0]
        if g.user["role"] == "admin":
            pending = db.execute("SELECT COUNT(*) FROM users WHERE role = 'pending' AND is_active = 1").fetchone()[0]
    settings = {}
    if getattr(g, "user", None) is not None:
        settings = M.get_settings(get_db())
    return {"csrf_token": csrf_token, "unread_count": unread, "pending_count": pending,
            "role_labels": ROLES, "site_settings": settings}
