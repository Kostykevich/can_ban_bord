/* Настройки: доски, колонки, поля карточки, списки, участники (для выбранной доски), теги и общие (админ). */
"use strict";

(() => {
  const { $, $$, h, api, busy, toast, BOOT, openModal, closeModal, confirmDialog, plural, roleBadge, showFieldErrors } = V;
  const ROLE_NAMES = { tester: "тестеры", developer: "разработчики" };
  const KIND_SHORT = { backlog: "Входящие", work: "В работе", review: "Проверка", done: "Завершение" };
  const isAdmin = !!BOOT.is_admin;

  let boards = BOOT.boards || [];
  let boardId = null;
  const base = () => `/api/boards/${boardId}`;
  const board = () => boards.find((b) => b.id === boardId);

  // для руководителя прячем общие вкладки администратора
  if (!isAdmin) $$("[data-admin]").forEach((el) => { el.hidden = true; });

  // ------------------------------------------------------------ вкладки (клавиши ←/→ тоже работают)

  const tabs = $$('#settings-tabs [role="tab"]').filter((t) => !t.hidden);
  function selectTab(tab) {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $("#" + t.getAttribute("aria-controls")).hidden = !on;
    });
    history.replaceState(null, "", location.pathname + `?board=${boardId}` + "#" + tab.id.replace("tab-", ""));
    // выбор доски не нужен для общих вкладок
    const global = ["tab-tags", "tab-general", "tab-boards"].includes(tab.id);
    $(".board-picker").classList.toggle("dim", global);
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectTab(t));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      const next = tabs[(i + d + tabs.length) % tabs.length];
      selectTab(next);
      next.focus();
    });
  });

  const moveButtons = (onMove, isFirst, isLast, name) => [
    h("button", { type: "button", class: "icon-btn", "aria-label": `Выше: ${name}`, disabled: isFirst, onclick: (e) => onMove(-1, e.currentTarget) }, "↑"),
    h("button", { type: "button", class: "icon-btn", "aria-label": `Ниже: ${name}`, disabled: isLast, onclick: (e) => onMove(1, e.currentTarget) }, "↓"),
  ];

  // ------------------------------------------------------------ выбор доски

  function renderBoardSelect() {
    const sel = $("#board-select");
    sel.replaceChildren(...boards.map((b) => h("option", { value: b.id }, b.name)));
    sel.value = String(boardId);
  }
  $("#board-select").addEventListener("change", (e) => {
    boardId = Number(e.target.value);
    const current = tabs.find((t) => t.getAttribute("aria-selected") === "true");
    if (current) selectTab(current);
    loadBoardSettings();
  });

  function loadBoardSettings() {
    if (!boardId) return;
    loadColumns();
    loadFields();
    loadOptions();
    loadMembers();
  }

  // ------------------------------------------------------------ доски

  async function loadBoards() {
    try {
      boards = (await api("GET", "/api/boards/manage")).boards.filter((b) => isAdmin || b.my_role === "lead");
      renderBoards();
      renderBoardSelect();
    } catch (e) { toast(e.message, "error"); }
  }

  function renderBoards() {
    $("#boards-list").replaceChildren(...boards.map((b, i) => h("li", { class: "settings-row" },
      isAdmin ? moveButtons(async (dir) => {
        try { boards = (await api("POST", `/api/boards/${b.id}/move`, { direction: dir })).boards; renderBoards(); renderBoardSelect(); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === boards.length - 1, b.name) : null,
      h("div", { class: "grow" },
        h("strong", null, b.name),
        h("small", null, (b.description ? b.description + " · " : "")
          + `${b.card_count} ${plural(b.card_count, "карточка", "карточки", "карточек")} · `
          + `${b.member_count} ${plural(b.member_count, "участник", "участника", "участников")}`)),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { boardId = b.id; renderBoardSelect(); loadBoardSettings(); selectTab($("#tab-columns")); } }, "Настроить"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => openBoard(b, e.currentTarget) }, "Изменить"),
      isAdmin ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => deleteBoard(b) }, "Удалить") : null)));
  }

  const boardModal = $("#board-modal");
  let editingBoard = null;
  function openBoard(b, opener) {
    editingBoard = b || null;
    $("#board-modal-title").textContent = b ? `Доска «${b.name}»` : "Новая доска";
    $("#board-error").hidden = true;
    $("#b-name").value = b ? b.name : "";
    $("#b-desc").value = b ? b.description : "";
    $("#b-create-note").hidden = !!b;
    openModal(boardModal, opener);
    $("#b-name").focus();
  }
  $("#add-board").addEventListener("click", (e) => openBoard(null, e.currentTarget));
  $("#board-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const data = { name: $("#b-name").value.trim(), description: $("#b-desc").value.trim() };
    if (!data.name) { $("#board-error").textContent = "Введите название."; $("#board-error").hidden = false; return; }
    busy(e.submitter, async () => {
      try {
        const res = editingBoard ? await api("PATCH", `/api/boards/${editingBoard.id}`, data) : await api("POST", "/api/boards", data);
        const before = new Set(boards.map((b) => b.id));
        boards = res.boards.filter((b) => isAdmin || b.my_role === "lead");
        const created = boards.find((b) => !before.has(b.id));
        if (created) boardId = created.id;
        renderBoards();
        renderBoardSelect();
        closeModal(boardModal);
        toast(editingBoard ? "Доска сохранена" : "Доска создана — добавьте участников");
        if (created) { loadBoardSettings(); selectTab($("#tab-members")); }
      } catch (ex) { $("#board-error").textContent = ex.message; $("#board-error").hidden = false; }
    });
  });

  async function deleteBoard(b) {
    if (!await confirmDialog(`Доска «${b.name}» со всеми колонками, полями и списками будет удалена. Удалить можно только доску без карточек.`, "Удалить", "Удалить доску?")) return;
    try {
      boards = (await api("DELETE", `/api/boards/${b.id}`)).boards;
      if (boardId === b.id) boardId = boards[0] && boards[0].id;
      renderBoards();
      renderBoardSelect();
      loadBoardSettings();
      toast("Доска удалена");
    } catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------ колонки

  let columns = [];
  async function loadColumns() {
    try { columns = (await api("GET", `${base()}/columns`)).columns; renderColumns(); }
    catch (e) { toast(e.message, "error"); }
  }

  function renderColumns() {
    $("#columns-list").replaceChildren(...columns.map((c, i) => h("li", { class: "settings-row" },
      ...moveButtons(async (dir) => {
        try { columns = (await api("POST", `${base()}/columns/${c.id}/move`, { direction: dir })).columns; renderColumns(); toast("Порядок колонок изменён"); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === columns.length - 1, c.name),
      h("div", { class: "grow" },
        h("strong", null, c.name),
        h("small", null, `${KIND_SHORT[c.kind]} · переносят: ${c.move_roles.length ? c.move_roles.map((r) => ROLE_NAMES[r]).join(", ") : "только руководитель"}`
          + (c.wip_limit ? ` · лимит ${c.wip_limit}` : "") + ` · ${c.card_count} ${plural(c.card_count, "карточка", "карточки", "карточек")}`)),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => openColumn(c, e.currentTarget) }, "Изменить"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => deleteColumn(c, e.currentTarget) }, "Удалить"))));
  }

  const colModal = $("#column-modal");
  const colForm = $("#column-form");
  let editingCol = null;
  $("#c-kind").replaceChildren(...Object.entries(BOOT.column_kinds).map(([k, label]) => h("option", { value: k }, label)));

  function openColumn(c, opener) {
    editingCol = c || null;
    $("#column-modal-title").textContent = c ? `Колонка «${c.name}»` : `Новая колонка · ${board().name}`;
    $("#column-error").hidden = true;
    $("#c-name").value = c ? c.name : "";
    $("#c-kind").value = c ? c.kind : "work";
    $$('input[name="c-roles"]').forEach((i) => { i.checked = c ? c.move_roles.includes(i.value) : i.value === "developer"; });
    $("#c-wip").value = c && c.wip_limit ? c.wip_limit : "";
    openModal(colModal, opener);
    $("#c-name").focus();
  }
  $("#add-column").addEventListener("click", (e) => openColumn(null, e.currentTarget));

  colForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = {
      name: $("#c-name").value.trim(),
      kind: $("#c-kind").value,
      move_roles: $$('input[name="c-roles"]:checked').map((i) => i.value),
      wip_limit: $("#c-wip").value || null,
    };
    if (!data.name) { $("#column-error").textContent = "Введите название."; $("#column-error").hidden = false; return; }
    busy(e.submitter, async () => {
      try {
        const res = editingCol ? await api("PATCH", `${base()}/columns/${editingCol.id}`, data) : await api("POST", `${base()}/columns`, data);
        columns = res.columns;
        renderColumns();
        closeModal(colModal);
        toast(editingCol ? "Колонка сохранена" : "Колонка создана");
      } catch (ex) { $("#column-error").textContent = ex.message; $("#column-error").hidden = false; }
    });
  });

  const dcModal = $("#delete-column-modal");
  let deletingCol = null;
  function deleteColumn(c, opener) {
    deletingCol = c;
    $("#dc-title").textContent = `Удалить колонку «${c.name}»?`;
    const others = columns.filter((x) => x.id !== c.id);
    $("#dc-text").textContent = c.card_count
      ? `В колонке ${c.card_count} ${plural(c.card_count, "карточка", "карточки", "карточек")}. Они не удалятся — выберите, куда их перенести.`
      : "Колонка пустая.";
    $("#dc-target-field").hidden = !c.card_count;
    $("#dc-target").replaceChildren(...others.map((x) => h("option", { value: x.id }, x.name)));
    openModal(dcModal, opener);
  }
  $("#delete-column-form").addEventListener("submit", (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      try {
        columns = (await api("DELETE", `${base()}/columns/${deletingCol.id}`,
          deletingCol.card_count ? { target_column_id: Number($("#dc-target").value) } : {})).columns;
        renderColumns();
        closeModal(dcModal);
        toast("Колонка удалена");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  // ------------------------------------------------------------ поля карточки

  let fields = [];
  let missingBuiltin = [];
  const TYPE_LABEL = (f) => f.builtin ? "встроенное" : (BOOT.field_types[f.type] || f.type).toLowerCase();

  async function loadFields() {
    try {
      const res = await api("GET", `${base()}/fields`);
      fields = res.fields;
      missingBuiltin = res.missing_builtin;
      renderFields();
    } catch (e) { toast(e.message, "error"); }
  }

  function renderFields() {
    $("#fields-list").replaceChildren(...(fields.length ? fields.map((f, i) => h("li", { class: "settings-row" + (f.is_active ? "" : " inactive") },
      ...moveButtons(async (dir) => {
        try { const r = await api("POST", `${base()}/fields/${f.id}/move`, { direction: dir }); fields = r.fields; renderFields(); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === fields.length - 1, f.label),
      h("div", { class: "grow" },
        h("strong", null, f.label),
        h("small", null, [TYPE_LABEL(f),
          f.required ? "обязательное" : null,
          f.show_on_card ? "на карточке" : null,
          f.is_active ? null : "отключено",
          f.type === "select" ? `варианты: ${f.choices.join(", ")}` : null,
          f.usage !== undefined ? `заполнено в ${f.usage}` : null].filter(Boolean).join(" · "))),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => openField(f, e.currentTarget) }, "Изменить"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => patchField(f, { is_active: !f.is_active }, f.is_active ? "Поле отключено" : "Поле включено") },
        f.is_active ? "Отключить" : "Включить"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => deleteField(f) }, "Удалить")))
      : [h("li", { class: "settings-row muted" }, "Своих полей нет — у карточек только название, описание, приоритет и теги")]));
    const addBox = $("#builtin-add");
    addBox.hidden = !missingBuiltin.length;
    $("#builtin-select").replaceChildren(...missingBuiltin.map((b) => h("option", { value: b.key }, b.label)));
  }

  async function patchField(f, data, message) {
    try { fields = (await api("PATCH", `${base()}/fields/${f.id}`, data)).fields; renderFields(); toast(message); }
    catch (e) { toast(e.message, "error"); }
  }

  async function deleteField(f) {
    const text = f.builtin
      ? `Поле «${f.label}» пропадёт из формы и карточек этой доски. Уже введённые данные сохранятся — поле можно вернуть.`
      : `Поле «${f.label}» и его значения во всех карточках доски (${f.usage || 0}) будут удалены. Чтобы только спрятать поле, отключите его.`;
    if (!await confirmDialog(text, "Удалить", "Удалить поле?")) return;
    try {
      const res = await api("DELETE", `${base()}/fields/${f.id}`, { confirm: true });
      fields = res.fields; missingBuiltin = res.missing_builtin; renderFields(); toast("Поле удалено");
    } catch (e) { toast(e.message, "error"); }
  }

  $("#builtin-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const key = $("#builtin-select").value;
    if (!key) return;
    busy(e.submitter, async () => {
      try {
        const res = await api("POST", `${base()}/fields`, { builtin: key });
        fields = res.fields; missingBuiltin = res.missing_builtin; renderFields(); toast("Поле добавлено");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  const fieldModal = $("#field-modal");
  const fieldForm = $("#field-form");
  let editingField = null;
  $("#fl-type").replaceChildren(...Object.entries(BOOT.field_types).map(([k, label]) => h("option", { value: k }, label)));
  function syncFieldType() {
    const type = editingField && editingField.builtin ? null : $("#fl-type").value;
    $("#fl-choices-field").hidden = type !== "select";
    $("#fl-oncard-label").hidden = type === "textarea" || (editingField && editingField.input === "textarea");
  }
  $("#fl-type").addEventListener("change", syncFieldType);

  function openField(f, opener) {
    editingField = f || null;
    showFieldErrors(fieldForm, {});
    $("#field-error").hidden = true;
    $("#field-modal-title").textContent = f ? `Поле «${f.label}»` : `Новое поле · ${board().name}`;
    $("#fl-label").value = f ? f.label : "";
    $("#fl-type").value = f && !f.builtin ? f.type : "text";
    $("#fl-type").disabled = !!f;
    $("#fl-type-field").hidden = !!(f && f.builtin);
    $("#fl-type-note").textContent = f ? "Тип нельзя поменять после создания." : "";
    $("#fl-choices").value = f ? f.choices.join("\n") : "";
    $("#fl-hint").value = f ? f.hint : "";
    $("#fl-required").checked = !!(f && f.required);
    $("#fl-oncard").checked = !!(f && f.show_on_card);
    $("#fl-active").checked = f ? f.is_active : true;
    syncFieldType();
    openModal(fieldModal, opener);
    $("#fl-label").focus();
  }
  $("#add-field").addEventListener("click", (e) => openField(null, e.currentTarget));

  fieldForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = {
      label: $("#fl-label").value.trim(),
      hint: $("#fl-hint").value.trim(),
      required: $("#fl-required").checked,
      show_on_card: !$("#fl-oncard-label").hidden && $("#fl-oncard").checked,
      is_active: $("#fl-active").checked,
    };
    if (!editingField) data.type = $("#fl-type").value;
    if ((editingField ? editingField.type : data.type) === "select") data.choices = $("#fl-choices").value;
    if (!data.label) { showFieldErrors(fieldForm, { label: "Введите название." }); return; }
    busy(e.submitter, async () => {
      try {
        const res = editingField ? await api("PATCH", `${base()}/fields/${editingField.id}`, data) : await api("POST", `${base()}/fields`, data);
        fields = res.fields; missingBuiltin = res.missing_builtin;
        renderFields();
        closeModal(fieldModal);
        toast(editingField ? "Поле сохранено" : "Поле добавлено — оно уже есть в форме карточки");
      } catch (ex) {
        showFieldErrors(fieldForm, ex.fields);
        $("#field-error").textContent = ex.message;
        $("#field-error").hidden = false;
      }
    });
  });

  // ------------------------------------------------------------ списки значений

  let options = {};
  async function loadOptions() {
    try { options = (await api("GET", `${base()}/options`)).options; renderOptions(); }
    catch (e) { toast(e.message, "error"); }
  }

  function renderOptions() {
    $("#lists-grid").replaceChildren(...Object.entries(BOOT.option_kinds).map(([kind, title]) => {
      const items = options[kind] || [];
      const input = h("input", { maxlength: 40, placeholder: "Новое значение", "aria-label": `Добавить: ${title}` });
      const add = h("button", { type: "submit", class: "btn btn-sm" }, "Добавить");
      const form = h("form", { class: "add-row" }, input, add);
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const name = input.value.trim();
        if (!name) return;
        busy(add, async () => {
          try { options = (await api("POST", `${base()}/options`, { kind, name })).options; renderOptions(); toast("Добавлено"); $(`[aria-label="Добавить: ${title}"]`)?.focus(); }
          catch (ex) { toast(ex.message, "error"); }
        });
      });
      return h("div", { class: "list-box" }, h("h3", null, title),
        h("ul", { class: "settings-list" }, items.map((o, i) => optionRow(o, i, items.length))), form);
    }));
  }

  function optionRow(o, i, n) {
    return h("li", { class: "settings-row" + (o.is_active ? "" : " inactive") },
      ...moveButtons(async (dir) => {
        try { options = (await api("POST", `${base()}/options/${o.id}/move`, { direction: dir })).options; renderOptions(); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === n - 1, o.name),
      h("div", { class: "grow" }, h("strong", null, o.name),
        h("small", null, (o.is_active ? "" : "отключено · ") + `${o.usage} ${plural(o.usage, "карточка", "карточки", "карточек")}`)),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => renameOption(o) }, "Переименовать"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => patchOption(o, { is_active: !o.is_active }, o.is_active ? "Отключено" : "Включено") },
        o.is_active ? "Отключить" : "Включить"),
      o.usage ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => deleteOption(o) }, "Удалить"));
  }

  async function patchOption(o, data, message) {
    try { options = (await api("PATCH", `${base()}/options/${o.id}`, data)).options; renderOptions(); toast(message); }
    catch (e) { toast(e.message, "error"); }
  }

  function inlineRename(current, onSave) {
    // простое переименование через небольшое окно подтверждения с полем ввода
    const dialog = $("#confirm-modal");
    const input = h("input", { value: current, maxlength: 40, "aria-label": "Новое название" });
    const text = $("#confirm-text");
    const original = Array.from(text.childNodes);
    text.replaceChildren(input);
    const ok = $("#confirm-ok");
    ok.textContent = "Сохранить";
    ok.className = "btn btn-primary";
    $("#confirm-title").textContent = "Переименовать";
    const save = () => { const v = input.value.trim(); if (v && v !== current) onSave(v); closeModal(dialog); };
    ok.addEventListener("click", save, { once: true });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    dialog.addEventListener("close", () => { ok.removeEventListener("click", save); ok.className = "btn btn-danger"; text.replaceChildren(...original); }, { once: true });
    openModal(dialog);
    input.select();
  }

  function renameOption(o) { inlineRename(o.name, (name) => patchOption(o, { name }, "Переименовано")); }

  async function deleteOption(o) {
    if (!await confirmDialog(`Значение «${o.name}» будет удалено.`, "Удалить", "Удалить значение?")) return;
    try { options = (await api("DELETE", `${base()}/options/${o.id}`)).options; renderOptions(); toast("Удалено"); }
    catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------ участники

  let members = { members: [], candidates: [] };
  const roleChoices = () => Object.entries(BOOT.board_roles).filter(([k]) => isAdmin || k !== "lead");

  async function loadMembers() {
    try { members = await api("GET", `${base()}/members`); renderMembers(); }
    catch (e) { toast(e.message, "error"); }
  }

  function renderMembers() {
    $("#member-user").replaceChildren(h("option", { value: "" }, members.candidates.length ? "Кого добавить…" : "Все уже на доске"),
      ...members.candidates.map((u) => h("option", { value: u.id }, `${u.name} (@${u.username})`)));
    $("#member-role").replaceChildren(...roleChoices().map(([k, label]) => h("option", { value: k }, label)));
    $("#member-role").value = "tester";
    $("#members-list").replaceChildren(...(members.members.length ? members.members.map((m) => {
      const leadLocked = m.role === "lead" && !isAdmin;
      const select = h("select", { "aria-label": `Роль: ${m.name}`, disabled: leadLocked, onchange: async (e) => {
        try { members = await api("PATCH", `${base()}/members/${m.id}`, { role: e.target.value }); renderMembers(); toast("Роль изменена"); }
        catch (ex) { toast(ex.message, "error"); renderMembers(); }
      } }, (leadLocked ? Object.entries(BOOT.board_roles) : roleChoices()).map(([k, label]) => h("option", { value: k, selected: k === m.role }, label)));
      return h("li", { class: "settings-row" + (m.is_active ? "" : " inactive") },
        V.avatarEl(m),
        h("div", { class: "grow" }, h("strong", null, m.name, " "), roleBadge(m.role),
          h("small", null, `@${m.username}` + (m.site_role === "admin" ? " · администратор сайта" : "") + (m.is_active ? "" : " · отключён"))),
        select,
        leadLocked ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => {
          if (!await confirmDialog(`${m.name} перестанет видеть доску «${board().name}». Его карточки останутся.`, "Убрать", "Убрать с доски?")) return;
          try { members = await api("DELETE", `${base()}/members/${m.id}`); renderMembers(); toast("Участник убран"); }
          catch (ex) { toast(ex.message, "error"); }
        } }, "Убрать"));
    }) : [h("li", { class: "settings-row muted" }, "На доске пока никого нет — добавьте людей выше")]));
  }

  $("#member-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const userId = Number($("#member-user").value);
    if (!userId) { toast("Выберите, кого добавить", "error"); return; }
    busy(e.submitter, async () => {
      try {
        members = await api("POST", `${base()}/members`, { user_id: userId, role: $("#member-role").value });
        renderMembers();
        toast("Участник добавлен");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  // ------------------------------------------------------------ теги (админ)

  let tags = [];
  async function loadTags() {
    try { tags = (await api("GET", "/api/admin/tags")).tags; renderTags(); }
    catch (e) { toast(e.message, "error"); }
  }
  function renderTags() {
    const list = $("#tags-list");
    if (!tags.length) { list.replaceChildren(h("li", { class: "settings-row muted" }, "Тегов пока нет — их создают участники в карточках")); return; }
    list.replaceChildren(...tags.map((t) => h("li", { class: "settings-row" },
      h("div", { class: "grow" }, h("span", { class: "tag" }, "#" + t.name), h("small", null, `${t.count} ${plural(t.count, "карточка", "карточки", "карточек")}`)),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => inlineRename(t.name, async (name) => {
        try { tags = (await api("PATCH", `/api/admin/tags/${t.id}`, { name })).tags; renderTags(); toast("Тег переименован"); }
        catch (e) { toast(e.message, "error"); }
      }) }, "Переименовать"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => {
        if (!await confirmDialog(`Тег #${t.name} будет снят со всех карточек (${t.count}).`, "Удалить", "Удалить тег?")) return;
        try { tags = (await api("DELETE", `/api/admin/tags/${t.id}`)).tags; renderTags(); toast("Тег удалён"); }
        catch (e) { toast(e.message, "error"); }
      } }, "Удалить"))));
  }

  // ------------------------------------------------------------ общие (админ)

  async function loadGeneral() {
    try {
      const s = await api("GET", "/api/admin/settings");
      $("#g-days").value = s.done_visible_days;
      const box = $("#g-channel-events");
      box.replaceChildren(h("legend", null, "Что отправлять в общий канал Discord"),
        ...Object.entries(s.channel_event_labels).map(([k, label]) => h("label", { class: "check" },
          h("input", { type: "checkbox", value: k, checked: s.channel_events.includes(k) }), label)));
    } catch (e) { toast(e.message, "error"); }
  }
  $("#general-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const data = {
      done_visible_days: Number($("#g-days").value),
      channel_events: $$("#g-channel-events input:checked").map((i) => i.value),
    };
    busy(e.submitter, async () => {
      try { await api("PATCH", "/api/admin/settings", data); toast("Настройки сохранены"); }
      catch (ex) { toast(ex.message, "error"); }
    });
  });

  // ------------------------------------------------------------ старт

  const ids = boards.map((b) => b.id);
  boardId = ids.includes(BOOT.board_id) ? BOOT.board_id : ids[0] || null;
  renderBoardSelect();
  const fromHash = tabs.find((t) => t.id === "tab-" + location.hash.slice(1));
  selectTab(fromHash || $("#tab-columns"));
  loadBoards();
  loadBoardSettings();
  if (isAdmin) { loadTags(); loadGeneral(); }
})();
