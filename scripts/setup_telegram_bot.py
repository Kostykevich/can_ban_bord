r"""Оформление Telegram-бота: имя, описание, меню команд и аватар.

Запуск из папки проекта (один раз, или после смены токена/логотипа):
    .\.venv\Scripts\python.exe scripts\setup_telegram_bot.py

Имя, описания и команды бот применяет и сам при каждом запуске сайта.
Аватар ставится только этим скриптом: static/img/telegram_avatar.png.
Токен на экран не выводится.
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE, ".env"))

from vektor.telegram_bot import PROFILE, TelegramBot, TelegramError  # noqa: E402

AVATAR = os.path.join(BASE, "static", "img", "telegram_avatar.png")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("  [!!] В .env пустой TELEGRAM_BOT_TOKEN")
        return 1
    bot = TelegramBot(token, "")
    try:
        me = bot._call("getMe")
    except TelegramError as err:
        print(f"  [!!] {err}\n       Проверка связи: scripts\\check_telegram.py")
        return 1
    print(f"  Бот: @{me['username']}")

    ok = bot.apply_profile()
    print(("  [OK]  " if ok else "  [!!]  ") + f"Имя «{PROFILE['name']}», описания и меню команд")

    try:
        with open(AVATAR, "rb") as f:
            bot.set_avatar(f.read())
        print("  [OK]  Аватар установлен")
    except (OSError, TelegramError) as err:
        print(f"  [?]   Аватар не установлен автоматически: {err}\n"
              f"        Поставьте вручную: @BotFather -> /setuserpic -> @{me['username']} -> файл\n"
              f"        {AVATAR}")
    print(f"\n  Готово: https://t.me/{me['username']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
