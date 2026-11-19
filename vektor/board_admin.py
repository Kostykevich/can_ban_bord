"""Настройки досок: сами доски (только администратор), а также колонки, списки значений,
поля карточек и участники конкретной доски (администратор или руководитель этой доски)."""

from functools import wraps

from flask import Blueprint, g, jsonify

from . import audit as A
from . import models as M
from . import permissions as P
from .api import body, clean_text, fail
from .constants import (BOARD_ROLES, BUILTIN_FIELDS, COLUMN_KINDS, CUSTOM_FIELD_TYPES, LIMITS,
                        MAX_CHOICES, MAX_FIELDS_PER_BOARD, OPTION_KINDS, SEED_OPTIONS)
from .db import get_db, seed_board, utcnow

bp = Blueprint("boards", __name__, url_prefix="/api/boards")


def _changed(db):
    M.bump_config(db)
    db.commit()


def manager_required(view):
    """Доступ к настройкам доски: администратор сайта или руководитель этой доски."""
    @wraps(view)
    def wrapper(board_id, *args, **kwargs):
        db = get_db()
        board = M.get_board(db, board_id)
        if board is None:
            return fail("Доска не найдена.", 404)
        if not P.is_manager(M.board_role(db, g.user, board_id)):
            return fail("Настраивать доску может только её руководитель или администратор.", 403)
        g.board = board
        g.audit_board_id = board_id
        return view(board_id, *args, **kwargs)
    return wrapper


def admin_only():
    if not P.is_admin(g.user):
        return fail("Создавать и удалять доски может только администратор.", 403)
    return None


def _reorder(db, table, item_id, direction, where, params):
    """Меняет местами элемент с соседом сверху (-1) или снизу (+1) в пределах where."""
    if direction not in (-1, 1):
        return fail("Неверное направление.")
    ids = [r["id"] for r in db.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY position, id", params)]
    if item_id not in ids:
        return fail("Не найдено.", 404)
    i = ids.index(item_id)
    j = i + direction
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, rid in enumerate(ids):
            db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, rid))
        _changed(db)
    return None


# ---------------------------------------------------------------- доски

def _boards_payload(db):
    boards = M.boards_for(db, g.user)
    for b in boards:
        r = db.execute(
            "SELECT (SELECT COUNT(*) FROM cards WHERE board_id = :b) AS cards,"
            " (SELECT COUNT(*) FROM board_members WHERE board_id = :b) AS members",
            {"b": b["id"]},
        ).fetchone()
        b["card_count"], b["member_count"] = r["cards"], r["members"]
    return jsonify(boards=boards)


def _board_values(data, partial=False):
    values, errors = {}, {}
    if "name" in data or not partial:
        name = clean_text(data.get("name"), LIMITS["board_name"])
        if not name or len(name) > LIMITS["board_name"]:
            errors["name"] = f"От 1 до {LIMITS['board_name']} символов."
        values["name"] = name
    if "description" in data:
        desc = clean_text(data.get("description"), LIMITS["board_description"])
        if len(desc) > LIMITS["board_description"]:
            errors["description"] = f"До {LIMITS['board_description']} символов."
        values["description"] = desc
    return values, errors


@bp.get("/manage")
def boards_manage():
    return _boards_payload(get_db())


@bp.post("")
def board_create():
    denied = admin_only()
    if denied:
        return denied
    db = get_db()
    values, errors = _board_values(body())
    if errors:
        return fail("Проверьте поля.", 400, errors)
    board_id = seed_board(
        db, values["name"], values.get("description", ""),
        [("Новые", "backlog", "developer,tester"), ("В работе", "work", "developer"),
         ("На проверке", "review", "developer"), ("Готово", "done", "tester")],
        {"priority": SEED_OPTIONS["priority"], "category": ["Прочее"], "resolution": ["Сделано", "Отменено"],
         "server": SEED_OPTIONS["server"]},
        [("category_id", True)], [],
    )
    g.audit_board_id = board_id
    _changed(db)
    A.details(f"«{values['name']}»")
    resp = _boards_payload(db)
    resp.status_code = 201
    return resp


@bp.patch("/<int:board_id>")
@manager_required
def board_update(board_id):
    db = get_db()
    values, errors = _board_values(body(), partial=True)
    if errors:
        return fail("Проверьте поля.", 400, errors)
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE boards SET {sets} WHERE id = ?", (*values.values(), board_id))
        _changed(db)
    A.details(f"«{g.board['name']}»" + (f" → «{values['name']}»" if values.get("name") != g.board["name"]
                                         and values.get("name") else ""))
    return _boards_payload(db)


@bp.post("/<int:board_id>/move")
def board_move(board_id):
    denied = admin_only()
    if denied:
        return denied
    db = get_db()
    return _reorder(db, "boards", board_id, body().get("direction"), "1 = 1", ()) or _boards_payload(db)


@bp.delete("/<int:board_id>")
def board_delete(board_id):
    denied = admin_only()
    if denied:
        return denied
    db = get_db()
    board = M.get_board(db, board_id)
    if board is None:
        return fail("Доска не найдена.", 404)
    if db.execute("SELECT COUNT(*) FROM boards").fetchone()[0] <= 1:
        return fail("Нельзя удалить последнюю доску.", 409)
    count = db.execute("SELECT COUNT(*) FROM cards WHERE board_id = ?", (board_id,)).fetchone()[0]
    if count:
        return fail(f"На доске {count} карточек. Удалить можно только пустую доску: "
                    "перенесите карточки или удалите их из архива.", 409)
    db.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    _changed(db)
    A.details(f"«{board['name']}»")
    return _boards_payload(db)


# ---------------------------------------------------------------- колонки

def _column_payload(data, current=None):
    errors = {}
    values = {}
    if "name" in data or current is None:
        name = clean_text(data.get("name"), LIMITS["column_name"])
        if not name or len(name) > LIMITS["column_name"]:
            errors["name"] = f"От 1 до {LIMITS['column_name']} символов."
        values["name"] = name
    if "kind" in data or current is None:
        if data.get("kind") not in COLUMN_KINDS:
            errors["kind"] = "Выберите тип."
        values["kind"] = data.get("kind")
    if "move_roles" in data or current is None:
        roles = data.get("move_roles") or []
        if not isinstance(roles, list) or any(r not in ("tester", "developer") for r in roles):
            errors["move_roles"] = "Неверные роли."
        else:
            values["move_roles"] = ",".join(r for r in ("tester", "developer") if r in roles)
    if "wip_limit" in data:
        raw = data.get("wip_limit")
        if raw in (None, "", 0, "0"):
            values["wip_limit"] = None
        else:
            try:
                limit = int(raw)
                assert 1 <= limit <= 999
                values["wip_limit"] = limit
            except (TypeError, ValueError, AssertionError):
                errors["wip_limit"] = "Число от 1 до 999 или пусто."
    return values, errors


@bp.get("/<int:board_id>/columns")
@manager_required
def columns_list(board_id):
    db = get_db()
    cols = M.list_columns(db, board_id)
    for col in cols:
        col["card_count"] = db.execute("SELECT COUNT(*) FROM cards WHERE column_id = ?", (col["id"],)).fetchone()[0]
    return jsonify(columns=cols, kinds=COLUMN_KINDS)


@bp.post("/<int:board_id>/columns")
@manager_required
def column_create(board_id):
    db = get_db()
    values, errors = _column_payload(body())
    if errors:
        return fail("Проверьте поля.", 400, errors)
    pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM board_columns WHERE board_id = ?",
                     (board_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO board_columns (board_id, name, kind, move_roles, wip_limit, position, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (board_id, values["name"], values["kind"], values["move_roles"], values.get("wip_limit"), pos, utcnow()),
    )
    _changed(db)
    A.details(f"{g.board['name']}: «{values['name']}»")
    resp = columns_list(board_id)
    return resp, 201


@bp.patch("/<int:board_id>/columns/<int:column_id>")
@manager_required
def column_update(board_id, column_id):
    db = get_db()
    current = M.get_column(db, column_id, board_id)
    if current is None:
        return fail("Колонка не найдена.", 404)
    values, errors = _column_payload(body(), current)
    if errors:
        return fail("Проверьте поля.", 400, errors)
    A.details(f"{g.board['name']}: «{current['name']}»"
              + (f" → «{values['name']}»" if values.get("name") and values["name"] != current["name"] else ""))
    if values.get("kind") and values["kind"] != current["kind"]:
        if current["kind"] == "done":
            # Карточки в колонке перестанут считаться закрытыми
            db.execute("UPDATE cards SET closed_at = NULL, resolution_id = NULL WHERE column_id = ?", (column_id,))
        if values["kind"] == "done":
            db.execute("UPDATE cards SET closed_at = ? WHERE column_id = ? AND closed_at IS NULL",
                       (utcnow(), column_id))
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE board_columns SET {sets} WHERE id = ?", (*values.values(), column_id))
        _changed(db)
    return columns_list(board_id)


@bp.post("/<int:board_id>/columns/<int:column_id>/move")
@manager_required
def column_move(board_id, column_id):
    db = get_db()
    col = M.get_column(db, column_id, board_id)
    if col is None:
        return fail("Колонка не найдена.", 404)
    A.details(f"{g.board['name']}: «{col['name']}» {'влево' if body().get('direction') == -1 else 'вправо'}")
    return (_reorder(db, "board_columns", column_id, body().get("direction"), "board_id = ?", (board_id,))
            or columns_list(board_id))


@bp.delete("/<int:board_id>/columns/<int:column_id>")
@manager_required
def column_delete(board_id, column_id):
    db = get_db()
    current = M.get_column(db, column_id, board_id)
    if current is None:
        return fail("Колонка не найдена.", 404)
    if db.execute("SELECT COUNT(*) FROM board_columns WHERE board_id = ?", (board_id,)).fetchone()[0] <= 1:
        return fail("Нельзя удалить последнюю колонку доски.", 409)
    count = db.execute("SELECT COUNT(*) FROM cards WHERE column_id = ?", (column_id,)).fetchone()[0]
    target = None
    if count:
        target = M.get_column(db, body().get("target_column_id"), board_id)
        if target is None or target["id"] == column_id:
            return fail(f"В колонке {count} карточек — выберите, куда их перенести.", 400,
                        {"target_column_id": "Обязательное поле."})
        now = utcnow()
        cards = db.execute("SELECT id FROM cards WHERE column_id = ?", (column_id,)).fetchall()
        closing = "closed_at = COALESCE(closed_at, ?)" if target["kind"] == "done" else "closed_at = NULL, resolution_id = NULL"
        params = (target["id"], now, now, column_id) if target["kind"] == "done" else (target["id"], now, column_id)
        db.execute(f"UPDATE cards SET column_id = ?, {closing}, updated_at = ?, version = version + 1 "
                   "WHERE column_id = ?", params)
        for c in cards:
            M.log(db, c["id"], g.user["id"], "moved", "column", current["name"] + " (удалена)", target["name"])
    db.execute("DELETE FROM board_columns WHERE id = ?", (column_id,))
    _changed(db)
    A.details(f"{g.board['name']}: «{current['name']}»"
              + (f", {count} карточек перенесено в «{target['name']}»" if count else ""))
    return columns_list(board_id)


# ---------------------------------------------------------------- списки значений

USAGE_SQL = ("SELECT COUNT(*) FROM cards WHERE priority_id = ? OR category_id = ? OR server_id = ? "
             "OR resolution_id = ?")


@bp.get("/<int:board_id>/options")
@manager_required
def options_list(board_id):
    db = get_db()
    grouped = M.options_grouped(db, board_id)
    for items in grouped.values():
        for opt in items:
            opt["usage"] = db.execute(USAGE_SQL, (opt["id"],) * 4).fetchone()[0]
    return jsonify(options=grouped, kinds=OPTION_KINDS)


def _get_option(db, board_id, option_id):
    return db.execute("SELECT * FROM options WHERE id = ? AND board_id = ?", (option_id, board_id)).fetchone()


def _active_count(db, board_id, kind):
    return db.execute("SELECT COUNT(*) FROM options WHERE board_id = ? AND kind = ? AND is_active = 1",
                      (board_id, kind)).fetchone()[0]


@bp.post("/<int:board_id>/options")
@manager_required
def option_create(board_id):
    db = get_db()
    data = body()
    kind = data.get("kind")
    name = clean_text(data.get("name"), LIMITS["option_name"])
    if kind not in OPTION_KINDS:
        return fail("Неизвестный список.")
    if not name or len(name) > LIMITS["option_name"]:
        return fail(f"Название от 1 до {LIMITS['option_name']} символов.", 400, {"name": "Неверная длина."})
    if db.execute("SELECT 1 FROM options WHERE board_id = ? AND kind = ? AND name = ?",
                  (board_id, kind, name)).fetchone():
        return fail("Такое значение уже есть.", 409, {"name": "Уже есть."})
    pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM options WHERE board_id = ? AND kind = ?",
                     (board_id, kind)).fetchone()[0]
    db.execute("INSERT INTO options (board_id, kind, name, position, created_at) VALUES (?, ?, ?, ?, ?)",
               (board_id, kind, name, pos, utcnow()))
    _changed(db)
    A.details(f"{g.board['name']} · {OPTION_KINDS[kind]}: «{name}»")
    return options_list(board_id), 201


@bp.patch("/<int:board_id>/options/<int:option_id>")
@manager_required
def option_update(board_id, option_id):
    db = get_db()
    opt = _get_option(db, board_id, option_id)
    if opt is None:
        return fail("Значение не найдено.", 404)
    data = body()
    what = []
    if "name" in data:
        name = clean_text(data["name"], LIMITS["option_name"])
        if not name or len(name) > LIMITS["option_name"]:
            return fail("Неверная длина названия.")
        if db.execute("SELECT 1 FROM options WHERE board_id = ? AND kind = ? AND name = ? AND id != ?",
                      (board_id, opt["kind"], name, option_id)).fetchone():
            return fail("Такое значение уже есть.", 409)
        db.execute("UPDATE options SET name = ? WHERE id = ?", (name, option_id))
        if name != opt["name"]:
            what.append(f"→ «{name}»")
    if "is_active" in data:
        active = 1 if data["is_active"] else 0
        if not active and opt["is_active"] and _active_count(db, board_id, opt["kind"]) <= 1:
            return fail("Должно остаться хотя бы одно активное значение.", 409)
        db.execute("UPDATE options SET is_active = ? WHERE id = ?", (active, option_id))
        if active != opt["is_active"]:
            what.append("включено" if active else "отключено")
    _changed(db)
    A.details(f"{g.board['name']} · {OPTION_KINDS[opt['kind']]}: «{opt['name']}» " + ", ".join(what))
    return options_list(board_id)


@bp.post("/<int:board_id>/options/<int:option_id>/move")
@manager_required
def option_move(board_id, option_id):
    db = get_db()
    opt = _get_option(db, board_id, option_id)
    if opt is None:
        return fail("Значение не найдено.", 404)
    A.details(f"{g.board['name']} · {OPTION_KINDS[opt['kind']]}: «{opt['name']}»")
    return (_reorder(db, "options", option_id, body().get("direction"), "board_id = ? AND kind = ?",
                     (board_id, opt["kind"])) or options_list(board_id))


@bp.delete("/<int:board_id>/options/<int:option_id>")
@manager_required
def option_delete(board_id, option_id):
    db = get_db()
    opt = _get_option(db, board_id, option_id)
    if opt is None:
        return fail("Значение не найдено.", 404)
    used = db.execute(USAGE_SQL, (option_id,) * 4).fetchone()[0]
    if used:
        return fail(f"Используется в {used} карточках. Отключите значение — оно пропадёт из форм, "
                    "но останется у старых карточек.", 409)
    if opt["is_active"] and _active_count(db, board_id, opt["kind"]) <= 1:
        return fail("Должно остаться хотя бы одно активное значение.", 409)
    db.execute("DELETE FROM options WHERE id = ?", (option_id,))
    _changed(db)
    A.details(f"{g.board['name']} · {OPTION_KINDS[opt['kind']]}: «{opt['name']}»")
    return options_list(board_id)


# ---------------------------------------------------------------- поля карточек

def _field_payload(data, current=None):
    """Проверка настроек поля. Для встроенного поля меняются только подпись, подсказка и флаги."""
    values, errors = {}, {}
    builtin = current is not None and current["type"] == "builtin"
    if "label" in data or current is None:
        label = clean_text(data.get("label"), LIMITS["field_label"])
        if not label or len(label) > LIMITS["field_label"]:
            errors["label"] = f"От 1 до {LIMITS['field_label']} символов."
        values["label"] = label
    if not builtin and ("type" in data or current is None):
        if current is not None and data.get("type") != current["type"]:
            errors["type"] = "Тип поля нельзя поменять после создания: создайте новое поле."
        elif data.get("type") not in CUSTOM_FIELD_TYPES:
            errors["type"] = "Выберите тип поля."
        values["type"] = data.get("type")
    if "hint" in data:
        hint = clean_text(data.get("hint"), LIMITS["field_hint"])
        if len(hint) > LIMITS["field_hint"]:
            errors["hint"] = f"До {LIMITS['field_hint']} символов."
        values["hint"] = hint
    for flag in ("required", "show_on_card", "is_active"):
        if flag in data:
            values[flag] = 1 if data.get(flag) else 0
    ftype = values.get("type") or (current["type"] if current is not None else None)
    if ftype == "select" and ("choices" in data or current is None):
        raw = data.get("choices")
        items = raw if isinstance(raw, list) else str(raw or "").split("\n")
        choices = []
        for item in items:
            c = clean_text(item, 60)
            if c and c not in choices:
                choices.append(c[:60])
        if not choices:
            errors["choices"] = "Добавьте хотя бы один вариант (каждый с новой строки)."
        elif len(choices) > MAX_CHOICES:
            errors["choices"] = f"Не больше {MAX_CHOICES} вариантов."
        values["choices"] = "\n".join(choices)
    if values.get("show_on_card") and ftype in ("textarea",):
        errors["show_on_card"] = "Большой текст не помещается на карточку доски."
    return values, errors


@bp.get("/<int:board_id>/fields")
@manager_required
def fields_list(board_id):
    db = get_db()
    fields = M.board_fields(db, board_id)
    have = {f["key"] for f in fields if f["builtin"]}
    # встроенные поля, которых на доске ещё нет, — их можно добавить
    missing = [{"key": k, "label": v["label"]} for k, v in BUILTIN_FIELDS.items() if k not in have]
    for f in fields:
        if not f["builtin"]:
            f["usage"] = db.execute("SELECT COUNT(*) FROM card_values WHERE field_id = ?", (f["id"],)).fetchone()[0]
    return jsonify(fields=fields, missing_builtin=missing, types=CUSTOM_FIELD_TYPES)


@bp.post("/<int:board_id>/fields")
@manager_required
def field_create(board_id):
    db = get_db()
    data = body()
    if db.execute("SELECT COUNT(*) FROM board_fields WHERE board_id = ?", (board_id,)).fetchone()[0] >= MAX_FIELDS_PER_BOARD:
        return fail(f"Не больше {MAX_FIELDS_PER_BOARD} полей на доске.", 409)
    pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM board_fields WHERE board_id = ?",
                     (board_id,)).fetchone()[0]
    builtin_key = data.get("builtin")
    if builtin_key:
        if builtin_key not in BUILTIN_FIELDS:
            return fail("Неизвестное встроенное поле.")
        if db.execute("SELECT 1 FROM board_fields WHERE board_id = ? AND key = ?", (board_id, builtin_key)).fetchone():
            return fail("Это поле уже есть на доске.", 409)
        db.execute("INSERT INTO board_fields (board_id, key, label, type, position, created_at) "
                   "VALUES (?, ?, ?, 'builtin', ?, ?)",
                   (board_id, builtin_key, BUILTIN_FIELDS[builtin_key]["label"], pos, utcnow()))
        label = BUILTIN_FIELDS[builtin_key]["label"]
    else:
        values, errors = _field_payload(data)
        if errors:
            return fail("Проверьте поля.", 400, errors)
        cur = db.execute(
            "INSERT INTO board_fields (board_id, key, label, type, required, choices, hint, show_on_card, "
            "position, created_at) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
            (board_id, values["label"], values["type"], values.get("required", 0), values.get("choices", ""),
             values.get("hint", ""), values.get("show_on_card", 0), pos, utcnow()),
        )
        db.execute("UPDATE board_fields SET key = ? WHERE id = ?", (f"f{cur.lastrowid}", cur.lastrowid))
        label = values["label"]
    _changed(db)
    A.details(f"{g.board['name']}: поле «{label}»")
    return fields_list(board_id), 201


def _get_field(db, board_id, field_id):
    return db.execute("SELECT * FROM board_fields WHERE id = ? AND board_id = ?", (field_id, board_id)).fetchone()


@bp.patch("/<int:board_id>/fields/<int:field_id>")
@manager_required
def field_update(board_id, field_id):
    db = get_db()
    field = _get_field(db, board_id, field_id)
    if field is None:
        return fail("Поле не найдено.", 404)
    values, errors = _field_payload(body(), field)
    if errors:
        return fail("Проверьте поля.", 400, errors)
    values.pop("type", None)
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE board_fields SET {sets} WHERE id = ?", (*values.values(), field_id))
        _changed(db)
    A.details(f"{g.board['name']}: поле «{field['label']}»"
              + (f" → «{values['label']}»" if values.get("label") and values["label"] != field["label"] else ""))
    return fields_list(board_id)


@bp.post("/<int:board_id>/fields/<int:field_id>/move")
@manager_required
def field_move(board_id, field_id):
    db = get_db()
    field = _get_field(db, board_id, field_id)
    if field is None:
        return fail("Поле не найдено.", 404)
    A.details(f"{g.board['name']}: поле «{field['label']}»")
    return (_reorder(db, "board_fields", field_id, body().get("direction"), "board_id = ?", (board_id,))
            or fields_list(board_id))


@bp.delete("/<int:board_id>/fields/<int:field_id>")
@manager_required
def field_delete(board_id, field_id):
    db = get_db()
    field = _get_field(db, board_id, field_id)
    if field is None:
        return fail("Поле не найдено.", 404)
    if field["type"] != "builtin":
        used = db.execute("SELECT COUNT(*) FROM card_values WHERE field_id = ?", (field_id,)).fetchone()[0]
        if used and not body().get("confirm"):
            return fail(f"Поле заполнено в {used} карточках — эти значения удалятся. "
                        "Чтобы просто спрятать поле, отключите его.", 409)
    # у встроенного поля данные остаются в карточке: его можно вернуть кнопкой «Добавить встроенное поле»
    db.execute("DELETE FROM board_fields WHERE id = ?", (field_id,))
    db.execute("UPDATE cards SET updated_at = ? WHERE board_id = ?", (utcnow(), board_id))
    _changed(db)
    A.details(f"{g.board['name']}: поле «{field['label']}»")
    return fields_list(board_id)


# ---------------------------------------------------------------- участники

@bp.get("/<int:board_id>/members")
@manager_required
def members_list(board_id):
    db = get_db()
    rows = db.execute(
        "SELECT u.id, u.username, u.display_name, u.avatar, u.role AS site_role, u.is_active, m.role, m.created_at "
        "FROM board_members m JOIN users u ON u.id = m.user_id WHERE m.board_id = ? "
        "ORDER BY CASE m.role WHEN 'lead' THEN 0 WHEN 'developer' THEN 1 ELSE 2 END, u.display_name COLLATE NOCASE",
        (board_id,),
    ).fetchall()
    members = [{"id": r["id"], "name": r["display_name"], "username": r["username"],
                "avatar": M.avatar_url(r["avatar"]), "site_role": r["site_role"], "role": r["role"],
                "is_active": bool(r["is_active"]), "added_at": r["created_at"]} for r in rows]
    ids = {m["id"] for m in members}
    candidates = [{"id": r["id"], "name": r["display_name"], "username": r["username"], "site_role": r["role"]}
                  for r in db.execute("SELECT id, display_name, username, role FROM users "
                                      "WHERE is_active = 1 AND role != 'pending' ORDER BY display_name COLLATE NOCASE")
                  if r["id"] not in ids]
    return jsonify(members=members, candidates=candidates, roles=BOARD_ROLES)


def _check_role(role):
    return role in BOARD_ROLES


@bp.post("/<int:board_id>/members")
@manager_required
def member_add(board_id):
    db = get_db()
    data = body()
    user = M.get_user(db, data.get("user_id"))
    role = data.get("role")
    if user is None or user["role"] == "pending" or not user["is_active"]:
        return fail("Пользователь не найден. Человеку без доступа сначала выдайте роль в «Пользователях».", 404)
    if not _check_role(role):
        return fail("Выберите роль.", 400, {"role": "Обязательное поле."})
    if role == "lead" and not P.is_admin(g.user):
        return fail("Назначать руководителей может только администратор.", 403)
    db.execute("INSERT INTO board_members (board_id, user_id, role, created_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT(board_id, user_id) DO UPDATE SET role = excluded.role",
               (board_id, user["id"], role, utcnow()))
    db.execute("INSERT INTO notifications (user_id, card_id, event, text, created_at) VALUES (?, NULL, 'access', ?, ?)",
               (user["id"], f"Вас добавили на доску «{g.board['name']}»: {BOARD_ROLES[role]}", utcnow()))
    _changed(db)
    A.details(f"{g.board['name']}: @{user['username']} — {BOARD_ROLES[role]}")
    return members_list(board_id), 201


@bp.patch("/<int:board_id>/members/<int:user_id>")
@manager_required
def member_update(board_id, user_id):
    db = get_db()
    row = db.execute("SELECT m.role, u.username FROM board_members m JOIN users u ON u.id = m.user_id "
                     "WHERE m.board_id = ? AND m.user_id = ?", (board_id, user_id)).fetchone()
    if row is None:
        return fail("Участник не найден.", 404)
    role = body().get("role")
    if not _check_role(role):
        return fail("Неизвестная роль.")
    if "lead" in (role, row["role"]) and role != row["role"] and not P.is_admin(g.user):
        return fail("Назначать и снимать руководителей может только администратор.", 403)
    db.execute("UPDATE board_members SET role = ? WHERE board_id = ? AND user_id = ?", (role, board_id, user_id))
    _changed(db)
    A.details(f"{g.board['name']}: @{row['username']}: {BOARD_ROLES[row['role']]} → {BOARD_ROLES[role]}")
    return members_list(board_id)


@bp.delete("/<int:board_id>/members/<int:user_id>")
@manager_required
def member_remove(board_id, user_id):
    db = get_db()
    row = db.execute("SELECT m.role, u.username FROM board_members m JOIN users u ON u.id = m.user_id "
                     "WHERE m.board_id = ? AND m.user_id = ?", (board_id, user_id)).fetchone()
    if row is None:
        return fail("Участник не найден.", 404)
    if row["role"] == "lead" and not P.is_admin(g.user):
        return fail("Убрать руководителя может только администратор.", 403)
    if user_id == g.user["id"] and not P.is_admin(g.user):
        return fail("Нельзя убрать с доски самого себя.", 409)
    db.execute("DELETE FROM board_members WHERE board_id = ? AND user_id = ?", (board_id, user_id))
    _changed(db)
    A.details(f"{g.board['name']}: @{row['username']}")
    return members_list(board_id)
