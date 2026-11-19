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
    "board_name": 40,
    "board_description": 200,
    "field_label": 50,
    "field_hint": 120,
}

# ---------------------------------------------------------------- доски (подразделения)

# Роль участника на конкретной доске. Администратор сайта может всё на любой доске.
BOARD_ROLES = {
    "lead": "Руководитель",
    "developer": "Разработчик",
    "tester": "Тестер",
}

# Встроенные поля карточки: хранятся в колонках таблицы cards.
# Название, описание, приоритет, теги и исполнитель есть всегда.
# Остальные включаются и переименовываются в настройках каждой доски.
BUILTIN_FIELDS = {
    "steps": {"label": "Шаги воспроизведения", "type": "textarea"},
    "expected": {"label": "Ожидаемый результат", "type": "textarea"},
    "actual": {"label": "Фактический результат", "type": "textarea"},
    "category_id": {"label": "Категория", "type": "option", "option_kind": "category"},
    "server_id": {"label": "Сервер", "type": "option", "option_kind": "server"},
    "frequency": {"label": "Как часто повторяется", "type": "frequency"},
    "build_version": {"label": "Версия сборки", "type": "text"},
    "coords": {"label": "Координаты", "type": "text"},
    "static_id": {"label": "Static ID персонажа", "type": "text"},
    "occurred_at": {"label": "Когда произошло (МСК)", "type": "datetime"},
    "video_url": {"label": "Ссылка на видео", "type": "url"},
}

# Типы полей, которые администратор создаёт сам (значения — в таблице card_values)
CUSTOM_FIELD_TYPES = {
    "text": "Строка",
    "textarea": "Большой текст",
    "number": "Число",
    "url": "Ссылка",
    "select": "Выбор из списка",
    "datetime": "Дата и время",
    "checkbox": "Галочка (да/нет)",
}
CUSTOM_LIMITS = {"text": 300, "textarea": 5000, "url": 500, "number": 30, "datetime": 30, "select": 60, "checkbox": 1}
MAX_FIELDS_PER_BOARD = 40
MAX_CHOICES = 30

# Вторая доска, которая создаётся при обновлении
SEED_FEATURE_BOARD = {
    "name": "Новые системы",
    "description": "Идеи и разработка новых игровых систем.",
    "columns": [("Идеи", "backlog", "developer"), ("В разработке", "work", "developer,tester"),
                ("Тестирование", "review", "developer"), ("Готово", "done", "tester")],
    "options": {
        "priority": ["Критический", "Высокий", "Средний", "Низкий"],
        "category": SEED_OPTIONS["category"],
        "resolution": ["Реализовано", "Отложено", "Отменено"],
        "server": SEED_OPTIONS["server"],
    },
    "builtin": [("category_id", True), ("video_url", False)],
    "custom": [
        ("Зачем нужна система", "textarea", "", False),
        ("Как должна работать", "textarea", "", False),
        ("Сложность", "select", "Небольшая\nСредняя\nБольшая", True),
        ("Ссылка на ТЗ или документ", "url", "", False),
    ],
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_CARD = 10
POLL_SECONDS = 20
