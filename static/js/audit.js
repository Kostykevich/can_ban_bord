/* Журнал действий в профиле администратора. */
"use strict";

(() => {
  const { $, h, api, toast, BOOT, fullDate, relDate, avatarEl } = V;
  const body = $("#audit-body");
  if (!body || BOOT.user.role !== "admin") return;

  const userSel = $("#a-user");
  const groupSel = $("#a-group");
  const search = $("#a-q");
  const more = $("#a-more");
  let lastId = null;
  let groupsFilled = false;

  for (const u of BOOT.audit_users || []) userSel.append(h("option", { value: u.id }, `${u.name} (@${u.username})`));

  function params(beforeId) {
    const p = new URLSearchParams();
    if (userSel.value) p.set("user_id", userSel.value);
    if (groupSel.value) p.set("group", groupSel.value);
    if (search.value.trim()) p.set("q", search.value.trim());
    if (beforeId) p.set("before_id", beforeId);
    return p.toString();
  }

  function row(x) {
    const who = x.user
      ? h("div", { class: "who" }, avatarEl(x.user, "avatar-xs"), h("span", null, x.user.name, h("small", null, "@" + x.user.username)))
      : h("span", { class: "muted" }, "гость");
    const card = x.card_id
      ? (x.card_exists ? h("a", { href: `/card/${x.card_id}` }, `#${x.card_id}`) : h("span", { class: "muted" }, `#${x.card_id} (удалена)`))
      : null;
    const failed = x.action === "auth.login_failed";
    return h("tr", { class: failed ? "audit-warn" : "" },
      h("td", { title: fullDate(x.created_at), class: "nowrap" }, relDate(x.created_at), h("small", { class: "muted block" }, fullDate(x.created_at))),
      h("td", null, who),
      h("td", null, failed ? h("strong", null, "⚠ " + x.label) : x.label),
      h("td", { class: "audit-details" }, card, card && x.details ? " " : null, x.details || (card ? "" : "—")),
      h("td", { class: "muted small nowrap" }, x.ip || "—"));
  }

  async function load(append) {
    try {
      const res = await api("GET", "/api/admin/audit?" + params(append ? lastId : null));
      if (!groupsFilled) {
        for (const [k, label] of Object.entries(res.groups)) groupSel.append(h("option", { value: k }, label));
        groupsFilled = true;
      }
      const rows = res.items.map(row);
      if (append) body.append(...rows);
      else body.replaceChildren(...(rows.length ? rows : [h("tr", null, h("td", { colspan: 5, class: "muted" }, "Записей нет"))]));
      lastId = res.items.length ? res.items[res.items.length - 1].id : lastId;
      more.hidden = !res.has_more;
    } catch (e) { toast(e.message, "error"); }
  }

  let timer = null;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load(false), 300); });
  userSel.addEventListener("change", () => load(false));
  groupSel.addEventListener("change", () => load(false));
  $("#audit-filters").addEventListener("submit", (e) => { e.preventDefault(); load(false); });
  $("#a-refresh").addEventListener("click", () => load(false));
  more.addEventListener("click", () => load(true));
  load(false);
})();
