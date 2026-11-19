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


def _migration_3(conn):
    """Журнал действий для администратора. card_id без внешнего ключа —
    запись остаётся, даже если карточку удалили навсегда."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,"
        " action TEXT NOT NULL,"
        " details TEXT NOT NULL DEFAULT '',"
        " card_id INTEGER,"
        " ip TEXT,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")


def seed_board(conn, name, description, columns, options, builtin, custom, position=None):
    """Создаёт доску с колонками, списками и полями. Возвращает id.
    builtin: [(ключ встроенного поля, показывать_на_карточке)];
    custom: [(название, тип, варианты через \\n, показывать_на_карточке)]."""
    now = utcnow()
    if position is None:
        position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM boards").fetchone()[0]
    board_id = conn.execute(
        "INSERT INTO boards (name, description, position, created_at) VALUES (?, ?, ?, ?)",
        (name, description, position, now),
    ).lastrowid
    for pos, (col_name, kind, roles) in enumerate(columns):
        conn.execute(
            "INSERT INTO board_columns (board_id, name, kind, move_roles, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (board_id, col_name, kind, roles, pos, now),
        )
    for kind, names in options.items():
        for pos, opt in enumerate(names):
            conn.execute(
                "INSERT INTO options (board_id, kind, name, position, created_at) VALUES (?, ?, ?, ?, ?)",
                (board_id, kind, opt, pos, now),
            )
    seed_fields(conn, board_id, builtin, custom)
    return board_id


def seed_fields(conn, board_id, builtin, custom):
    now = utcnow()
    pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM board_fields WHERE board_id = ?",
                       (board_id,)).fetchone()[0]
    for key, on_card in builtin:
        conn.execute(
            "INSERT OR IGNORE INTO board_fields (board_id, key, label, type, show_on_card, position, created_at) "
            "VALUES (?, ?, ?, 'builtin', ?, ?, ?)",
            (board_id, key, C.BUILTIN_FIELDS[key]["label"], 1 if on_card else 0, pos, now),
        )
        pos += 1
    for label, ftype, choices, on_card in custom:
        cur = conn.execute(
            "INSERT INTO board_fields (board_id, key, label, type, choices, show_on_card, position, created_at) "
            "VALUES (?, '', ?, ?, ?, ?, ?, ?)",
            (board_id, label, ftype, choices, 1 if on_card else 0, pos, now),
        )
        conn.execute("UPDATE board_fields SET key = ? WHERE id = ?", (f"f{cur.lastrowid}", cur.lastrowid))
        pos += 1


def _migration_4(conn):
    """Несколько досок (подразделений): у каждой свои колонки, списки, поля карточек и участники.
    Существующие колонки, списки и карточки переезжают на доску №1 «Баги»."""
    now = utcnow()
    _script(conn, """
        CREATE TABLE boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE board_members (
            board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('lead', 'developer', 'tester')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (board_id, user_id)
        );
        CREATE TABLE board_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            label TEXT NOT NULL,
            type TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 0,
            choices TEXT NOT NULL DEFAULT '',
            hint TEXT NOT NULL DEFAULT '',
            show_on_card INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE card_values (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            field_id INTEGER NOT NULL REFERENCES board_fields(id) ON DELETE CASCADE,
            value TEXT NOT NULL,
            PRIMARY KEY (card_id, field_id)
        );
        CREATE INDEX idx_board_members_user ON board_members(user_id);
        CREATE INDEX idx_board_fields_board ON board_fields(board_id, position)
    """)
    conn.execute("INSERT INTO boards (id, name, description, position, created_at) VALUES (1, 'Баги', ?, 0, ?)",
                 (C.DEFAULT_SETTINGS["board_subtitle"], now))
    conn.execute("ALTER TABLE board_columns ADD COLUMN board_id INTEGER REFERENCES boards(id) ON DELETE CASCADE")
    conn.execute("ALTER TABLE cards ADD COLUMN board_id INTEGER REFERENCES boards(id)")
    conn.execute("UPDATE board_columns SET board_id = 1")
    conn.execute("UPDATE cards SET board_id = 1")
    conn.execute("CREATE INDEX idx_columns_board ON board_columns(board_id, position)")
    conn.execute("CREATE INDEX idx_cards_board ON cards(board_id)")

    # Списки теперь у каждой доски свои: пересобираем таблицу, чтобы имя было уникально в пределах доски.
    # (внешние ключи на время миграции выключены — см. migrate)
    _script(conn, """
        CREATE TABLE options_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER REFERENCES boards(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('priority', 'category', 'resolution', 'server')),
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE (board_id, kind, name)
        );
        INSERT INTO options_new (id, board_id, kind, name, position, is_active, created_at)
            SELECT id, 1, kind, name, position, is_active, created_at FROM options;
        DROP TABLE options;
        ALTER TABLE options_new RENAME TO options;
        CREATE INDEX idx_options_board ON options(board_id, kind, position)
    """)

    seed_fields(conn, 1, [(key, key == "category_id") for key in C.BUILTIN_FIELDS], [])
    conn.execute("INSERT INTO board_members (board_id, user_id, role, created_at) "
                 "SELECT 1, id, role, ? FROM users WHERE role IN ('tester', 'developer')", (now,))

    s = C.SEED_FEATURE_BOARD
    feature = seed_board(conn, s["name"], s["description"], s["columns"], s["options"], s["builtin"], s["custom"])
    conn.execute("INSERT INTO board_members (board_id, user_id, role, created_at) "
                 "SELECT ?, user_id, role, ? FROM board_members WHERE board_id = 1", (feature, now))


# Новые миграции добавляются в конец списка. Старые никогда не меняются.
MIGRATIONS = [_migration_1, _migration_2, _migration_3, _migration_4]


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
    # Пересборка таблиц (миграция №4) требует выключенных внешних ключей; их целостность
    # проверяется перед COMMIT через foreign_key_check.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for number in range(current + 1, target + 1):
            before = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            conn.execute("BEGIN")
            try:
                MIGRATIONS[number - 1](conn)
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if len(broken) > before:
                    raise RuntimeError(f"Миграция №{number}: нарушены связи в таблицах {sorted({r[0] for r in broken})}")
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
