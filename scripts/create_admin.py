r"""Создаёт администратора (или делает существующего пользователя админом).

Запуск из папки проекта:
    .\.venv\Scripts\python.exe scripts\create_admin.py

Пароль вводится скрыто и нигде не сохраняется в открытом виде.
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from vektor import create_app  # noqa: E402
from vektor.auth import MIN_PASSWORD, USERNAME_RE  # noqa: E402
from vektor.db import connect, utcnow  # noqa: E402
from vektor.notify import DEFAULT_EVENTS  # noqa: E402


def main():
    app = create_app()  # применит миграции, если нужно
    conn = connect(app.config["DATABASE"])
    print(f"База: {app.config['DATABASE']}\n")

    username = input("Логин администратора (латиница, 2–32 символа): ").strip()
    if not USERNAME_RE.match(username):
        print("Неверный логин.")
        return 1
    existing = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        answer = input(f"Пользователь «{existing['display_name']}» уже есть. Сделать администратором "
                       "и задать новый пароль? (да/нет): ").strip().lower()
        if answer not in ("да", "д", "yes", "y"):
            print("Отменено.")
            return 0

    display = input("Отображаемое имя (Enter — как логин): ").strip()[:50] or username
    while True:
        password = getpass.getpass(f"Пароль (от {MIN_PASSWORD} символов, ввод не отображается): ")
        if len(password) < MIN_PASSWORD:
            print("Слишком короткий пароль.")
            continue
        if password != getpass.getpass("Повторите пароль: "):
            print("Пароли не совпадают.")
            continue
        break

    with conn:
        if existing:
            conn.execute("UPDATE users SET role = 'admin', is_active = 1, display_name = ?, password_hash = ? "
                         "WHERE id = ?", (display, generate_password_hash(password), existing["id"]))
        else:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role, notify_events, created_at) "
                "VALUES (?, ?, ?, 'admin', ?, ?)",
                (username, display, generate_password_hash(password), DEFAULT_EVENTS["admin"], utcnow()),
            )
    conn.close()
    print(f"\nГотово! Войдите на сайт с логином «{username}».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
