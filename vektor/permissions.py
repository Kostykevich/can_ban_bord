"""Правила доступа. Всё проверяется на сервере; интерфейс лишь прячет недоступные кнопки.

Права зависят от роли человека на конкретной доске (models.board_role):
- admin — администратор сайта: всё на любой доске;
- lead — руководитель доски: всё на своей доске (настройки, участники, архив, назначение);
- developer, tester — участники; что им можно, задают настройки колонок;
- None — не участник: доску не видит.

Правила по умолчанию:
- Переносить карточку в колонку может роль, отмеченная в настройках этой колонки.
- Возврат из «Проверки»/«Завершения» во «Входящие»/«В работе» — только с комментарием
  (причина возврата), счётчик возвратов увеличивается.
- Перенос в колонку типа «Завершение» требует резолюции (Исправлено, Дубликат…).
- Текст карточки и свои поля доски редактирует автор, руководитель и администратор.
- Приоритет, категорию и теги меняют автор, разработчики, руководитель и администратор.
- Теги может добавлять любой участник доски.
- Разработчик может взять задачу себе или снять себя; назначать других — руководитель и админ.
"""

CONTENT_FIELDS = {
    "title", "description", "steps", "expected", "actual", "build_version",
    "coords", "static_id", "occurred_at", "frequency", "video_url", "server_id",
}
META_FIELDS = {"priority_id", "category_id", "tags"}
CUSTOM_PREFIX = "f"  # ключи своих полей доски: f12, f13…


def is_admin(user):
    return user is not None and user["role"] == "admin"


def is_member(role):
    return role is not None


def is_manager(role):
    """Администратор сайта или руководитель этой доски."""
    return role in ("admin", "lead")


def can_enter_column(role, column):
    if is_manager(role):
        return True
    return role in column["move_roles"]


def transition_rules(from_col, to_col):
    """Что нужно для переноса: комментарий и/или резолюция."""
    is_return = from_col["kind"] in ("review", "done") and to_col["kind"] in ("backlog", "work")
    return {
        "needs_comment": is_return,
        "needs_resolution": to_col["kind"] == "done",
        "is_return": is_return,
    }


def editable_fields(role, user_id, card_author_id, custom_keys=()):
    """Какие поля карточки может менять человек. custom_keys — ключи своих полей доски."""
    content = CONTENT_FIELDS | set(custom_keys)
    if is_manager(role):
        return content | META_FIELDS
    if not is_member(role):
        return set()
    fields = {"tags"}
    if card_author_id is not None and card_author_id == user_id:
        fields |= content | META_FIELDS
    if role == "developer":
        fields |= META_FIELDS
    return fields


def can_assign(role, user_id, current_assignee_id, new_assignee_id):
    if is_manager(role):
        return True
    if role != "developer":
        return False
    if new_assignee_id == user_id:
        return current_assignee_id in (None, user_id)  # чужую задачу не забираем
    if new_assignee_id is None:
        return current_assignee_id == user_id  # снять можно только себя
    return False


def can_delete_attachment(role, user_id, uploader_id):
    return is_manager(role) or (is_member(role) and uploader_id == user_id)
