"""Фиксированные справочники и ограничения.

Колонки, приоритеты, категории, резолюции и серверы НЕ здесь:
они хранятся в базе и настраиваются администратором (страница «Настройки»).
Ниже — только начальные значения, которыми заполняется новая база.
"""

ROLES = {
    "pending": "Ожидает доступа",
    "tester": "Тестер",
    "developer": "Разработчик",
    "admin": "Администратор",
}
WORK_ROLES = ["tester", "developer", "admin"]

# Тип колонки определяет её поведение
COLUMN_KINDS = {
    "backlog": "Входящие — сюда попадают новые карточки",
    "work": "В работе — разработчик назначается исполнителем автоматически",
    "review": "Проверка — в Discord уходит уведомление «на проверке»",
    "done": "Завершение — нужна резолюция, карточка считается закрытой",
}

# Типы настраиваемых списков
OPTION_KINDS = {
    "priority": "Приоритеты",
    "category": "Категории",
    "resolution": "Резолюции (причины закрытия)",
    "server": "Серверы",
}

FREQUENCIES = {
    "always": "Всегда",
    "sometimes": "Иногда",
    "once": "Один раз",
    "unknown": "Неизвестно",
}

# Начальные данные для новой базы
SEED_COLUMNS = [
    # (название, тип, роли, которые могут переносить В эту колонку)
    ("Новые", "backlog", "developer"),
    ("В работе", "work", "developer,tester"),
    ("На проверке", "review", "developer"),
    ("Закрыто", "done", "tester"),
]
# Соответствие статусов старого прототипа колонкам выше
LEGACY_STATUS_TO_KIND = {
    "new": "backlog",
    "in_progress": "work",
    "testing": "review",
    "done": "done",
}

# Приоритеты: сверху — самый важный
SEED_OPTIONS = {
    "priority": ["Критический", "Высокий", "Средний", "Низкий"],
    "category": [
        "Интерфейс",
        "Игровая механика",
        "Транспорт",
        "Работа и фракции",
        "Экономика",
        "Авторизация",
        "Прочее",
    ],
    "resolution": ["Исправлено", "Дубликат", "Не воспроизводится", "Не баг"],
    "server": ["Тестовый", "Основной"],
}
DEFAULT_PRIORITY = "Средний"
DEFAULT_CATEGORY = "Прочее"
DEFAULT_SERVER = "Тестовый"

DEFAULT_SETTINGS = {
    "board_title": "Доска задач",
    "board_subtitle": "Баги и задачи сервера. Нашли проблему — создайте карточку.",
    "done_visible_days": "14",
}

# Ограничения длины полей (символов)
LIMITS = {
    "title": 150,
    "description": 5000,
    "steps": 5000,
    "expected": 2000,
    "actual": 2000,
    "build_version": 50,
    "coords": 100,
    "static_id": 30,
    "occurred_at": 30,
    "video_url": 500,
    "comment": 3000,
    "display_name": 50,
    "username": 32,
    "option_name": 40,
    "column_name": 40,
    "board_title": 60,
    "board_subtitle": 200,
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_CARD = 10
POLL_SECONDS = 20
