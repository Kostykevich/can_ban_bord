r"""Диагностика Discord: связь, ключи OAuth, redirect URI, токен бота, канал.

Запуск из папки проекта (на VDS или на ПК):
    .\.venv\Scripts\python.exe scripts\check_discord.py

Секреты на экран не выводятся — только «задан / пусто» и результат проверки.
"""

import base64
import json
import os
import socket
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE, ".env"))

from vektor.discord_api import API, DiscordError, _http, _opener, _proxy_url  # noqa: E402

OK, BAD, WARN = "  [OK]  ", "  [!!]  ", "  [?]   "
problems = 0


def env(name):
    return os.environ.get(name, "").strip()


def bad(text):
    global problems
    problems += 1
    print(BAD + text)


print("\n=== 1. Настройки в .env ===")
for name in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI", "DISCORD_BOT_TOKEN",
             "DISCORD_CHANNEL_ID", "PUBLIC_URL"):
    value = env(name)
    shown = value if name in ("DISCORD_REDIRECT_URI", "PUBLIC_URL", "DISCORD_CLIENT_ID", "DISCORD_CHANNEL_ID") else "задан"
    print((OK if value else WARN) + f"{name}: {shown if value else 'пусто'}")
proxy = _proxy_url()
print((OK if proxy else WARN) + f"Прокси: {urllib.parse.urlsplit(proxy).hostname + ':' + str(urllib.parse.urlsplit(proxy).port) if proxy else 'не задан (DISCORD_PROXY)'}")

print("\n=== 2. Redirect URI ===")
redirect, public = env("DISCORD_REDIRECT_URI"), env("PUBLIC_URL").rstrip("/")
if env("DISCORD_CLIENT_ID"):
    if not redirect:
        bad("DISCORD_REDIRECT_URI пуст")
    else:
        if not redirect.endswith("/auth/discord/callback"):
            bad("должен заканчиваться на /auth/discord/callback")
        if public and not redirect.startswith(public + "/"):
            bad(f"не начинается с PUBLIC_URL ({public}) — домен или http/https не совпадают")
        if env("HTTPS") in ("1", "true", "yes") and not redirect.startswith("https://"):
            bad("HTTPS=1, а redirect начинается с http:// — нужен https://")
        if problems == 0:
            print(OK + "формат правильный")
        print(WARN + "Проверьте вручную: Developer Portal → OAuth2 → Redirects содержит ТОЧНО:")
        print("         " + redirect)
else:
    print(WARN + "вход через Discord не настроен — пропуск")

print("\n=== 3. Связь с Discord ===")
for host in ("discord.com", "gateway.discord.gg"):
    if proxy:
        print(WARN + f"{host}: прямую связь не проверяем — используется прокси")
        continue
    try:
        socket.create_connection((host, 443), timeout=8).close()
        print(OK + f"{host}:443 доступен")
    except OSError as err:
        bad(f"{host}:443 НЕДОСТУПЕН ({err.__class__.__name__}). Хостинг или провайдер блокирует Discord.")
try:
    req = urllib.request.Request(f"{API}/gateway", headers={"User-Agent": "DiscordBot (https://localhost, 1.0)"})
    with _opener().open(req, timeout=15) as resp:
        print(OK + f"HTTPS-запрос к API Discord: {resp.status}" + (" (через прокси)" if proxy else ""))
except Exception as err:
    bad(f"HTTPS-запрос к API Discord не прошёл: {getattr(err, 'reason', err)}")
    print("         → Discord заблокирован для этого сервера. Варианты: другой хостинг (не РФ)")
    print("           или HTTP-прокси в .env: DISCORD_PROXY=http://логин:пароль@адрес:порт")

print("\n=== 4. Ключи приложения (Client ID + Secret) ===")
if env("DISCORD_CLIENT_ID") and env("DISCORD_CLIENT_SECRET"):
    cred = base64.b64encode(f"{env('DISCORD_CLIENT_ID')}:{env('DISCORD_CLIENT_SECRET')}".encode()).decode()
    try:
        _http("POST", f"{API}/oauth2/token", {"grant_type": "client_credentials", "scope": "identify"},
              headers={"Authorization": f"Basic {cred}"}, form=True)
        print(OK + "Discord принял Client ID и Client Secret")
    except DiscordError as err:
        if err.kind == "auth":
            bad("Discord ОТКЛОНИЛ ключи: неверный Client ID или Client Secret.")
            print("         → Developer Portal → OAuth2 → Reset Secret, вставьте новый в .env без пробелов.")
        elif err.kind == "network":
            bad(f"нет связи: {err}")
        else:
            print(WARN + f"ответ Discord: {err}")
else:
    print(WARN + "Client ID или Secret пусты — пропуск")

print("\n=== 5. Бот ===")
token = env("DISCORD_BOT_TOKEN")
if token:
    try:
        me = _http("GET", f"{API}/users/@me", headers={"Authorization": f"Bot {token}"})
        print(OK + f"токен рабочий, бот: {me.get('username')}")
        channel = env("DISCORD_CHANNEL_ID")
        if channel:
            try:
                ch = _http("GET", f"{API}/channels/{channel}", headers={"Authorization": f"Bot {token}"})
                print(OK + f"канал найден: #{ch.get('name')}")
            except DiscordError as err:
                bad(f"бот не видит канал {channel} ({err.status}): пригласите бота на сервер "
                    "и дайте права View Channel + Send Messages")
    except DiscordError as err:
        if err.kind == "auth":
            bad("токен бота неверный. Developer Portal → Bot → Reset Token → обновите .env")
        else:
            bad(f"проверка бота не прошла: {err}")
else:
    print(WARN + "DISCORD_BOT_TOKEN пуст — бот выключен")

print("\n" + ("Проблем не найдено. Если вход всё равно не работает — перезапустите сайт и пришлите "
              "строку [Discord] Ошибка входа из instance\\logs\\server.log (без токенов)."
              if problems == 0 else f"Найдено проблем: {problems}. Исправьте их и перезапустите сайт."))
print("Перезапуск на VDS: Stop-ScheduledTask -TaskName 'Vektor Kanban'; Start-ScheduledTask -TaskName 'Vektor Kanban'\n")
sys.exit(1 if problems else 0)
