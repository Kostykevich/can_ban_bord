r"""Резервная копия базы в instance\\backups. Хранит последние 30 копий каждого вида.

Запуск вручную:
    .\.venv\Scripts\python.exe scripts\backup_db.py
Копия перед обновлением (её делает deploy\update.ps1, хранится отдельно от ежедневных):
    .\.venv\Scripts\python.exe scripts\backup_db.py update

На VDS добавьте этот запуск в Планировщик заданий Windows (раз в день), см. DEPLOY.md.
Копия делается безопасно, даже когда сайт работает.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from vektor.db import backup_file  # noqa: E402

KEEP = 30
KINDS = ("daily", "update")


def main(kind="daily"):
    if kind not in KINDS:
        print(f"Неизвестный вид копии: {kind}. Допустимо: {', '.join(KINDS)}")
        return 2
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(base, ".env"))
    db_path = os.environ.get("VEKTOR_DB_PATH") or os.path.join(base, "instance", "kanban.db")
    if not os.path.exists(db_path):
        print(f"База не найдена: {db_path}")
        return 1
    path = backup_file(db_path, kind)
    print(f"Резервная копия: {path}")

    copies = sorted(glob.glob(os.path.join(os.path.dirname(path), f"kanban-{kind}-*.db")))
    for old in copies[:-KEEP]:
        os.remove(old)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
