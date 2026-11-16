/* Администратор: колонки, списки значений, теги, общие настройки. */
"use strict";

(() => {
  const { $, $$, h, api, busy, toast, BOOT, openModal, closeModal, confirmDialog, plural } = V;
  const ROLE_NAMES = { tester: "тестеры", developer: "разработчики" };
  const KIND_SHORT = { backlog: "Входящие", work: "В работе", review: "Проверка", done: "Завершение" };

  // ------------------------------------------------------------ вкладки (клавиши ←/→ тоже работают)

  const tabs = $$('#settings-tabs [role="tab"]');
  function selectTab(tab) {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $("#" + t.getAttribute("aria-controls")).hidden = !on;
    });
    history.replaceState(null, "", "#" + tab.id.replace("tab-", ""));
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
  const fromHash = tabs.find((t) => t.id === "tab-" + location.hash.slice(1));
  if (fromHash) selectTab(fromHash);

  const moveButtons = (onMove, isFirst, isLast, name) => [
    h("button", { type: "button", class: "icon-btn", "aria-label": `Выше: ${name}`, disabled: isFirst, onclick: (e) => onMove(-1, e.currentTarget) }, "↑"),
    h("button", { type: "button", class: "icon-btn", "aria-label": `Ниже: ${name}`, disabled: isLast, onclick: (e) => onMove(1, e.currentTarget) }, "↓"),
  ];

  // ------------------------------------------------------------ колонки

  let columns = [];
  async function loadColumns() {
    try { columns = (await api("GET", "/api/admin/columns")).columns; renderColumns(); }
    catch (e) { toast(e.message, "error"); }
  }

  function renderColumns() {
    $("#columns-list").replaceChildren(...columns.map((c, i) => h("li", { class: "settings-row" },
      ...moveButtons(async (dir, btn) => {
        try { columns = (await api("POST", `/api/admin/columns/${c.id}/move`, { direction: dir })).columns; renderColumns(); toast("Порядок колонок изменён"); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === columns.length - 1, c.name),
      h("div", { class: "grow" },
        h("strong", null, c.name),
        h("small", null, `${KIND_SHORT[c.kind]} · переносят: ${c.move_roles.length ? c.move_roles.map((r) => ROLE_NAMES[r]).join(", ") : "только админ"}`
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
    $("#column-modal-title").textContent = c ? `Колонка «${c.name}»` : "Новая колонка";
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
        const res = editingCol ? await api("PATCH", `/api/admin/columns/${editingCol.id}`, data) : await api("POST", "/api/admin/columns", data);
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
        columns = (await api("DELETE", `/api/admin/columns/${deletingCol.id}`,
          deletingCol.card_count ? { target_column_id: Number($("#dc-target").value) } : {})).columns;
        renderColumns();
        closeModal(dcModal);
        toast("Колонка удалена");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  // ------------------------------------------------------------ списки значений

  let options = {};
  async function loadOptions() {
    try { options = (await api("GET", "/api/admin/options")).options; renderOptions(); }
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
          try { options = (await api("POST", "/api/admin/options", { kind, name })).options; renderOptions(); toast("Добавлено"); $(`[aria-label="Добавить: ${title}"]`)?.focus(); }
          catch (ex) { toast(ex.message, "error"); }
        });
      });
      return h("div", { class: "list-box" }, h("h3", null, title),
        h("ul", { class: "settings-list" }, items.map((o, i) => optionRow(o, i, items.length))), form);
    }));
  }

  function optionRow(o, i, n) {
    const row = h("li", { class: "settings-row" + (o.is_active ? "" : " inactive") },
      ...moveButtons(async (dir) => {
        try { options = (await api("POST", `/api/admin/options/${o.id}/move`, { direction: dir })).options; renderOptions(); }
        catch (e) { toast(e.message, "error"); }
      }, i === 0, i === n - 1, o.name),
      h("div", { class: "grow" }, h("strong", null, o.name),
        h("small", null, (o.is_active ? "" : "отключено · ") + `${o.usage} ${plural(o.usage, "карточка", "карточки", "карточек")}`)),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => renameOption(o) }, "Переименовать"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => patchOption(o, { is_active: !o.is_active }, o.is_active ? "Отключено" : "Включено") },
        o.is_active ? "Отключить" : "Включить"),
      o.usage ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => deleteOption(o) }, "Удалить"));
    return row;
  }

  async function patchOption(o, data, message) {
    try { options = (await api("PATCH", `/api/admin/options/${o.id}`, data)).options; renderOptions(); toast(message); }
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
    try { options = (await api("DELETE", `/api/admin/options/${o.id}`)).options; renderOptions(); toast("Удалено"); }
    catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------ теги

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

  // ------------------------------------------------------------ общие

  async function loadGeneral() {
    try {
      const s = await api("GET", "/api/admin/settings");
      $("#g-title").value = s.board_title;
      $("#g-subtitle").value = s.board_subtitle;
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
      board_title: $("#g-title").value.trim(),
      board_subtitle: $("#g-subtitle").value.trim(),
      done_visible_days: Number($("#g-days").value),
      channel_events: $$("#g-channel-events input:checked").map((i) => i.value),
    };
    if (!data.board_title) { toast("Заголовок не может быть пустым", "error"); return; }
    busy(e.submitter, async () => {
      try { await api("PATCH", "/api/admin/settings", data); toast("Настройки сохранены"); }
      catch (ex) { toast(ex.message, "error"); }
    });
  });

  loadColumns();
  loadOptions();
  loadTags();
  loadGeneral();
})();
