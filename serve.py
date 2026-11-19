r"""Запуск в PRODUCTION (на VDS) через Waitress.

    .\.venv\Scripts\python.exe serve.py

По умолчанию слушает 127.0.0.1:8000 — снаружи недоступно напрямую.
Доступ из интернета — только через reverse proxy с HTTPS (Caddy), см. DEPLOY.md.

TRUST_PROXY=1: Waitress доверяет заголовкам X-Forwarded-* от Caddy (127.0.0.1).
Без этого Waitress их выбрасывает, и в журнале действий у всех IP 127.0.0.1.
"""

import os

from waitress import serve

from vektor import create_app, start_background

app = create_app()


def proxy_options():
    if os.environ.get("TRUST_PROXY", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return {}
    return {
        "trusted_proxy": "127.0.0.1",
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": "x-forwarded-for x-forwarded-proto x-forwarded-host",
        "clear_untrusted_proxy_headers": True,
    }


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    start_background(app)
    extra = proxy_options()
    print(f"[Vektor] Сервер запущен: http://{host}:{port}  (остановить: Ctrl + C)"
          + ("  · IP посетителей берётся из заголовков Caddy" if extra else ""))
    serve(app, host=host, port=port, threads=8, ident="vektor", **extra)
