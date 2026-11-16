"""Работа с базой SQLite: подключение, миграции, резервные копии.

Версия схемы хранится в PRAGMA user_version. При запуске приложение
применяет только недостающие миграции, а перед этим делает резервную
копию базы в instance/backups. Существующие данные не удаляются.
Миграция №1 умеет «подхватить» базу старого прототипа (таблица cards
с полями title/description/steps/status) и сохранить все карточки.
"""

import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

from . import constants as C


def utcnow():
    """Текущее время в UTC в формате ISO 8601, например 2026-11-16T17:41:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    """Одно соединение на запрос. Закрывается автоматически в close_db."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- миграции

CARD_COLUMNS = {
    # имя: определение для ALTER TABLE (для старой базы-прототипа)
    "steps": "TEXT NOT NULL DEFAULT ''",
    "expected": "TEXT NOT NULL DEFAULT ''",
    "actual": "TEXT NOT NULL DEFAULT ''",
    "column_id": "INTEGER REFERENCES board_columns(id)",
    "priority_id": "INTEGER REFERENCES options(id)",
    "category_id": "INTEGER REFERENCES options(id)",
    "resolution_id": "INTEGER REFERENCES options(id)",
    "server_id": "INTEGER REFERENCES options(id)",
    "build_version": "TEXT NOT NULL DEFAULT ''",
    "coords": "TEXT NOT NULL DEFAULT ''",
    "static_id": "TEXT NOT NULL DEFAULT ''",
    "occurred_at": "TEXT NOT NULL DEFAULT ''",
    "frequency": "TEXT NOT NULL DEFAULT 'unknown'",
    "video_url": "TEXT NOT NULL DEFAULT ''",
    "author_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "assignee_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "reopen_count": "INTEGER NOT NULL DEFAULT 0",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "created_at": "TEXT",
    "updated_at": "TEXT",
    "closed_at": "TEXT",
    "archived_at": "TEXT",
}


def _script(conn, sql):
    """Выполняет несколько SQL-команд внутри текущей транзакции.
    (conn.executescript нельзя: он сам делает COMMIT и ломает атомарность.)"""
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migration_1(conn):
    now = utcnow()
    _script(conn, 
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT,
            discord_id TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'pending'
                CHECK (role IN ('pending', 'tester', 'developer', 'admin')),
            is_active INTEGER NOT NULL DEFAULT 1,
            telegram_chat_id TEXT UNIQUE,
            telegram_link_code TEXT,
            telegram_link_expires TEXT,
            notify_discord INTEGER NOT NULL DEFAULT 1,
            notify_telegram INTEGER NOT NULL DEFAULT 1,
            notify_events TEXT NOT NULL DEFAULT 'assigned,mention,comment,status',
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS board_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('backlog', 'work', 'review', 'done')),
            move_roles TEXT NOT NULL DEFAULT '',
            wip_limit INTEGER,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('priority', 'category', 'resolution', 'server')),
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE (kind, name)
        );
        """
    )

    # Начальные значения настроек (не перезаписывают существующие)
    for key, value in C.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('channel_events', ?)",
        ("created,review,returned,closed",),
    )

    if not conn.execute("SELECT 1 FROM board_columns LIMIT 1").fetchone():
        for pos, (name, kind, roles) in enumerate(C.SEED_COLUMNS):
            conn.execute(
                "INSERT INTO board_columns (name, kind, move_roles, position, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, kind, roles, pos, now),
            )
    if not conn.execute("SELECT 1 FROM options LIMIT 1").fetchone():
        for kind, names in C.SEED_OPTIONS.items():
            for pos, name in enumerate(names):
                conn.execute(
                    "INSERT INTO options (kind, name, position, created_at) VALUES (?, ?, ?, ?)",
                    (kind, name, pos, now),
                )

    # Карточки: новая таблица или дополнение таблицы старого прототипа
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'"
    ).fetchone()
    if not exists:
        conn.execute(
            "CREATE TABLE cards ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL,"
            " description TEXT NOT NULL)"
        )
    have = {row["name"] for row in conn.execute("PRAGMA table_info(cards)")}
    legacy = "status" in have  # старый прототип хранил статус текстом
    for name, definition in CARD_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {definition}")

    def option_id(kind, name):
        row = conn.execute(
            "SELECT id FROM options WHERE kind = ? AND name = ?", (kind, name)
        ).fetchone()
        return row["id"] if row else None

    def column_id(kind):
        row = conn.execute(
            "SELECT id FROM board_columns WHERE kind = ? ORDER BY position LIMIT 1", (kind,)
        ).fetchone()
        return row["id"] if row else None

    if legacy:
        for status, kind in C.LEGACY_STATUS_TO_KIND.items():
            conn.execute(
                "UPDATE cards SET column_id = ? WHERE column_id IS NULL AND status = ?",
                (column_id(kind), status),
            )
        conn.execute(
            "UPDATE cards SET resolution_id = ?, closed_at = COALESCE(closed_at, ?) "
            "WHERE status = 'done' AND resolution_id IS NULL",
            (option_id("resolution", "Исправлено"), now),
        )
    # У старых карточек нет дат — ставим время миграции; автор остаётся неизвестным (NULL)
    conn.execute("UPDATE cards SET column_id = ? WHERE column_id IS NULL", (column_id("backlog"),))
    conn.execute("UPDATE cards SET created_at = ? WHERE created_at IS NULL", (now,))
    conn.execute("UPDATE cards SET updated_at = created_at WHERE updated_at IS NULL")
    conn.execute(
        "UPDATE cards SET priority_id = ? WHERE priority_id IS NULL",
        (option_id("priority", C.DEFAULT_PRIORITY),),
    )
    conn.execute(
        "UPDATE cards SET category_id = ? WHERE category_id IS NULL",
        (option_id("category", C.DEFAULT_CATEGORY),),
    )
    conn.execute(
        "UPDATE cards SET server_id = ? WHERE server_id IS NULL",
        (option_id("server", C.DEFAULT_SERVER),),
    )

    _script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_tags (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (card_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            uploader_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            stored_name TEXT NOT NULL UNIQUE,
            original_name TEXT NOT NULL,
            mime TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cards_column ON cards(column_id);
        CREATE INDEX IF NOT EXISTS idx_cards_assignee ON cards(assignee_id);
        CREATE INDEX IF NOT EXISTS idx_comments_card ON comments(card_id);
        CREATE INDEX IF NOT EXISTS idx_history_card ON card_history(card_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_card ON attachments(card_id);
        CREATE INDEX IF NOT EXISTS idx_card_tags_tag ON card_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);
        """
    )


def _migration_2(conn):
    """Аватарки и старые логины (псевдонимы), чтобы @упоминания работали после смены логина."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "avatar" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS username_aliases ("
        " username TEXT PRIMARY KEY COLLATE NOCASE,"
        " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_user ON username_aliases(user_id)")


# Новые миграции добавляются в конец списка. Старые никогда не меняются.
MIGRATIONS = [_migration_1, _migration_2]


def backup_file(db_path, reason):
    """Копирует базу в instance/backups через безопасный SQLite backup. Возвращает путь."""
    backups = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(backups, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backups, f"kanban-{reason}-{stamp}.db")
    src = connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def migrate(db_path, log=print):
    """Применяет недостающие миграции. Безопасно запускать повторно."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    had_file = os.path.exists(db_path) and os.path.getsize(db_path) > 0

    conn = connect(db_path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        target = len(MIGRATIONS)
        if current >= target:
            return
        has_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        conn.close()

    if had_file and has_tables:
        log(f"[Vektor] Резервная копия перед миграцией: {backup_file(db_path, f'before-v{target}')}")

    conn = connect(db_path)
    conn.isolation_level = None  # транзакции управляются вручную
    try:
        for number in range(current + 1, target + 1):
            conn.execute("BEGIN")
            try:
                MIGRATIONS[number - 1](conn)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            log(f"[Vektor] Применена миграция базы №{number}")
    finally:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    migrate(app.config["DATABASE"], log=app.logger.info if app.testing else print)
