/* Общие функции для всех страниц: запросы к API, тосты, модальные окна,
   форматирование дат, @упоминания и колокольчик уведомлений.
   Пользовательский текст всегда вставляется через textContent — защита от XSS. */
"use strict";

const V = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function readJSON(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  const BOOT = readJSON("boot") || {};

  /* Создание элементов: h("div", {class: "x", onclick: fn}, "текст", child) */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "text") el.textContent = v;
        // Атрибут style блокируется CSP (style-src 'self'), а свойства через CSSOM — нет
        else if (k === "style") {
          const props = typeof v === "string"
            ? v.split(";").map((p) => p.split(":")).filter((p) => p.length > 1).map(([n, ...r]) => [n, r.join(":")])
            : Object.entries(v);
          for (const [name, value] of props) el.style.setProperty(name.trim(), String(value).trim());
        }
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, v);
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // ------------------------------------------------------------ API

  class ApiError extends Error {
    constructor(message, status, fields) { super(message); this.status = status; this.fields = fields || {}; }
  }

  async function api(method, url, data, isForm) {
    const opts = { method, headers: { "X-CSRF-Token": BOOT.csrf || "", "Accept": "application/json" }, credentials: "same-origin" };
    if (data !== undefined) {
      if (isForm) opts.body = data;
      else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(data); }
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      throw new ApiError("Нет связи с сервером. Проверьте интернет — ваши данные не потеряны, попробуйте ещё раз.", 0);
    }
    let payload = null;
    try { payload = await resp.json(); } catch (e) { /* не JSON */ }
    if (resp.status === 401) {
      toast("Сессия закончилась. Войдите снова.", "error");
      setTimeout(() => { location.href = "/login?next=" + encodeURIComponent(location.pathname); }, 1500);
      throw new ApiError("Нужно войти в аккаунт.", 401);
    }
    if (!resp.ok) {
      throw new ApiError((payload && payload.error) || `Ошибка сервера (${resp.status}). Попробуйте ещё раз.`,
                         resp.status, payload && payload.fields);
    }
    return payload;
  }

  /* Блокирует кнопку на время запроса — защита от двойной отправки */
  async function busy(button, fn) {
    if (!button) return fn();
    if (button.dataset.busy === "1") return undefined;
    button.dataset.busy = "1";
    button.disabled = true;
    const spinner = h("span", { class: "spinner", "aria-hidden": "true" });
    button.prepend(spinner);
    try { return await fn(); }
    finally { button.disabled = false; delete button.dataset.busy; spinner.remove(); }
  }

  // ------------------------------------------------------------ тосты

  const ICONS = { success: "✓", error: "!", info: "i" };

  /* Поднимает контейнер тостов на самый верх «верхнего слоя» — выше открытых окон.
     Браузер показывает последним открытый popover/dialog поверх остальных. */
  function raiseToasts() {
    const box = document.getElementById("toasts");
    if (!box || typeof box.showPopover !== "function") return;
    try {
      if (box.matches(":popover-open")) box.hidePopover();
      box.showPopover();
    } catch (e) { /* старый браузер — остаётся обычный z-index */ }
  }
  function toast(message, type = "success", timeout) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const ms = timeout || (type === "error" ? 7000 : 4000);
    const el = h("div", { class: `toast toast-${type}`, role: type === "error" ? "alert" : "status" },
      h("span", { class: "toast-icon", "aria-hidden": "true" }, ICONS[type] || "i"),
      h("p", { class: "toast-text", text: message }),
      h("button", { type: "button", class: "toast-close", "aria-label": "Закрыть уведомление" }, "✕"));
    let timer;
    const close = () => {
      clearTimeout(timer);
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 200);
    };
    const start = () => { timer = setTimeout(close, ms); };
    el.querySelector(".toast-close").addEventListener("click", close);
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", start);
    box.append(el);
    while (box.children.length > 5) box.firstElementChild.remove();
    raiseToasts();
    start();
  }

  // ------------------------------------------------------------ даты (храним UTC, показываем по Москве)

  const TZ = "Europe/Moscow";
  const fullFmt = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "numeric", month: "short" });

  function parse(iso) { return iso ? new Date(iso) : null; }
  function fullDate(iso) { const d = parse(iso); return d ? fullFmt.format(d) + " МСК" : "—"; }
  function relDate(iso) {
    const d = parse(iso);
    if (!d) return "—";
    const sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (sec < 45) return "только что";
    const min = Math.round(sec / 60);
    if (min < 60) return `${min} ${plural(min, "минуту", "минуты", "минут")} назад`;
    const hr = Math.round(min / 60);
    if (hr < 24) return `${hr} ${plural(hr, "час", "часа", "часов")} назад`;
    const day = Math.round(hr / 24);
    if (day < 7) return day === 1 ? "вчера" : `${day} ${plural(day, "день", "дня", "дней")} назад`;
    return dateFmt.format(d);
  }
  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }
  function timeEl(iso) {
    return h("time", { datetime: iso || "", title: fullDate(iso) }, relDate(iso));
  }
  // Раз в минуту обновляем «5 минут назад»
  setInterval(() => {
    $$("time[datetime]").forEach((t) => { if (t.dateTime) t.textContent = relDate(t.dateTime); });
  }, 60000);

  // ------------------------------------------------------------ текст с @упоминаниями и ссылками

  /* Аватарка: картинка, если загружена, иначе первая буква имени */
  function avatarEl(user, extraClass = "") {
    const cls = "avatar" + (extraClass ? " " + extraClass : "");
    if (user && user.avatar) return h("img", { class: cls, src: user.avatar, alt: "", loading: "lazy" });
    return h("span", { class: cls, "aria-hidden": "true" }, ((user && user.name) || "?")[0].toUpperCase());
  }

  /* Роль на доске: значок «Руководитель», «Разработчик»… */
  const ROLE_LABELS = { admin: "Администратор", lead: "Руководитель", developer: "Разработчик", tester: "Тестер", pending: "Ожидает доступа" };
  function roleLabel(role) { return ROLE_LABELS[role] || ""; }
  function roleBadge(role, short = false) {
    if (!role || !ROLE_LABELS[role]) return null;
    const text = short ? { admin: "Админ", lead: "Рук.", developer: "Разраб.", tester: "Тестер" }[role] || ROLE_LABELS[role] : ROLE_LABELS[role];
    return h("span", { class: `role-badge role-${role}`, title: ROLE_LABELS[role] }, text);
  }

  /* Имя человека, по клику — мини-профиль */
  function userLink(user, { avatar = true, size = "avatar-xs", role = null, short = false } = {}) {
    if (!user) return h("span", { class: "muted" }, "Удалённый пользователь");
    return h("button", { type: "button", class: "user-link", dataset: { userId: user.id }, title: `Профиль: ${user.name}` },
      avatar ? avatarEl(user, size) : null,
      h("span", { class: "user-link-name" }, user.name),
      role ? roleBadge(role, short) : null);
  }

  /* Находит пользователя по логину — текущему или старому (после смены логина) */
  function userByLogin(login) {
    const l = login.toLowerCase();
    return mentionUsers.find((u) => u.username.toLowerCase() === l)
      || mentionUsers.find((u) => (u.aliases || []).some((a) => a.toLowerCase() === l));
  }

  const RICH_RE = /(https?:\/\/[^\s<>"']+)|(^|[^\w@])@([A-Za-z0-9_.\-]{2,32})/g;
  function rich(text) {
    const frag = document.createDocumentFragment();
    let last = 0;
    String(text || "").replace(RICH_RE, (match, url, pre, name, offset) => {
      frag.append(document.createTextNode(text.slice(last, offset)));
      if (url) {
        frag.append(h("a", { href: url, target: "_blank", rel: "noopener noreferrer nofollow" }, url));
      } else {
        frag.append(document.createTextNode(pre));
        // старый логин показываем как текущий — упоминание не «ломается» после смены логина
        const user = userByLogin(name);
        frag.append(user
          ? h("button", { type: "button", class: "mention", title: `Профиль: ${user.name}`, dataset: { userId: user.id } }, "@" + user.username)
          : h("span", { class: "mention" }, "@" + name));
      }
      last = offset + match.length;
      return match;
    });
    frag.append(document.createTextNode(String(text || "").slice(last)));
    return frag;
  }

  // ------------------------------------------------------------ модальные окна

  const openers = new WeakMap();
  function openModal(dialog, opener) {
    if (!dialog) return;
    openers.set(dialog, opener || document.activeElement);
    if (!dialog.open) dialog.showModal();
    // если тосты уже на экране — оставляем их поверх нового окна
    if (document.getElementById("toasts")?.children.length) raiseToasts();
  }
  function closeModal(dialog) { if (dialog && dialog.open) dialog.close(); }

  function setupModals() {
    $$("dialog.modal").forEach((dialog) => {
      dialog.addEventListener("click", (e) => {
        if (e.target.closest("[data-close]")) closeModal(dialog);
        else if (e.target === dialog) {
          // клик по затемнению (вне окна) закрывает окно
          const r = dialog.getBoundingClientRect();
          const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
          if (!inside) closeModal(dialog);
        }
      });
      dialog.addEventListener("close", () => {
        const back = openers.get(dialog);
        if (back && document.contains(back) && typeof back.focus === "function") back.focus();
      });
    });
  }

  function confirmDialog(text, okLabel = "Подтвердить", title = "Вы уверены?") {
    const dialog = document.getElementById("confirm-modal");
    return new Promise((resolve) => {
      $("#confirm-title", dialog).textContent = title;
      $("#confirm-text", dialog).textContent = text;
      const ok = $("#confirm-ok", dialog);
      ok.textContent = okLabel;
      let result = false;
      const onOk = () => { result = true; closeModal(dialog); };
      ok.addEventListener("click", onOk, { once: true });
      dialog.addEventListener("close", () => { ok.removeEventListener("click", onOk); resolve(result); }, { once: true });
      openModal(dialog);
      ok.focus();
    });
  }

  // ------------------------------------------------------------ ошибки полей формы

  function showFieldErrors(form, fields) {
    $$("[data-error-for]", form).forEach((el) => { el.textContent = ""; el.closest(".field")?.classList.remove("invalid"); });
    let first = null;
    for (const [name, message] of Object.entries(fields || {})) {
      const hint = $(`[data-error-for="${name}"]`, form);
      if (hint) {
        hint.textContent = message;
        const field = hint.closest(".field");
        field?.classList.add("invalid");
        first = first || field?.querySelector("input, textarea, select");
      }
    }
    if (first) first.focus();
  }

  // ------------------------------------------------------------ @упоминания: подсказка при вводе

  let mentionUsers = BOOT.users || [];
  function setMentionUsers(users) { mentionUsers = users || []; }

  function attachMentions(textarea) {
    if (!textarea || textarea.dataset.mentionsReady) return;
    textarea.dataset.mentionsReady = "1";
    let menu = null, items = [], index = 0, start = -1;

    const close = () => { menu?.remove(); menu = null; start = -1; };
    const insert = (user) => {
      const value = textarea.value;
      const caret = textarea.selectionStart;
      const before = value.slice(0, start) + "@" + user.username + " ";
      textarea.value = before + value.slice(caret);
      textarea.setSelectionRange(before.length, before.length);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      close();
      textarea.focus();
    };
    const render = () => {
      menu.replaceChildren(...items.map((u, i) => h("li", {
        role: "option", "aria-selected": i === index ? "true" : "false",
        onmousedown: (e) => { e.preventDefault(); insert(u); },
      }, avatarEl(u, "avatar-xs"),
         h("span", null, u.name, " ", h("small", null, "@" + u.username)))));
    };
    textarea.addEventListener("input", () => {
      const caret = textarea.selectionStart;
      const m = /(^|[^\w@])@([A-Za-z0-9_.\-]{0,32})$/.exec(textarea.value.slice(0, caret));
      if (!m) return close();
      start = caret - m[2].length - 1;
      const q = m[2].toLowerCase();
      items = mentionUsers.filter((u) => u.username.toLowerCase().includes(q) || (u.name || "").toLowerCase().includes(q)).slice(0, 6);
      if (!items.length) return close();
      index = 0;
      if (!menu) {
        menu = h("ul", { class: "mention-menu", role: "listbox", "aria-label": "Упомянуть пользователя" });
        (textarea.closest("dialog") || document.body).append(menu);
      }
      const r = textarea.getBoundingClientRect();
      menu.style.left = Math.min(r.left, window.innerWidth - 240) + "px";
      menu.style.top = Math.min(r.bottom + 4, window.innerHeight - 230) + "px";
      render();
    });
    textarea.addEventListener("keydown", (e) => {
      if (!menu) return;
      if (e.key === "ArrowDown") { index = (index + 1) % items.length; render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { index = (index - 1 + items.length) % items.length; render(); e.preventDefault(); }
      else if (e.key === "Enter" || e.key === "Tab") { insert(items[index]); e.preventDefault(); }
      else if (e.key === "Escape") { close(); e.preventDefault(); e.stopPropagation(); }
    });
    textarea.addEventListener("blur", () => setTimeout(close, 150));
  }

  // ------------------------------------------------------------ колокольчик

  function setUnread(n) {
    const badge = document.getElementById("bell-count");
    if (!badge) return;
    const before = parseInt(badge.dataset.n || "0", 10);
    badge.dataset.n = String(n);
    if (n > before && before >= 0 && badge.dataset.ready) {
      // новое уведомление — колокольчик «звенит», счётчик подпрыгивает
      badge.classList.remove("bump");
      void badge.offsetWidth;
      badge.classList.add("bump");
      const bell = document.getElementById("bell-btn");
      bell?.classList.remove("ring"); void bell?.offsetWidth; bell?.classList.add("ring");
    }
    badge.dataset.ready = "1";
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.hidden = !n;
    document.title = document.title.replace(/^\(\d+\+?\) /, "");
    if (n) document.title = `(${n > 99 ? "99+" : n}) ` + document.title;
  }

  function setupBell() {
    const btn = document.getElementById("bell-btn");
    const panel = document.getElementById("bell-panel");
    if (!btn || !panel) return;
    const list = document.getElementById("bell-list");

    const render = (data) => {
      setUnread(data.unread);
      if (!data.items.length) {
        list.replaceChildren(h("li", { class: "bell-empty" }, "Пока нет уведомлений"));
        return;
      }
      list.replaceChildren(...data.items.map((n) => h("li", null,
        h("button", {
          type: "button", class: "bell-item" + (n.read ? "" : " unread"),
          onclick: async () => {
            try { if (!n.read) await api("POST", "/api/notifications/read", { ids: [n.id] }); } catch (e) { /* не критично */ }
            close();
            if (n.card_id) {
              if (window.VBoard && window.VBoard.openCard) window.VBoard.openCard(n.card_id);
              else location.href = "/card/" + n.card_id;
            } else if (n.event === "access" && BOOT.user && BOOT.user.role === "admin") {
              location.href = "/admin/users";
            }
          },
        }, n.text, n.card_id ? h("small", null, `#${n.card_id} ${n.card_title || ""}`) : null,
           h("small", null, relDate(n.created_at))))));
    };
    const open = async () => {
      panel.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      list.replaceChildren(h("li", { class: "bell-empty" }, "Загрузка…"));
      try { render(await api("GET", "/api/notifications")); }
      catch (e) { list.replaceChildren(h("li", { class: "bell-empty" }, e.message)); }
    };
    const close = () => { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", () => (panel.hidden ? open() : close()));
    document.addEventListener("click", (e) => { if (!panel.hidden && !e.target.closest(".bell-wrap")) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) { close(); btn.focus(); } });
    document.getElementById("bell-read-all").addEventListener("click", async () => {
      try { render(await api("POST", "/api/notifications/read", { all: true })); }
      catch (e) { toast(e.message, "error"); }
    });

    // На страницах без доски обновляем счётчик раз в минуту
    if (!document.getElementById("board")) {
      setInterval(async () => {
        if (document.visibilityState !== "visible") return;
        try { setUnread((await api("GET", "/api/board/stamp")).unread); } catch (e) { /* тихо */ }
      }, 60000);
    }
  }

  // ------------------------------------------------------------ мини-профиль

  /* Карточка человека поверх страницы (и поверх открытого окна — через popover).
     Страница доски может передать свои действия через V.userCardActions(user) */
  let pop = null;
  let popFor = null;
  let userCardActions = null;
  let userCardBoard = null;
  function setUserCardContext(boardId, actions) { userCardBoard = boardId; userCardActions = actions; }

  function closeUserCard() {
    if (!pop) return;
    try { if (pop.matches(":popover-open")) pop.hidePopover(); } catch (e) { /* */ }
    pop.remove();
    pop = null;
    const back = popFor;
    popFor = null;
    if (back && document.contains(back)) back.focus();
  }

  function placePop(anchor) {
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, ht = pop.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    let top = r.bottom + 6;
    if (top + ht > window.innerHeight - 8) top = Math.max(8, r.top - ht - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  async function openUserCard(userId, anchor) {
    if (popFor === anchor && pop) { closeUserCard(); return; }
    closeUserCard();
    popFor = anchor;
    pop = h("div", { class: "user-pop", role: "dialog", "aria-label": "Профиль участника", tabindex: "-1" },
      h("p", { class: "muted small m0" }, "Загрузка…"));
    const usePopover = typeof pop.showPopover === "function";
    if (usePopover) pop.setAttribute("popover", "manual");
    (anchor.closest("dialog[open]") && !usePopover ? anchor.closest("dialog[open]") : document.body).append(pop);
    if (usePopover) { try { pop.showPopover(); } catch (e) { /* */ } }
    placePop(anchor);
    let u;
    try {
      u = await api("GET", `/api/users/${userId}` + (userCardBoard ? `?board_id=${userCardBoard}` : ""));
    } catch (e) {
      if (pop) pop.replaceChildren(h("p", { class: "muted small m0" }, e.message));
      return;
    }
    if (!pop || popFor !== anchor) return;
    const stats = u.stats ? h("dl", { class: "user-pop-stats" },
      h("div", null, h("dt", null, "В работе"), h("dd", null, String(u.stats.active))),
      h("div", null, h("dt", null, "Закрыто"), h("dd", null, String(u.stats.closed))),
      h("div", null, h("dt", null, "Создал(а)"), h("dd", null, String(u.stats.created)))) : null;
    const actions = userCardActions ? userCardActions(u) : [];
    pop.replaceChildren(
      h("button", { type: "button", class: "icon-btn user-pop-close", "aria-label": "Закрыть", onclick: closeUserCard }, "✕"),
      h("div", { class: "user-pop-head" },
        avatarEl({ name: u.name, avatar: u.avatar }, "avatar-lg"),
        h("div", { class: "user-pop-id" },
          h("strong", null, u.name),
          h("span", { class: "muted small" }, "@" + u.username),
          h("div", { class: "row-gap user-pop-roles" },
            u.board_role ? roleBadge(u.board_role) : roleBadge(u.role),
            u.is_active ? null : h("span", { class: "pill" }, "Отключён")))),
      u.boards.length ? h("div", { class: "user-pop-boards" },
        h("span", { class: "muted small" }, "Доски:"),
        ...u.boards.map((b) => h("span", { class: "pill", title: b.role_label }, `${b.name} · ${b.role_label}`))) : null,
      stats,
      h("p", { class: "muted small m0" },
        u.last_login_at ? `Заходил(а): ${relDate(u.last_login_at)}` : "Ещё не заходил(а)",
        u.discord ? " · Discord" : "", u.telegram ? " · Telegram" : ""),
      h("div", { class: "row-gap user-pop-actions" },
        ...actions,
        h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => {
          navigator.clipboard.writeText("@" + u.username).then(() => toast("Логин скопирован — вставьте в комментарий"), () => {});
        } }, "Скопировать @логин")));
    placePop(anchor);
    pop.focus();
  }

  function setupUserCards() {
    document.addEventListener("click", (e) => {
      const link = e.target.closest("[data-user-id]");
      if (link) {
        e.preventDefault();
        e.stopPropagation();
        openUserCard(link.dataset.userId, link);
        return;
      }
      if (pop && !e.target.closest(".user-pop")) closeUserCard();
    }, true);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && pop) { e.preventDefault(); e.stopPropagation(); closeUserCard(); }
    }, true);
    window.addEventListener("resize", closeUserCard);
    document.addEventListener("scroll", (e) => { if (pop && !e.target.closest?.(".user-pop")) closeUserCard(); }, true);
  }

  // ------------------------------------------------------------ старт

  document.addEventListener("DOMContentLoaded", () => {
    setupModals();
    setupBell();
    setupUserCards();
    $$("textarea[data-mentions]").forEach(attachMentions);
    (readJSON("flashes") || []).forEach(([cat, msg]) => toast(msg, cat === "error" ? "error" : "success"));
    const badge = document.getElementById("bell-count");
    if (badge && !badge.hidden) setUnread(parseInt(badge.textContent, 10) || 0);
  });

  return { $, $$, h, api, ApiError, busy, toast, BOOT, fullDate, relDate, timeEl, plural, rich, avatarEl,
           openModal, closeModal, confirmDialog, showFieldErrors, attachMentions, setMentionUsers, setUnread,
           roleBadge, roleLabel, userLink, openUserCard, closeUserCard, setUserCardContext };
})();
