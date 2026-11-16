"""Сборка приложения Flask (create_app)."""

import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import admin, api, auth, db, pages
from .discord_api import DiscordBot
from .notify import Dispatcher
from .telegram_bot import TelegramBot

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _secret_key(instance_path):
    """Ключ из переменной окружения; для локальной разработки — из файла instance/secret_key."""
    key = os.environ.get("VEKTOR_SECRET_KEY", "").strip()
    if key:
        return key
    path = os.path.join(instance_path, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    os.makedirs(instance_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def _flag(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    app = Flask(__name__, instance_path=os.path.join(BASE_DIR, "instance"),
                template_folder=os.path.join(BASE_DIR, "templates"),
                static_folder=os.path.join(BASE_DIR, "static"))
    https = _flag("HTTPS")
    app.config.update(
        DATABASE=os.environ.get("VEKTOR_DB_PATH") or os.path.join(app.instance_path, "kanban.db"),
        UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
        AVATAR_FOLDER=os.path.join(app.instance_path, "avatars"),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,  # максимальный размер запроса
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=https,
        SESSION_COOKIE_NAME="vektor_session",
        PUBLIC_URL=os.environ.get("PUBLIC_URL", "").strip(),
        HTTPS=https,
        DISCORD_CLIENT_ID=os.environ.get("DISCORD_CLIENT_ID", "").strip(),
        DISCORD_CLIENT_SECRET=os.environ.get("DISCORD_CLIENT_SECRET", "").strip(),
        DISCORD_REDIRECT_URI=os.environ.get("DISCORD_REDIRECT_URI", "").strip(),
        DISCORD_BOT_TOKEN=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
        DISCORD_CHANNEL_ID=os.environ.get("DISCORD_CHANNEL_ID", "").strip(),
        TELEGRAM_BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        NOTIFY_CAPTURE=False,
    )
    hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if hosts:
        app.config["TRUSTED_HOSTS"] = hosts + ["127.0.0.1", "localhost"]
    if test_config:
        app.config.update(test_config)
    app.secret_key = app.config.get("SECRET_KEY") or _secret_key(app.instance_path)
    if app.config["DISCORD_CLIENT_ID"] and not (app.config["DISCORD_CLIENT_SECRET"] and app.config["DISCORD_REDIRECT_URI"]):
        print("[Vektor] Для входа через Discord нужны DISCORD_CLIENT_SECRET и DISCORD_REDIRECT_URI — вход отключён")
        app.config["DISCORD_CLIENT_ID"] = ""

    if _flag("TRUST_PROXY"):
        # За Caddy/IIS: берём настоящий IP и https из заголовков прокси
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    notifier = Dispatcher(app)
    if app.config["DISCORD_BOT_TOKEN"]:
        notifier.discord = DiscordBot(app.config["DISCORD_BOT_TOKEN"], app.config["DISCORD_CHANNEL_ID"] or None)
    if app.config["TELEGRAM_BOT_TOKEN"]:
        notifier.telegram = TelegramBot(app.config["TELEGRAM_BOT_TOKEN"], app.config["DATABASE"],
                                        app.config["PUBLIC_URL"])
    app.extensions["vektor_notify"] = notifier

    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(admin.bp)

    @app.before_request
    def _before():
        auth.load_user()
        auth.check_csrf()
        return auth.require_login()

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com",
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if app.config["HTTPS"]:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error(status, message):
        if request.path.startswith("/api/"):
            return jsonify(error=message), status
        return render_template("error.html", status=status, message=message), status

    app.register_error_handler(400, lambda e: _error(400, "Неверный запрос."))
    app.register_error_handler(403, lambda e: _error(403, "У вас нет доступа к этой странице."))
    app.register_error_handler(404, lambda e: _error(404, "Страница или карточка не найдена."))
    app.register_error_handler(405, lambda e: _error(405, "Такое действие не поддерживается."))
    app.register_error_handler(413, lambda e: _error(413, "Файл или запрос слишком большой (максимум 5 МБ)."))
    app.register_error_handler(500, lambda e: _error(500, "Ошибка на сервере. Попробуйте ещё раз."))

    return app


def start_background(app):
    """Запускает фоновые потоки: очередь уведомлений и подключение ботов.
    Вызывается ровно один раз — из app.py или serve.py."""
    notifier = app.extensions["vektor_notify"]
    notifier.start()
    if notifier.discord:
        notifier.discord.start_gateway()
    if notifier.telegram:
        notifier.telegram.start_polling()
    if not notifier.discord:
        print("[Vektor] Discord-бот не настроен (DISCORD_BOT_TOKEN пуст) — уведомления в Discord выключены")
    if not notifier.telegram:
        print("[Vektor] Telegram-бот не настроен (TELEGRAM_BOT_TOKEN пуст) — уведомления в Telegram выключены")
