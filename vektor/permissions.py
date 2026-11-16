"""Правила доступа. Всё проверяется на сервере; интерфейс лишь прячет недоступные кнопки.

Правила по умолчанию:
- Администратор может всё.
- Переносить карточку в колонку может роль, отмеченная в настройках этой колонки.
- Возврат из «Проверки»/«Завершения» в «Входящие»/«В работе» — только с комментарием
  (причина возврата), счётчик возвратов увеличивается.
- Перенос в колонку типа «Завершение» требует резолюции (Исправлено, Дубликат…).
- Текст карточки редактирует автор и администратор.
- Приоритет, категорию и теги меняют автор, разработчики и администратор.
- Теги может добавлять любой участник команды.
- Разработчик может взять задачу себе или снять себя; назначать других — только админ.
"""

from .constants import WORK_ROLES

CONTENT_FIELDS = {
    "title", "description", "steps", "expected", "actual", "build_version",
    "coords", "static_id", "occurred_at", "frequency", "video_url", "server_id",
}
META_FIELDS = {"priority_id", "category_id", "tags"}


def is_admin(user):
    return user is not None and user["role"] == "admin"


def is_worker(user):
    return user is not None and user["is_active"] and user["role"] in WORK_ROLES


def can_enter_column(user, column):
    if is_admin(user):
        return True
    return is_worker(user) and user["role"] in column["move_roles"]


def transition_rules(from_col, to_col):
    """Что нужно для переноса: комментарий и/или резолюция."""
    is_return = from_col["kind"] in ("review", "done") and to_col["kind"] in ("backlog", "work")
    return {
        "needs_comment": is_return,
        "needs_resolution": to_col["kind"] == "done",
        "is_return": is_return,
    }


def editable_fields(user, card_author_id):
    if is_admin(user):
        return CONTENT_FIELDS | META_FIELDS
    if not is_worker(user):
        return set()
    fields = {"tags"}
    if card_author_id is not None and card_author_id == user["id"]:
        fields |= CONTENT_FIELDS | META_FIELDS
    if user["role"] == "developer":
        fields |= META_FIELDS
    return fields


def can_assign(user, current_assignee_id, new_assignee_id):
    if is_admin(user):
        return True
    if not is_worker(user) or user["role"] != "developer":
        return False
    if new_assignee_id == user["id"]:
        return current_assignee_id in (None, user["id"])  # чужую задачу не забираем
    if new_assignee_id is None:
        return current_assignee_id == user["id"]  # снять можно только себя
    return False


def can_delete_attachment(user, uploader_id):
    return is_admin(user) or (is_worker(user) and uploader_id == user["id"])
