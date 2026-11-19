r"""Диагностика Telegram: связь, сертификат, прокси, токен бота.

Запуск из папки проекта (на VDS или на ПК):
    .\.venv\Scripts\python.exe scripts\check_telegram.py

Токен на экран не выводится. Если сертификат api.telegram.org подменён
(антивирус или фильтр хостинга), скрипт покажет, кто его выдал.
"""

import json
import os
import socket
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE, ".env"))

from vektor.telegram_bot import _opener, proxy_url  # noqa: E402

HOST = "api.telegram.org"
OK, BAD, WARN = "  [OK]  ", "  [!!]  ", "  [?]   "
problems = 0


def bad(text):
    global problems
    problems += 1
    print(BAD + text)


def name_of(rdn):
    """('CN', 'x') из структуры сертификата -> 'CN=x, O=y'."""
    return ", ".join(f"{k}={v}" for part in rdn for k, v in part)


def read_cert_unverified(host):
    """Сертификат, который реально видит сервер (без проверки — только чтобы узнать, кто его выдал).
    Никакие данные, кроме рукопожатия TLS, не отправляются."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(pem)
        return ssl._ssl._test_decode_cert(path)  # noqa: SLF001 — стандартный разбор сертификата CPython
    finally:
        os.remove(path)


token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
proxy = proxy_url()

print("\n=== 1. Настройки в .env ===")
print((OK if token else BAD) + ("TELEGRAM_BOT_TOKEN задан" if token else "TELEGRAM_BOT_TOKEN пустой — бот выключен"))
if not token:
    problems += 1
print(OK + (f"Прокси: {proxy.split('@')[-1]}" if proxy else "Прокси не задан (TELEGRAM_PROXY пуст)"))

print("\n=== 2. Связь с api.telegram.org напрямую ===")
try:
    ips = sorted({a[4][0] for a in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)})
    print(OK + f"DNS: {', '.join(ips)}")
except OSError as err:
    bad(f"DNS не находит {HOST}: {err}")
    ips = []

if ips:
    try:
        cert = read_cert_unverified(HOST)
        issuer, subject = name_of(cert.get("issuer", ())), name_of(cert.get("subject", ()))
        print(f"        Сертификат выдан для: {subject}")
        print(f"        Кем выдан:            {issuer}")
        genuine = any(x in issuer for x in ("Go Daddy", "GoDaddy", "Starfield", "DigiCert", "Let's Encrypt", "GlobalSign"))
        try:
            with socket.create_connection((HOST, 443), timeout=10) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=HOST):
                    pass
            print(OK + "Проверка сертификата прошла — соединение не перехватывается")
        except ssl.SSLCertVerificationError as err:
            bad(f"Сертификат НЕ прошёл проверку: {err.verify_message}")
            low = issuer.lower()
            if any(x in low for x in ("kaspersky", "eset", "avast", "avg", "dr.web", "drweb", "bitdefender", "norton", "fortinet", "sophos")):
                print("        → Соединение перехватывает АНТИВИРУС на этом сервере. Отключите в нём проверку")
                print("          HTTPS/SSL (или добавьте python.exe в исключения) и перезапустите сайт.")
            elif not genuine:
                print("        → Сертификат подменяет кто-то по пути: антивирус/файрвол на сервере или фильтр хостинга.")
                print("          1) Проверьте, нет ли на VDS антивируса с «проверкой HTTPS» — отключите её.")
                print("          2) Если антивируса нет — это фильтр хостинга: укажите TELEGRAM_PROXY (прокси вне этой сети)")
                print("             или спросите поддержку хостинга, почему подменяется сертификат api.telegram.org.")
    except OSError as err:
        bad(f"Нет соединения с {HOST}:443 — {err}")

print("\n=== 3. Запрос к боту (getMe)" + (" через прокси" if proxy else "") + " ===")
if token:
    req = urllib.request.Request(f"https://{HOST}/bot{token}/getMe", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener().open(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("ok"):
            print(OK + f"Бот @{body['result'].get('username')} отвечает — Telegram работает")
        else:
            bad(f"Telegram ответил ошибкой: {body.get('description')}")
    except urllib.error.HTTPError as err:
        if err.code in (401, 404):
            bad("Telegram не принял токен — получите новый у @BotFather и вставьте в .env")
        else:
            bad(f"Telegram вернул ошибку {err.code}")
    except urllib.error.URLError as err:
        bad(f"Не удалось связаться: {err.reason}")
    except OSError as err:
        bad(f"Не удалось связаться: {err}")
else:
    print(WARN + "Пропущено: нет токена")

print()
if problems:
    print(f"Найдено проблем: {problems}. После исправления перезапустите сайт (задача «Vektor Kanban»).")
else:
    print("Всё в порядке. Если сайт всё ещё пишет об ошибке — перезапустите его.")
