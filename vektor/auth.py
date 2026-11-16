"""Вход: логин и пароль, Discord OAuth2. Сессии, CSRF, ограничение попыток входа."""

import re
import secrets
import threading
import time
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, jsonify,
                   make_response, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import audit, discord_api
from .constants import LIMITS
from .db import get_db, utcnow
from .notify import DEFAULT_EVENTS

bp = Blueprint("auth", __name__)

# Хеш-«пустышка»: проверяем пароль даже для несуществующего логина, чтобы по
# времени ответа нельзя было понять, есть ли такой пользователь.
_DUMMY_HASH = generate_password_hash("vektor-dummy-password")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")
MIN_PASSWORD = 10


# ---------------------------------------------------------------- ограничение попыток

class LoginLimiter:
    """Не более N неудачных попыток за окно времени с одного IP и для одного логина.
    Хранится в памяти процесса: после перезапуска счётчики сбрасываются.
    Это базовая защита от перебора, а не абсолютная."""

    def __init__(self, max_fails=5, window=600):
        self.max_fails = max_fails
        self.window = window
        self.fails = {}
        self.lock = threading.Lock()

    def _keys(self, ip, username):
        return [f"ip:{ip}", f"user:{(username or '').lower()}"]

    def blocked_for(self, ip, username):
        now = time.time()
        worst = 0
        with self.lock:
            for key in self._keys(ip, username):
                times = [t for t in self.fails.get(key, []) if now - t < self.window]
                self.fails[key] = times
                limit = self.max_fails * (4 if key.startswith("ip:") else 1)
                if len(times) >= limit:
                    worst = max(worst, int(self.window - (now - times[0])))
        return worst

    def fail(self, ip, username):
        with self.lock:
            for key in self._keys(ip, username):
                self.fails.setdefault(key, []).append(time.time())

    def reset(self, ip, username):
        with self.lock:
            self.fails.pop(f"user:{(username or '').lower()}", None)


limiter = LoginLimiter()


# ---------------------------------------------------------------- сессия и CSRF

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf") or ""
    if not expected or not secrets.compare_digest(sent, expected):
        if request.path.startswith("/api/"):
            abort(make_response(jsonify(error="Сессия устарела. Обновите страницу и повторите действие."), 400))
        flash("Форма устарела. Попробуйте ещё раз.", "error")
        abort(redirect(request.path))


def login_user(user, method="пароль"):
    session.clear()  # защита от фиксации сессии
    session.permanent = True
    session["uid"] = user["id"]
    csrf_token()
    db = get_db()
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user["id"]))
    audit.write(db, "auth.login", f"способ: {method}", user_id=user["id"])
    db.commit()


def load_user():
    g.user = None
    uid = session.get("uid")
    if uid is None:
        return
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None or not user["is_active"]:
        session.clear()  # пользователя отключили — выходим из всех сессий
        return
    g.user = user


PUBLIC_ENDPOINTS = {"auth.login", "auth.discord_start", "auth.discord_callback", "static", "pages.health"}
PENDING_ENDPOINTS = {"auth.pending", "auth.logout"}


def require_login():
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS:
        return None
    if g.user is None:
        if request.path.startswith("/api/"):
            return jsonify(error="Нужно войти в аккаунт."), 401
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
    if g.user["role"] == "pending" and endpoint not in PENDING_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify(error="Администратор ещё не выдал вам доступ."), 403
        return redirect(url_for("auth.pending"))
    return None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None or g.user["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="Только для администратора."), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def safe_next(target):
    """Разрешаем возврат только на страницы этого же сайта."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("pages.board")


# ---------------------------------------------------------------- маршруты

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("pages.board"))
    discord_enabled = bool(current_app.config.get("DISCORD_CLIENT_ID"))
    error = None
    username = ""
    if request.method == "POST":
        check_csrf()
        username = (request.form.get("username") or "").strip()[:LIMITS["username"]]
        password = request.form.get("password") or ""
        ip = request.remote_addr or "?"
        wait = limiter.blocked_for(ip, username)
        if wait:
            error = f"Слишком много неудачных попыток. Попробуйте через {max(1, wait // 60)} мин."
        else:
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ? AND password_hash IS NOT NULL", (username,)
            ).fetchone()
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if user and ok and user["is_active"]:
                limiter.reset(ip, username)
                login_user(user)
                flash(f"Добро пожаловать, {user['display_name']}!", "success")
                return redirect(safe_next(request.args.get("next")))
            limiter.fail(ip, username)
            db = get_db()
            audit.write(db, "auth.login_failed", f"логин: {username or '—'}",
                        user_id=user["id"] if user else None)
            db.commit()
            error = "Неверный логин или пароль."
    return render_template("login.html", error=error, username=username,
                           discord_enabled=discord_enabled), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user is not None:
        db = get_db()
        audit.write(db, "auth.logout")
        db.commit()
    session.clear()
    flash("Вы вышли из аккаунта.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/pending")
def pending():
    if g.user["role"] != "pending":
        return redirect(url_for("pages.board"))
    return render_template("pending.html")


@bp.route("/auth/discord")
def discord_start():
    cfg = current_app.config
    if not cfg.get("DISCORD_CLIENT_ID"):
        abort(404)
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    # Если пользователь уже вошёл — это привязка Discord к аккаунту
    session["oauth_link"] = bool(g.user)
    return redirect(discord_api.oauth_authorize_url(cfg["DISCORD_CLIENT_ID"], cfg["DISCORD_REDIRECT_URI"], state))


def _unique_username(db, base):
    base = re.sub(r"[^A-Za-z0-9_.\-]", "", base or "")[:28] or "user"
    if len(base) < 2:
        base = f"user{base}"
    name, n = base, 1
    while db.execute(
        "SELECT 1 FROM users WHERE username = ? UNION SELECT 1 FROM username_aliases WHERE username = ?",
        (name, name),
    ).fetchone():
        n += 1
        name = f"{base}{n}"
    return name


@bp.route("/auth/discord/callback")
def discord_callback():
    cfg = current_app.config
    if not cfg.get("DISCORD_CLIENT_ID"):
        abort(404)
    expected = session.pop("oauth_state", None)
    linking = session.pop("oauth_link", False)
    state = request.args.get("state") or ""
    if not expected or not secrets.compare_digest(state, expected):
        flash("Вход через Discord не удался: устаревший запрос. Попробуйте ещё раз.", "error")
        return redirect(url_for("auth.login"))
    if request.args.get("error") or not request.args.get("code"):
        flash("Вход через Discord отменён.", "error")
        return redirect(url_for("auth.login"))
    try:
        info = discord_api.oauth_fetch_user(cfg["DISCORD_CLIENT_ID"], cfg["DISCORD_CLIENT_SECRET"],
                                            cfg["DISCORD_REDIRECT_URI"], request.args["code"])
    except discord_api.DiscordError as err:
        current_app.logger.warning("Discord OAuth: %s", err)
        flash("Discord не ответил. Попробуйте позже.", "error")
        return redirect(url_for("auth.login"))

    discord_id = str(info["id"])
    db = get_db()
    existing = db.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()

    if linking and g.user is not None:
        if existing and existing["id"] != g.user["id"]:
            flash("Этот Discord уже привязан к другому аккаунту.", "error")
        else:
            db.execute("UPDATE users SET discord_id = ? WHERE id = ?", (discord_id, g.user["id"]))
            audit.write(db, "auth.discord_link", f"Discord ID {discord_id}")
            db.commit()
            flash("Discord привязан к аккаунту.", "success")
        return redirect(url_for("pages.profile"))

    if existing is None:
        display = (info.get("global_name") or info.get("username") or "Игрок")[:LIMITS["display_name"]]
        cur = db.execute(
            "INSERT INTO users (username, display_name, discord_id, role, notify_events, created_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (_unique_username(db, info.get("username")), display, discord_id,
             DEFAULT_EVENTS["pending"], utcnow()),
        )
        audit.write(db, "auth.discord_register", f"Discord: {info.get('username', '')} ({discord_id})",
                    user_id=cur.lastrowid)
        db.commit()
        existing = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        _notify_admins_new_user(db, existing)
    if not existing["is_active"]:
        flash("Ваш аккаунт отключён администратором.", "error")
        return redirect(url_for("auth.login"))
    login_user(existing, "Discord")
    if existing["role"] == "pending":
        return redirect(url_for("auth.pending"))
    flash(f"Добро пожаловать, {existing['display_name']}!", "success")
    return redirect(url_for("pages.board"))


def _notify_admins_new_user(db, user):
    """Сообщаем администраторам (на сайте), что кто-то ждёт доступа."""
    for admin in db.execute("SELECT id FROM users WHERE role = 'admin' AND is_active = 1"):
        db.execute(
            "INSERT INTO notifications (user_id, card_id, event, text, created_at) VALUES (?, NULL, 'access', ?, ?)",
            (admin["id"], f"{user['display_name']} ждёт доступа к доске", utcnow()),
        )
    db.commit()
