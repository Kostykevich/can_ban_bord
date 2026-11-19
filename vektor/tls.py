"""Проверка HTTPS-сертификатов для запросов к Telegram и Discord.

На Windows Python берёт корневые сертификаты из хранилища системы. На свежем Windows Server
там часто нет нужных корней (например, GoDaddy у Telegram): Windows докачивает их, только когда
сайт открывает сама система. Тогда Python пишет «self-signed certificate in certificate chain».

Поэтому к системным корням добавляем стандартный набор certifi (тот же, что в Firefox).
Проверка сертификата остаётся включённой — просто доверенных корней становится больше.
"""

import ssl
import urllib.request

_context = None


def context():
    global _context
    if _context is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except (ImportError, OSError):
            pass  # без certifi остаются системные корни
        _context = ctx
    return _context


def opener(proxy=""):
    handlers = [urllib.request.HTTPSHandler(context=context())]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    return urllib.request.build_opener(*handlers)
