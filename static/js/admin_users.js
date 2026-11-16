/* Администратор: пользователи и роли. */
"use strict";

(() => {
  const { $, h, api, busy, toast, BOOT, relDate, fullDate, openModal, closeModal, showFieldErrors } = V;
  const tbody = $("#users-table tbody");
  let users = [];
  let editing = null;

  const roleSelect = (value, includePending) => Object.entries(BOOT.roles)
    .filter(([k]) => includePending || k !== "pending")
    .map(([k, label]) => h("option", { value: k, selected: k === value }, label));

  async function load() {
    try {
      users = (await api("GET", "/api/admin/users")).users;
      render();
    } catch (e) { toast(e.message, "error"); }
  }

  function render() {
    if (!users.length) {
      tbody.replaceChildren(h("tr", null, h("td", { colspan: 6, class: "muted" }, "Пользователей нет")));
      return;
    }
    tbody.replaceChildren(...users.map((u) => {
      const self = u.id === BOOT.user.id;
      const role = h("select", { "aria-label": `Роль: ${u.display_name}`, disabled: self },
        ...roleSelect(u.role, u.role === "pending"));
      role.addEventListener("change", () => patch(u, { role: role.value }, role.value === "pending" ? "Роль снята" : `Роль: ${BOOT.roles[role.value]}`));
      const active = h("input", { type: "checkbox", checked: u.is_active, disabled: self, "aria-label": `Активен: ${u.display_name}` });
      active.addEventListener("change", () => patch(u, { is_active: active.checked }, active.checked ? "Пользователь включён" : "Пользователь отключён — его сессии завершены"));
      const login = [u.has_password ? "пароль" : null, u.discord_id ? "Discord" : null].filter(Boolean).join(" + ") || "—";
      return h("tr", { class: u.role === "pending" ? "is-pending" : "" },
        h("td", null, h("div", { class: "who" },
          h("span", { class: "avatar", "aria-hidden": "true" }, (u.display_name || "?")[0].toUpperCase()),
          h("div", null, h("strong", null, u.display_name, self ? " (вы)" : ""), h("small", null, "@" + u.username + (u.telegram ? " · Telegram ✓" : ""))))),
        h("td", null, login),
        h("td", null, role, u.role === "pending" ? h("div", { class: "small muted" }, "← выдайте роль") : null),
        h("td", null, active),
        h("td", { title: u.last_login_at ? fullDate(u.last_login_at) : "" }, u.last_login_at ? relDate(u.last_login_at) : "не входил"),
        h("td", null, h("div", { class: "row-gap" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => openEdit(u, e.currentTarget) }, "Изменить"),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => openPassword(u, e.currentTarget) }, "Пароль"))));
    }));
  }

  async function patch(u, data, message) {
    try {
      users = (await api("PATCH", `/api/admin/users/${u.id}`, data)).users;
      render();
      toast(message);
    } catch (e) { toast(e.message, "error"); render(); }
  }

  // ------------------------------------------------------------ окно пользователя

  const modal = $("#user-modal");
  const form = $("#user-form");
  function openEdit(u, opener) {
    editing = u || null;
    form.reset();
    showFieldErrors(form, {});
    $("#user-error").hidden = true;
    $("#user-modal-title").textContent = u ? `Пользователь ${u.display_name}` : "Новый пользователь";
    $("#u-role").replaceChildren(...roleSelect(u ? u.role : "tester", u && u.role === "pending"));
    $("#u-username").value = u ? u.username : "";
    $("#u-display").value = u ? u.display_name : "";
    $("#u-discord").value = u ? (u.discord_id || "") : "";
    $("#u-password-field").hidden = !!u;
    $("#u-role").disabled = !!u && u.id === BOOT.user.id;
    openModal(modal, opener);
    $("#u-username").focus();
  }
  $("#add-user-btn").addEventListener("click", (e) => openEdit(null, e.currentTarget));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = {
      username: $("#u-username").value.trim(),
      display_name: $("#u-display").value.trim(),
      role: $("#u-role").value,
      discord_id: $("#u-discord").value.trim(),
    };
    if (!editing) data.password = $("#u-password").value;
    if (editing && editing.id === BOOT.user.id) delete data.role;
    busy($("#user-submit"), async () => {
      try {
        const res = editing
          ? await api("PATCH", `/api/admin/users/${editing.id}`, data)
          : await api("POST", "/api/admin/users", data);
        users = res.users;
        render();
        closeModal(modal);
        toast(editing ? "Изменения сохранены" : "Пользователь добавлен");
      } catch (ex) {
        showFieldErrors(form, ex.fields);
        $("#user-error").textContent = ex.message;
        $("#user-error").hidden = false;
      }
    });
  });

  // ------------------------------------------------------------ пароль

  const passModal = $("#pass-modal");
  let passUser = null;
  function openPassword(u, opener) {
    passUser = u;
    $("#pass-desc").textContent = `Новый пароль для ${u.display_name} (@${u.username}). Передайте его лично, не в общий чат.`;
    $("#pass-input").value = "";
    openModal(passModal, opener);
    $("#pass-input").focus();
  }
  $("#pass-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const password = $("#pass-input").value;
    if (password.length < 10) { toast("Пароль — от 10 символов", "error"); return; }
    busy(e.submitter, async () => {
      try {
        await api("POST", `/api/admin/users/${passUser.id}/password`, { password });
        closeModal(passModal);
        toast("Пароль задан");
        load();
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  load();
})();
