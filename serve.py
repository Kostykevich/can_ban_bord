r"""Запуск в PRODUCTION (на VDS) через Waitress.

    .\.venv\Scripts\python.exe serve.py

По умолчанию слушает 127.0.0.1:8000 — снаружи недоступно напрямую.
Доступ из интернета — только через reverse proxy с HTTPS (Caddy), см. DEPLOY.md.
"""

import os

from waitress import serve

from vektor import create_app, start_background

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    start_background(app)
    print(f"[Vektor] Сервер запущен: http://{host}:{port}  (остановить: Ctrl + C)")
    serve(app, host=host, port=port, threads=8, ident="vektor")
