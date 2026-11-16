r"""Запуск для ЛОКАЛЬНОЙ разработки: http://127.0.0.1:5000

    .\.venv\Scripts\python.exe app.py

Это встроенный сервер Flask — только для вашего компьютера.
На VDS используйте serve.py (Waitress).
"""

import os

from vektor import create_app, start_background

app = create_app()

if __name__ == "__main__":
    # При debug Flask перезапускает процесс при изменении кода.
    # Фоновые потоки (боты) запускаем только в «рабочем» процессе, чтобы они не задвоились.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_background(app)
    app.run(host="127.0.0.1", port=5000, debug=True)
