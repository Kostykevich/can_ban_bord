/* Канбан-доска: доски, отрисовка, фильтры, перетаскивание, подробности, форма, автообновление.
   Поля карточки приходят с сервера (настройки доски): встроенные и свои. */
"use strict";

(() => {
  const { $, $$, h, api, busy, toast, BOOT, fullDate, timeEl, rich, openModal, closeModal,
          confirmDialog, showFieldErrors, attachMentions, setMentionUsers, setUnread, plural,
          roleBadge, userLink, setUserCardContext, closeUserCard } = V;

  const me = BOOT.user;
  const STEPS_TEMPLATE = "1. \n2. \n3. ";
  const LAST_BOARD_KEY = `vektor:board:${me.id}`;
  const EMPTY_FILTERS = () => ({ q: "", tab: "all", priority: "", category: "", assignee: "", tag: "", server: "" });
  const CUSTOM_LIMITS = { text: 300, textarea: 5000, url: 500, number: 30, datetime: 30, select: 60 };
  const PLACEHOLDERS = {
    build_version: "например 1.4.2", coords: "x, y, z", video_url: "https://youtu.be/… или Medal, Streamable",
  };

  const state = {
    boardId: null,
    data: null,
    view: "board",
    archive: null,
    filters: EMPTY_FILTERS(),
    mobileCol: null,
    dragging: null,
    detail: null,      // открытая карточка (полные данные)
    detailTab: "comments",
    formCard: null,    // редактируемая карточка; null — создание
    formTags: [],
    pendingFiles: [],
    moveCtx: null,
    loading: false,
    animate: true,     // следующая отрисовка доски — с анимацией появления
  };

  /* Плавная смена содержимого (если браузер умеет View Transitions) */
  function smooth(fn) {
    if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) document.startViewTransition(fn);
    else fn();
  }
  function skeleton() {
    return Array.from({ length: 4 }, () => h("div", { class: "skeleton-col", "aria-hidden": "true" },
      h("div", { class: "skeleton skeleton-title" }), h("div", { class: "skeleton skeleton-card" }),
      h("div", { class: "skeleton skeleton-card" }), h("div", { class: "skeleton skeleton-card" })));
  }

  const els = {
    board: $("#board"), archive: $("#archive"), tabs: $("#column-tabs"), count: $("#result-count"),
    search: $("#search"), reset: $("#reset-filters"), quick: $("#quick-tabs"),
    details: $("#details-modal"), detailsBody: $("#details-body"),
    formModal: $("#form-modal"), form: $("#card-form"), moveModal: $("#move-modal"), moveForm: $("#move-form"),
    imageModal: $("#image-modal"), dynamic: $("#cf-dynamic"), switcher: $("#board-switch"),
  };

  const draftKey = () => `vektor:draft:${me.id}:${state.boardId}`;

  // ------------------------------------------------------------ справочники

  let OPT = new Map();
  let COL = new Map();
  let USERS = new Map();
  const optName = (id) => (OPT.get(id) || {}).name || "—";
  const colOf = (card) => COL.get(card.column_id);
  const priorityLevel = (id) => (OPT.get(id) || {}).level || 1;
  const fields = () => (state.data ? state.data.fields.filter((f) => f.is_active) : []);
  const fieldByKey = (key) => state.data && state.data.fields.find((f) => f.key === key);
  const fieldActive = (key) => fields().some((f) => f.key === key);
  const roleOf = (user) => user && (USERS.get(user.id) || {}).board_role;

  function fieldValue(card, f) {
    if (f.builtin) return card[f.key];
    return (card.values || {})[String(f.id)] || "";
  }
  function displayValue(f, value) {
    if (value === null || value === undefined || value === "") return "";
    switch (f.input) {
      case "option": return optName(value);
      case "frequency": return value === "unknown" ? "" : BOOT.frequencies[value] || value;
      case "datetime": return String(value).replace("T", " ");
      case "checkbox": return value ? "Да" : "";
      default: return String(value);
    }
  }

  function transition(fromCol, toCol) {
    const isReturn = ["review", "done"].includes(fromCol.kind) && ["backlog", "work"].includes(toCol.kind);
    return { needsComment: isReturn, needsResolution: toCol.kind === "done", isReturn };
  }

  // ------------------------------------------------------------ загрузка

  function pickInitialBoard() {
    const fromUrl = Number(new URLSearchParams(location.search).get("board")) || null;
    let saved = null;
    try { saved = Number(localStorage.getItem(LAST_BOARD_KEY)) || null; } catch (e) { /* */ }
    const ids = (BOOT.boards || []).map((b) => b.id);
    for (const id of [BOOT.board_id, fromUrl, saved]) if (id && ids.includes(id)) return id;
    return ids[0] || null;
  }

  async function load({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api("GET", "/api/board" + (state.boardId ? `?board_id=${state.boardId}` : ""));
      const switched = !state.data || state.data.board.id !== data.board.id;
      state.data = data;
      state.boardId = data.board.id;
      try { localStorage.setItem(LAST_BOARD_KEY, String(data.board.id)); } catch (e) { /* */ }
      OPT = new Map();
      for (const list of Object.values(data.options)) for (const o of list) OPT.set(o.id, o);
      COL = new Map(data.columns.map((c) => [c.id, c]));
      USERS = new Map(data.users.map((u) => [u.id, u]));
      setMentionUsers(data.users);
      setUnread(data.unread);
      setUserCardContext(data.board.id, userCardActions);
      if (switched || !state.mobileCol || !COL.has(state.mobileCol)) state.mobileCol = data.columns[0] && data.columns[0].id;
      renderBoardHead();
      fillFilterSelects();
      render();
      if (state.view === "archive") loadArchive();
      checkDetailFresh();
    } catch (e) {
      if (!quiet) {
        toast(e.message, "error");
        els.board.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, "Не удалось загрузить доску"), e.message,
          e.status === 404 ? null : h("p", null, h("button", { type: "button", class: "btn", onclick: () => load() }, "Повторить"))));
      }
    } finally {
      state.loading = false;
    }
  }

  function renderBoardHead() {
    const d = state.data;
    $("#board-title").textContent = d.board.name;
    $("#board-subtitle").textContent = d.board.description || "";
    const roleBox = $("#board-my-role");
    roleBox.replaceChildren(...(d.my_role ? ["Ваша роль на доске: ", roleBadge(d.my_role)] : []));
    document.title = document.title.replace(/^(\(\d+\+?\) )?.*? · /, `$1${d.board.name} · `);
    // переключатель досок
    els.switcher.replaceChildren(...d.boards.map((b) => h("a", {
      href: `/?board=${b.id}`, class: "board-tab", "aria-current": b.id === d.board.id ? "page" : null,
      onclick: (e) => { if (e.ctrlKey || e.metaKey || e.button === 1) return; e.preventDefault(); switchBoard(b.id); },
    }, b.name)));
    els.switcher.hidden = d.boards.length < 2;
    const link = $("#board-settings-link");
    if (link) { link.hidden = !d.can_manage; link.href = `/admin/settings?board=${d.board.id}`; }
  }

  function switchBoard(id) {
    if (id === state.boardId) return;
    closeUserCard();
    state.boardId = id;
    state.filters = EMPTY_FILTERS();
    els.search.value = "";
    state.archive = null;
    $$("select[data-filter]").forEach((s) => { s.value = ""; s.classList.remove("active"); });
    state.animate = true;
    els.board.replaceChildren(...skeleton());
    writeUrl("/");
    load();
  }

  // Автообновление: раз в N секунд спрашиваем короткую «отметку» изменений
  function startPolling() {
    setInterval(async () => {
      if (document.visibilityState !== "visible" || state.dragging || !state.data) return;
      try {
        const s = await api("GET", `/api/board/stamp?board_id=${state.boardId}`);
        setUnread(s.unread);
        if (s.stamp && s.stamp !== state.data.stamp) await load({ quiet: true });
      } catch (e) { /* сеть пропала — попробуем позже */ }
    }, (BOOT.poll_seconds || 20) * 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && state.data) load({ quiet: true });
    });
  }

  // ------------------------------------------------------------ фильтры

  function readUrl() {
    const p = new URLSearchParams(location.search);
    for (const k of Object.keys(state.filters)) if (p.get(k)) state.filters[k] = p.get(k);
    if (p.get("view") === "archive") state.view = "archive";
    els.search.value = state.filters.q;
  }

  function writeUrl(path) {
    const p = new URLSearchParams();
    const onCard = !path && state.detail && els.details.open;
    const base = path || (onCard ? `/card/${state.detail.id}` : "/");
    if (state.boardId && base === "/") p.set("board", state.boardId);
    for (const [k, v] of Object.entries(state.filters)) if (v && !(k === "tab" && v === "all")) p.set(k, v);
    if (state.view === "archive") p.set("view", "archive");
    const qs = p.toString();
    history.replaceState(null, "", base + (qs ? "?" + qs : ""));
  }

  function fillSelect(select, items, first) {
    const value = select.value || state.filters[select.dataset.filter] || "";
    select.replaceChildren(h("option", { value: "" }, first), ...items.map(([v, label]) => h("option", { value: String(v) }, label)));
    select.value = items.some(([v]) => String(v) === value) ? value : "";
    if (select.dataset.filter) state.filters[select.dataset.filter] = select.value;
    select.classList.toggle("active", !!select.value);
  }

  function fillFilterSelects() {
    const o = state.data.options;
    fillSelect($("#f-priority"), o.priority.map((x) => [x.id, x.name]), "Любой приоритет");
    fillSelect($("#f-category"), o.category.map((x) => [x.id, x.name]), "Любая категория");
    fillSelect($("#f-server"), o.server.map((x) => [x.id, x.name]), "Любой сервер");
    $("#f-category").hidden = !fieldActive("category_id");
    $("#f-server").hidden = !fieldActive("server_id");
    fillSelect($("#f-assignee"), [["none", "Без исполнителя"], [String(me.id), "Я"],
      ...state.data.users.filter((u) => u.id !== me.id).map((u) => [u.id, u.name])], "Любой исполнитель");
    fillSelect($("#f-tag"), state.data.tags.map((t) => [t.id, `#${t.name}`]), "Любой тег");
  }

  function matches(card, { ignoreTab = false } = {}) {
    const f = state.filters;
    if (f.q) {
      const q = f.q.trim().toLowerCase();
      const idQ = q.replace(/^#/, "");
      const hay = `${card.title}\n${card.description}`.toLowerCase();
      const tagHit = card.tags.some((t) => t.name.toLowerCase().includes(idQ));
      if (!(String(card.id) === idQ || hay.includes(q) || tagHit)) return false;
    }
    if (f.priority && String(card.priority_id) !== f.priority) return false;
    if (f.category && fieldActive("category_id") && String(card.category_id) !== f.category) return false;
    if (f.server && fieldActive("server_id") && String(card.server_id) !== f.server) return false;
    if (f.tag && !card.tags.some((t) => String(t.id) === f.tag)) return false;
    if (f.assignee) {
      if (f.assignee === "none" ? card.assignee : String(card.assignee && card.assignee.id) !== f.assignee) return false;
    }
    if (!ignoreTab) return tabMatch(card, f.tab);
    return true;
  }

  function tabMatch(card, tab) {
    const col = colOf(card);
    switch (tab) {
      case "mine": return (card.author && card.author.id === me.id) || (card.assignee && card.assignee.id === me.id);
      case "assigned": return !!card.assignee && card.assignee.id === me.id;
      case "unassigned": return !card.assignee && col && col.kind !== "done";
      case "review": return !!col && col.kind === "review";
      default: return true;
    }
  }

  const filtersActive = () => {
    const f = state.filters;
    return !!(f.q || f.priority || f.category || f.server || f.tag || f.assignee || f.tab !== "all");
  };

  /* Кнопки в мини-профиле на странице доски */
  function userCardActions(u) {
    return [h("button", { type: "button", class: "btn btn-sm", onclick: () => {
      closeUserCard();
      if (els.details.open) closeModal(els.details);
      if (state.view === "archive") setView("board");
      state.filters.assignee = String(u.id);
      $("#f-assignee").value = String(u.id);
      $("#f-assignee").classList.add("active");
      writeUrl();
      render();
      toast(`Задачи: ${u.name}`, "info", 1800);
    } }, "Задачи на этой доске")];
  }

  // ------------------------------------------------------------ отрисовка доски

  function sortCards(cards, col) {
    const byPrio = (c) => priorityLevel(c.priority_id);
    return cards.slice().sort((a, b) => {
      if (col.kind === "done") return (b.closed_at || "").localeCompare(a.closed_at || "");
      return byPrio(b) - byPrio(a) || (b.created_at || "").localeCompare(a.created_at || "") || b.id - a.id;
    });
  }

  function render() {
    const data = state.data;
    if (!data) return;
    const visible = data.cards.filter((c) => matches(c));
    const total = data.cards.length;

    // счётчики и пустые состояния
    els.count.textContent = filtersActive()
      ? `Найдено: ${visible.length} из ${total} ${plural(total, "задачи", "задач", "задач")} на доске`
      : `Всего на доске: ${total} ${plural(total, "задача", "задачи", "задач")}`;
    els.reset.hidden = !filtersActive();
    $$("button[data-tab]", els.quick).forEach((b) => {
      const tab = b.dataset.tab;
      b.setAttribute("aria-pressed", String(state.filters.tab === tab));
      const n = data.cards.filter((c) => matches(c, { ignoreTab: true }) && tabMatch(c, tab)).length;
      let span = $(".n", b);
      if (!span) { span = h("span", { class: "n" }); b.append(span); }
      span.textContent = String(n);
    });

    if (!data.columns.length) {
      els.board.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, "На доске нет колонок"),
        data.can_manage ? h("a", { href: `/admin/settings?board=${data.board.id}` }, "Создайте их в настройках доски")
          : "Попросите руководителя доски создать колонки."));
      els.tabs.replaceChildren();
      return;
    }

    const byCol = new Map(data.columns.map((c) => [c.id, []]));
    for (const card of visible) if (byCol.has(card.column_id)) byCol.get(card.column_id).push(card);

    els.board.replaceChildren(...data.columns.map((col, ci) => columnEl(col, sortCards(byCol.get(col.id), col),
      data.cards.filter((c) => c.column_id === col.id).length, ci)));
    if (state.animate) {
      state.animate = false;
      els.board.classList.remove("enter");
      void els.board.offsetWidth;  // перезапуск анимации
      els.board.classList.add("enter");
      clearTimeout(render.timer);
      render.timer = setTimeout(() => els.board.classList.remove("enter"), 1600);
    }

    // вкладки колонок для телефона
    els.tabs.replaceChildren(...data.columns.map((col) => h("button", {
      type: "button", role: "tab", "aria-selected": String(col.id === state.mobileCol),
      onclick: () => { state.mobileCol = col.id; smooth(render); },
    }, `${col.name} · ${byCol.get(col.id).length}`)));
    els.tabs.setAttribute("role", "tablist");

    if (visible.length === 0 && total > 0 && filtersActive()) {
      els.count.textContent = "Ничего не найдено. Измените запрос или сбросьте фильтры.";
    }
  }

  function columnEl(col, cards, totalInCol, ci = 0) {
    const over = col.wip_limit && totalInCol > col.wip_limit;
    const filtered = filtersActive() && cards.length !== totalInCol;
    const countText = (filtered ? `${cards.length} из ${totalInCol}` : String(totalInCol)) + (col.wip_limit ? ` / ${col.wip_limit}` : "");
    const body = h("div", { class: "column-body" },
      cards.length ? cards.map((c, i) => { const el = cardEl(c); el.style.setProperty("--i", Math.min(i, 10)); return el; })
        : h("div", { class: "column-empty" }, filtered ? "Нет подходящих карточек" : emptyText(col)));
    const el = h("section", {
      class: "column" + (col.id === state.mobileCol ? " mobile-active" : ""), "aria-labelledby": `col-${col.id}`,
      dataset: { col: col.id }, style: `--ci:${ci}`,
    },
      h("header", { class: "column-head" },
        h("span", { class: `column-kind k-${col.kind}`, "aria-hidden": "true" }),
        h("h2", { class: "column-title", id: `col-${col.id}` }, col.name),
        h("span", { class: "column-count" + (over ? " over" : ""), title: col.wip_limit ? `Лимит колонки: ${col.wip_limit}` : null,
          "aria-label": `карточек: ${countText}${over ? ", лимит превышен" : ""}` }, countText)),
      body);

    el.addEventListener("dragover", (e) => {
      if (!state.dragging || !el.classList.contains("drop-ok")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("drop-over");
    });
    el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("drop-over"); });
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drop-over");
      const card = state.dragging;
      if (card && el.classList.contains("drop-ok")) requestMove(card, col);
    });
    return el;
  }

  function emptyText(col) {
    return { backlog: "Новых задач нет", work: "Никто ничего не делает", review: "Нечего проверять", done: "Пока ничего не закрыто" }[col.kind] || "Пусто";
  }

  const ICON_COMMENT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/></svg>';
  const ICON_IMAGE = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="m3 16 5-5 5 5 3-3 5 5" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
  function svg(markup) { const s = h("span", { class: "card-meta-icon" }); s.innerHTML = markup; return s.firstChild; } // статичная разметка, без пользовательских данных

  function prioEl(id) {
    const level = priorityLevel(id);
    return h("span", { class: "prio", dataset: { level } },
      h("span", { class: "prio-bars", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i")), optName(id));
  }

  /* Аватарка на карточке доски: по клику — мини-профиль (карточка при этом не открывается) */
  function faceAvatar(user, label) {
    if (!user) return null;
    const role = roleOf(user);
    return h("button", { type: "button", class: "face-avatar", dataset: { userId: user.id },
      title: `${label}: ${user.name}${role ? " · " + V.roleLabel(role) : ""}` }, V.avatarEl(user, "avatar-xs"));
  }

  function cardEl(card) {
    const level = priorityLevel(card.priority_id);
    const col = colOf(card);
    const onCard = fields().filter((f) => f.show_on_card);
    const category = onCard.find((f) => f.key === "category_id");
    const pills = onCard.filter((f) => f.key !== "category_id").map((f) => {
      const v = displayValue(f, fieldValue(card, f));
      if (!v) return null;
      return h("span", { class: "field-pill", title: f.label }, f.input === "checkbox" ? f.label : `${f.label}: ${v}`);
    }).filter(Boolean);
    const authorRole = roleOf(card.author);

    const el = h("div", {
      class: `card lvl-${level}`, role: "button", tabindex: "0", draggable: "true",
      "aria-label": `#${card.id} ${card.title}. Приоритет: ${optName(card.priority_id)}. ${card.assignee ? "Исполнитель: " + card.assignee.name : "Без исполнителя"}`,
      dataset: { id: card.id },
    },
      h("div", { class: "card-top" },
        h("span", { class: "card-id" }, `#${card.id}`),
        prioEl(card.priority_id),
        category && card.category_id ? h("span", { class: "card-cat" }, "· " + optName(card.category_id)) : null,
        col && col.kind === "done" && card.resolution_id ? h("span", { class: "pill" }, optName(card.resolution_id)) : null),
      h("p", { class: "card-title" }, card.title),
      card.description ? h("p", { class: "card-desc" }, card.description) : null,
      pills.length ? h("div", { class: "card-fields" }, pills) : null,
      card.tags.length ? h("div", { class: "card-tags" }, card.tags.map((t) => h("span", { class: "tag" }, "#" + t.name))) : null,
      h("div", { class: "card-foot" },
        card.author ? h("span", { class: "card-person" }, faceAvatar(card.author, "Автор"),
          h("span", { class: "card-person-name" }, card.author.name),
          authorRole ? roleBadge(authorRole, true) : null) : h("span", { class: "card-meta" }, "Автор неизвестен"),
        card.assignee ? h("span", { class: "card-person", title: "Исполнитель" }, h("span", { "aria-hidden": "true" }, "→"),
          faceAvatar(card.assignee, "Исполнитель"), h("span", { class: "card-person-name" }, card.assignee.name)) : null,
        h("span", { class: "spacer" }),
        card.reopen_count ? h("span", { class: "card-meta card-returns", title: "Сколько раз возвращали в работу" }, `↩ ${card.reopen_count}`) : null,
        card.attachment_count ? h("span", { class: "card-meta", title: "Скриншоты" }, svg(ICON_IMAGE), card.attachment_count) : null,
        card.comment_count ? h("span", { class: "card-meta", title: "Комментарии" }, svg(ICON_COMMENT), card.comment_count) : null,
        timeEl(card.created_at)));

    el.addEventListener("click", (e) => { if (!e.target.closest("[data-user-id]")) openCard(card.id, el); });
    el.addEventListener("keydown", (e) => {
      if (e.target !== el) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCard(card.id, el); }
    });
    el.addEventListener("dragstart", (e) => {
      state.dragging = card;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(card.id));
      requestAnimationFrame(() => el.classList.add("dragging"));
      $$(".column", els.board).forEach((c) => {
        const target = COL.get(Number(c.dataset.col));
        const ok = target && target.id !== card.column_id && target.can_enter;
        c.classList.add(ok ? "drop-ok" : "drop-no");
      });
    });
    el.addEventListener("dragend", () => {
      state.dragging = null;
      el.classList.remove("dragging");
      $$(".column", els.board).forEach((c) => c.classList.remove("drop-ok", "drop-no", "drop-over"));
    });
    return el;
  }

  // ------------------------------------------------------------ перемещение

  function requestMove(card, toCol, opener) {
    const fromCol = colOf(card);
    if (!fromCol || fromCol.id === toCol.id) return;
    if (!toCol.can_enter) {
      toast(`Ваша роль не может переносить карточки в «${toCol.name}».`, "error");
      return;
    }
    const rules = transition(fromCol, toCol);
    if (rules.needsComment || rules.needsResolution) {
      openMoveModal(card, fromCol, toCol, rules, opener);
    } else {
      doMove(card.id, fromCol.id, toCol.id, {});
    }
  }

  async function doMove(cardId, fromId, toId, extra) {
    const node = $(`.card[data-id="${cardId}"]`, els.board);
    node && node.classList.add("pending");
    try {
      const card = await api("POST", `/api/cards/${cardId}/move`, { column_id: toId, from_column_id: fromId, ...extra });
      replaceCard(card);
      toast(`Карточка #${cardId} перенесена в «${COL.get(toId).name}»`);
      if (state.detail && state.detail.id === cardId && els.details.open) renderDetails(card);
      flashCard(cardId);
      return card;
    } catch (e) {
      node && node.classList.remove("pending");  // карточка остаётся на месте
      toast(e.message, "error");
      if (e.status === 409) load({ quiet: true });
      throw e;
    }
  }

  function replaceCard(full) {
    const cards = state.data.cards;
    const i = cards.findIndex((c) => c.id === full.id);
    const col = COL.get(full.column_id);
    const visibleOnBoard = !full.archived_at && col && full.board_id === state.boardId;
    if (i >= 0 && visibleOnBoard) cards[i] = full;
    else if (i >= 0) cards.splice(i, 1);
    else if (visibleOnBoard) cards.push(full);
    render();
    // получим свежую «отметку», чтобы автообновление не перерисовывало лишний раз
    api("GET", `/api/board/stamp?board_id=${state.boardId}`).then((s) => { state.data.stamp = s.stamp; }).catch(() => {});
  }

  function flashCard(id) {
    requestAnimationFrame(() => {
      const node = $(`.card[data-id="${id}"]`, els.board);
      if (node) { node.classList.add("flash"); setTimeout(() => node.classList.remove("flash"), 1300); }
    });
  }

  function openMoveModal(card, fromCol, toCol, rules, opener) {
    state.moveCtx = { card, fromCol, toCol, rules };
    $("#move-title").textContent = rules.needsResolution ? "Закрыть задачу" : rules.isReturn ? "Вернуть задачу" : "Перенос карточки";
    $("#move-desc").textContent = `#${card.id} «${card.title}»: ${fromCol.name} → ${toCol.name}`;
    $("#move-error").hidden = true;
    const resField = $("#move-resolution-field");
    resField.hidden = !rules.needsResolution;
    const res = $("#move-resolution");
    res.replaceChildren(h("option", { value: "" }, "Выберите…"),
      ...state.data.options.resolution.filter((o) => o.is_active).map((o) => h("option", { value: o.id }, o.name)));
    const label = $("#move-comment-label");
    label.textContent = rules.needsComment ? "Почему возвращаете? (обязательно)" : "Комментарий (необязательно)";
    const comment = $("#move-comment");
    comment.value = "";
    comment.required = rules.needsComment;
    comment.placeholder = rules.needsComment ? "Что всё ещё не работает? Можно упомянуть @логин" : "";
    $("#move-submit").textContent = rules.needsResolution ? "Закрыть" : rules.isReturn ? "Вернуть в работу" : "Перенести";
    openModal(els.moveModal, opener);
    (rules.needsResolution ? res : comment).focus();
  }

  els.moveForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const ctx = state.moveCtx;
    if (!ctx) return;
    const comment = $("#move-comment").value.trim();
    const resolution = $("#move-resolution").value;
    const err = $("#move-error");
    if (ctx.rules.needsResolution && !resolution) { err.textContent = "Выберите резолюцию."; err.hidden = false; return; }
    if (ctx.rules.needsComment && !comment) { err.textContent = "Напишите причину возврата."; err.hidden = false; $("#move-comment").focus(); return; }
    busy($("#move-submit"), async () => {
      try {
        await doMove(ctx.card.id, ctx.fromCol.id, ctx.toCol.id, { comment, resolution_id: resolution || null });
        closeModal(els.moveModal);
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      }
    });
  });

  // ------------------------------------------------------------ подробности карточки

  async function openCard(id, opener) {
    try {
      const card = await api("GET", `/api/cards/${id}`);
      if (card.board_id !== state.boardId) {
        // карточка с другой доски (например, из уведомления) — переключаемся на её доску
        state.boardId = card.board_id;
        state.filters = EMPTY_FILTERS();
        await load({ quiet: true });
      }
      state.detail = card;
      state.detailTab = "comments";
      renderDetails(card);
      openModal(els.details, opener);
      writeUrl(`/card/${id}`);
      $("#details-title", els.details)?.focus();
    } catch (e) {
      toast(e.status === 404 ? `Карточка #${id} не найдена` : e.message, "error");
      if (e.status === 404) writeUrl("/");
    }
  }

  els.details.addEventListener("close", () => { closeUserCard(); state.detail = null; writeUrl("/"); });

  function section(title, content) {
    if (content === null || content === undefined || content === "") return null;
    return h("section", { class: "details-section" }, h("h3", null, title),
      typeof content === "string" ? h("p", { class: "prewrap" }, rich(content)) : content);
  }

  /* Поле в правой колонке свойств */
  function metaField(card, f) {
    const raw = fieldValue(card, f);
    const text = displayValue(f, raw);
    if (!text) return null;
    if (f.key === "coords") {
      return [dt(f.label), h("dd", null, h("code", null, text), " ",
        h("button", { type: "button", class: "link-btn small", onclick: () => copyText(text, "Координаты скопированы") }, "копировать"))];
    }
    return [dt(f.label), h("dd", null, text)];
  }

  /* Поле в основной колонке: длинный текст или ссылка */
  function mainField(card, f) {
    const value = fieldValue(card, f);
    if (!value) return null;
    if (f.input === "url") {
      return section(f.label, h("p", { class: "break" }, h("a", { href: value, target: "_blank", rel: "noopener noreferrer nofollow" }, value)));
    }
    return section(f.label, value);
  }
  const isMainField = (f) => f.input === "textarea" || f.input === "url";

  function personRow(label, user, fallback) {
    return [dt(label), h("dd", null, user ? userLink(user, { role: roleOf(user) }) : fallback)];
  }

  function renderDetails(card) {
    state.detail = card;
    const p = card.permissions;
    const col = COL.get(card.column_id);
    const active = fields();

    const actions = h("div", { class: "details-actions" });
    for (const mv of p.moves) {
      const target = COL.get(mv.column_id);
      if (!target) continue;
      actions.append(h("button", {
        type: "button", class: "btn btn-sm" + (target.kind === "done" ? " btn-primary" : ""),
        onclick: (e) => requestMove(card, target, e.currentTarget),
      }, (mv.is_return ? "↩ " : "→ ") + target.name));
    }
    if (p.can_take && !card.archived_at) actions.append(h("button", { type: "button", class: "btn btn-sm", onclick: (e) => assign(card, me.id, e.currentTarget) }, "Взять себе"));
    if (p.can_release) actions.append(h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: (e) => assign(card, null, e.currentTarget) }, "Снять с себя"));
    if (p.editable.length) actions.append(h("button", { type: "button", class: "btn btn-sm", onclick: (e) => openForm(card, e.currentTarget) }, "Редактировать"));
    actions.append(h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => copyLink(card.id) }, "Скопировать ссылку"));
    if (p.can_archive) {
      if (card.archived_at) {
        actions.append(h("button", { type: "button", class: "btn btn-sm", onclick: (e) => archiveAction(card, "restore", e.currentTarget) }, "Восстановить"));
        actions.append(h("button", { type: "button", class: "btn btn-sm btn-danger", onclick: () => deleteCard(card) }, "Удалить навсегда"));
      } else {
        actions.append(h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => archiveAction(card, "archive") }, "В архив"));
      }
    }

    // вложения
    const thumbs = h("ul", { class: "thumbs" }, card.attachments.map((a) => h("li", { class: "thumb" },
      h("img", { src: a.url, alt: a.name, loading: "lazy", onclick: () => showImage(a.url, a.name) }),
      a.can_delete ? h("button", { type: "button", class: "rm", "aria-label": `Удалить ${a.name}`, onclick: () => deleteAttachment(a) }, "✕") : null)));
    const fileInput = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", multiple: true, hidden: true,
      onchange: (e) => { uploadToCard(card.id, Array.from(e.target.files)); e.target.value = ""; } });
    const attachBlock = h("div", null,
      card.attachments.length ? thumbs : h("p", { class: "muted small" }, "Скриншотов нет"),
      p.can_upload ? h("p", { class: "small muted" },
        h("button", { type: "button", class: "btn btn-sm", onclick: () => fileInput.click() }, "Добавить скриншот"),
        " или Ctrl + V прямо в этом окне", fileInput) : null);

    // комментарии и история
    const tabs = h("div", { class: "tabs-mini", role: "tablist" },
      tabBtn("comments", `Комментарии (${card.comments.length})`), tabBtn("history", "История"));
    const feed = state.detailTab === "comments" ? commentsEl(card) : historyEl(card);

    const assigneeEl = p.can_assign_any && !card.archived_at
      ? h("div", { class: "assignee-pick" },
          card.assignee ? userLink(card.assignee, { role: roleOf(card.assignee) }) : null,
          h("select", { "aria-label": "Исполнитель", onchange: (e) => assign(card, e.target.value ? Number(e.target.value) : null, e.target) },
            h("option", { value: "" }, "— не назначен —"),
            state.data.users.map((u) => h("option", { value: u.id, selected: card.assignee && card.assignee.id === u.id },
              u.name + (u.board_role ? ` · ${V.roleLabel(u.board_role)}` : "")))))
      : (card.assignee ? userLink(card.assignee, { role: roleOf(card.assignee) }) : "не назначен");

    const meta = h("dl", { class: "meta-list" },
      dt("Колонка"), h("dd", null, col ? col.name : "—"),
      card.resolution_id ? [dt("Резолюция"), h("dd", null, optName(card.resolution_id))] : null,
      dt("Приоритет"), h("dd", null, prioEl(card.priority_id)),
      active.filter((f) => !isMainField(f)).map((f) => metaField(card, f)),
      personRow("Автор", card.author, "Автор неизвестен"),
      dt("Исполнитель"), h("dd", null, assigneeEl),
      card.tags.length ? [dt("Теги"), h("dd", { class: "card-tags" }, card.tags.map((t) => h("span", { class: "tag" }, "#" + t.name)))] : null,
      dt("Создана"), h("dd", { title: fullDate(card.created_at) }, fullDate(card.created_at)),
      dt("Изменена"), h("dd", null, timeEl(card.updated_at)),
      card.closed_at ? [dt("Закрыта"), h("dd", null, fullDate(card.closed_at))] : null,
      card.reopen_count ? [dt("Возвратов"), h("dd", null, String(card.reopen_count))] : null);

    els.detailsBody.replaceChildren(
      h("header", { class: "modal-head" },
        h("div", { class: "modal-head-main" },
          h("div", { class: "details-top" },
            h("span", { class: "card-id" }, `#${card.id}`),
            h("span", { class: "pill" }, state.data.board.name),
            col ? h("span", { class: "pill" + (col.kind === "done" ? " pill-strong" : "") }, col.name) : null,
            card.archived_at ? h("span", { class: "pill" }, "В архиве") : null,
            p.role ? h("span", { class: "details-myrole" }, "вы: ", roleBadge(p.role)) : null),
          h("h2", { id: "details-title", tabindex: "-1" }, card.title),
          h("div", { class: "details-people" },
            card.author ? h("span", null, h("span", { class: "muted" }, "Автор "), userLink(card.author, { role: roleOf(card.author) })) : null,
            card.assignee ? h("span", null, h("span", { class: "muted" }, "Исполнитель "), userLink(card.assignee, { role: roleOf(card.assignee) })) : null)),
        h("button", { type: "button", class: "icon-btn", "data-close": true, "aria-label": "Закрыть" }, "✕")),
      h("div", { id: "details-notice" }),
      actions,
      h("div", { class: "details-grid" },
        h("div", { class: "details-main" },
          section("Описание", card.description),
          active.filter(isMainField).map((f) => mainField(card, f)),
          section("Скриншоты", attachBlock),
          h("section", { class: "details-section" }, tabs, feed)),
        h("aside", { class: "side", "aria-label": "Свойства карточки" }, meta)));

    const ta = $("textarea", els.detailsBody);
    if (ta) attachMentions(ta);
  }

  const dt = (t) => h("dt", null, t);

  function tabBtn(key, label) {
    return h("button", { type: "button", role: "tab", "aria-selected": String(state.detailTab === key),
      onclick: () => { state.detailTab = key; renderDetails(state.detail); $(`.tabs-mini [aria-selected="true"]`, els.detailsBody)?.focus(); } }, label);
  }

  function commentsEl(card) {
    const list = h("ul", { class: "feed" }, card.comments.length ? card.comments.map((c) => h("li", { class: "feed-item" },
      c.author ? h("button", { type: "button", class: "face-avatar", dataset: { userId: c.author.id }, "aria-label": `Профиль: ${c.author.name}` }, V.avatarEl(c.author)) : V.avatarEl(null),
      h("div", { class: "feed-body" },
        h("div", { class: "feed-head" },
          c.author ? userLink(c.author, { avatar: false, role: roleOf(c.author) }) : h("strong", null, "Удалённый пользователь"),
          card.author && c.author && c.author.id === card.author.id ? h("span", { class: "muted small" }, "автор карточки") : null,
          timeEl(c.created_at)),
        h("p", { class: "feed-text" }, rich(c.body)))))
      : h("li", { class: "muted small" }, "Комментариев пока нет. Будьте первым."));
    if (!card.permissions.can_comment) return list;
    const ta = h("textarea", { rows: 3, maxlength: 3000, "aria-label": "Новый комментарий", placeholder: "Написать комментарий… @логин — упомянуть человека",
      onkeydown: (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); } } });
    const submit = h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Отправить");
    const form = h("form", { class: "comment-form", novalidate: true },
      ta, h("div", { class: "row-gap" }, h("span", { class: "muted small" }, "Ctrl + Enter — отправить"), submit));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = ta.value.trim();
      if (!text) { toast("Комментарий пустой.", "error"); ta.focus(); return; }
      busy(submit, async () => {
        try {
          const updated = await api("POST", `/api/cards/${card.id}/comments`, { body: text });
          renderDetails(updated);
          replaceCard(updated);
          toast("Комментарий добавлен");
          $(".feed-item:last-child", els.detailsBody)?.scrollIntoView({ block: "nearest" });
        } catch (ex) {
          toast(ex.message, "error");  // текст остаётся в поле
        }
      });
    });
    return h("div", null, list, form);
  }

  const DEFAULT_LABELS = { title: "название", description: "описание", priority_id: "приоритет", tags: "теги" };
  function fieldLabel(key) {
    const f = fieldByKey(key);
    if (f) return f.label.toLowerCase();
    return DEFAULT_LABELS[key] || (BOOT.builtin_labels || {})[key] || "поле";
  }
  const ACTION_TEXT = {
    created: (x) => `создал(а) карточку в «${x.new_value}»`,
    moved: (x) => `перенёс(ла): ${x.old_value} → ${x.new_value}`,
    resolved: (x) => `резолюция: ${x.new_value}`,
    assigned: (x) => x.new_value === "никто" ? `снял(а) исполнителя (${x.old_value})` : `исполнитель: ${x.new_value}`,
    edited: (x) => {
      const what = fieldLabel(x.field);
      return x.old_value !== null && x.new_value !== null ? `изменил(а) ${what}: ${x.old_value} → ${x.new_value}` : `изменил(а) ${what}`;
    },
    attachment: (x) => `добавил(а) скриншот ${x.new_value}`,
    attachment_deleted: (x) => `удалил(а) скриншот ${x.old_value}`,
    archived: () => "отправил(а) в архив",
    restored: () => "восстановил(а) из архива",
  };

  function historyEl(card) {
    if (!card.history.length) return h("p", { class: "muted small" }, "История пуста");
    return h("ul", { class: "feed" }, card.history.slice().reverse().map((x) => h("li", { class: "history-item" },
      x.user ? userLink(x.user, { size: "avatar-xs" }) : h("strong", null, "Система"), " ",
      (ACTION_TEXT[x.action] || (() => x.action))(x), timeEl(x.created_at))));
  }

  function checkDetailFresh() {
    if (!state.detail || !els.details.open) return;
    const fresh = state.data.cards.find((c) => c.id === state.detail.id);
    const box = $("#details-notice", els.detailsBody);
    if (!box || !fresh || fresh.version === state.detail.version && fresh.comment_count === state.detail.comments.length) return;
    const typing = $("textarea", els.detailsBody);
    if (!typing || !typing.value.trim()) {
      // никто ничего не печатает — тихо обновляем
      api("GET", `/api/cards/${state.detail.id}`).then((c) => { if (els.details.open) renderDetails(c); }).catch(() => {});
      return;
    }
    box.replaceChildren(h("div", { class: "notice", role: "status" }, "Карточку изменил кто-то другой.",
      h("button", { type: "button", class: "link-btn", onclick: () => openCard(state.detail.id) }, "Обновить")));
  }

  async function assign(card, userId, btn) {
    await busy(btn && btn.tagName === "BUTTON" ? btn : null, async () => {
      try {
        const updated = await api("POST", `/api/cards/${card.id}/assign`, { assignee_id: userId });
        renderDetails(updated);
        replaceCard(updated);
        toast(userId === null ? "Исполнитель снят" : userId === me.id ? "Задача теперь ваша" : "Исполнитель назначен");
      } catch (e) {
        toast(e.message, "error");
        if (btn && btn.tagName === "SELECT") renderDetails(card);
      }
    });
  }

  async function archiveAction(card, action, btn) {
    if (action === "archive") {
      const ok = await confirmDialog(`Карточка #${card.id} пропадёт с доски, но останется в архиве. Её можно будет восстановить.`, "В архив", "Отправить в архив?");
      if (!ok) return;
    }
    await busy(btn, async () => {
      try {
        const updated = await api("POST", `/api/cards/${card.id}/${action}`);
        renderDetails(updated);
        replaceCard(updated);
        if (state.view === "archive") loadArchive();
        toast(action === "archive" ? "Карточка в архиве" : "Карточка восстановлена");
      } catch (e) { toast(e.message, "error"); }
    });
  }

  async function deleteCard(card) {
    const ok = await confirmDialog(`Карточка #${card.id} «${card.title}», её комментарии, история и скриншоты будут удалены безвозвратно.`,
      "Удалить навсегда", "Удалить навсегда?");
    if (!ok) return;
    try {
      await api("DELETE", `/api/cards/${card.id}`);
      closeModal(els.details);
      toast(`Карточка #${card.id} удалена`);
      loadArchive();
      load({ quiet: true });
    } catch (e) { toast(e.message, "error"); }
  }

  async function deleteAttachment(att) {
    const ok = await confirmDialog(`Удалить скриншот «${att.name}»?`, "Удалить", "Удалить скриншот?");
    if (!ok) return;
    try {
      const updated = await api("DELETE", `/api/attachments/${att.id}`);
      renderDetails(updated);
      replaceCard(updated);
      toast("Скриншот удалён");
    } catch (e) { toast(e.message, "error"); }
  }

  async function uploadFiles(cardId, files) {
    let last = null, ok = 0;
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file, file.name || "screenshot.png");
      try { last = await api("POST", `/api/cards/${cardId}/attachments`, fd, true); ok++; }
      catch (e) { toast(`${file.name || "Скриншот"}: ${e.message}`, "error"); }
    }
    return { last, ok };
  }

  async function uploadToCard(cardId, files) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    toast(`Загружаю ${images.length} ${plural(images.length, "скриншот", "скриншота", "скриншотов")}…`, "info", 2000);
    const { last, ok } = await uploadFiles(cardId, images);
    if (last) {
      if (state.detail && state.detail.id === cardId) renderDetails(last);
      replaceCard(last);
    }
    if (ok) toast(`Загружено: ${ok}`);
  }

  function showImage(url, name) {
    const img = $("#image-full");
    img.src = url;
    img.alt = name || "Скриншот";
    openModal(els.imageModal);
  }

  function copyText(text, message) {
    navigator.clipboard.writeText(text).then(() => toast(message), () => toast("Не удалось скопировать", "error"));
  }
  function copyLink(id) { copyText(`${location.origin}/card/${id}`, "Ссылка скопирована — можно вставить в Discord"); }

  // ------------------------------------------------------------ форма создания / редактирования

  const F = (name) => els.form.elements[name];
  const CORE_FIELDS = ["title", "description", "priority_id"];
  const formKeys = () => [...CORE_FIELDS, ...fields().map((f) => f.key)];

  /* Поля доски в форме: строятся по настройкам (встроенные и свои) */
  function renderFormFields(card) {
    const o = state.data.options;
    const optList = (list, current) => list.filter((x) => x.is_active || x.id === current)
      .map((x) => h("option", { value: x.id }, x.name + (x.is_active ? "" : " (отключено)")));
    els.dynamic.replaceChildren(...fields().map((f) => {
      const id = `cf-${f.key}`;
      const req = f.required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : null;
      let input;
      const limit = f.builtin ? (BOOT.limits[f.key] || 300) : CUSTOM_LIMITS[f.input] || 300;
      switch (f.input) {
        case "textarea":
          input = h("textarea", { id, name: f.key, maxlength: limit, rows: f.key === "steps" ? 4 : 3, "data-mentions": f.key === "steps" ? "" : null });
          break;
        case "option":
          input = h("select", { id, name: f.key }, optList(o[f.option_kind] || [], card && card[f.key]));
          break;
        case "frequency":
          input = h("select", { id, name: f.key }, Object.entries(BOOT.frequencies).map(([v, l]) => h("option", { value: v }, l)));
          break;
        case "select": {
          // старое значение, которого уже нет в списке вариантов, не теряем при редактировании
          const current = card ? fieldValue(card, f) : "";
          const choices = current && !f.choices.includes(current) ? [...f.choices, current] : f.choices;
          input = h("select", { id, name: f.key }, h("option", { value: "" }, f.required ? "Выберите…" : "—"),
            choices.map((c) => h("option", { value: c }, c)));
          break;
        }
        case "checkbox":
          input = h("input", { id, name: f.key, type: "checkbox", value: "1" });
          break;
        case "datetime":
          input = h("input", { id, name: f.key, type: "datetime-local" });
          break;
        case "number":
          input = h("input", { id, name: f.key, inputmode: "decimal", maxlength: limit });
          break;
        case "url":
          input = h("input", { id, name: f.key, type: "url", maxlength: limit, placeholder: PLACEHOLDERS[f.key] || "https://…" });
          break;
        default:
          input = h("input", { id, name: f.key, maxlength: limit, placeholder: PLACEHOLDERS[f.key] || null });
      }
      const wide = f.input === "textarea" || f.input === "url";
      if (f.input === "checkbox") {
        return h("div", { class: "field field-check" + (wide ? " field-full" : "") },
          h("label", { class: "check", for: id }, input, f.label, req),
          f.hint ? h("p", { class: "muted small m0" }, f.hint) : null,
          h("p", { class: "hint", dataset: { errorFor: f.key } }));
      }
      return h("div", { class: "field" + (wide ? " field-full" : "") },
        h("label", { for: id }, f.label, req),
        input,
        f.hint ? h("p", { class: "muted small m0" }, f.hint) : null,
        h("p", { class: "hint", dataset: { errorFor: f.key } }));
    }));
    $$("textarea[data-mentions]", els.dynamic).forEach(attachMentions);
  }

  function setInput(name, value) {
    const el = F(name);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  }
  function getInput(name) {
    const el = F(name);
    if (!el) return "";
    return el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
  }

  function fillFormSelects(card) {
    const o = state.data.options;
    F("priority_id").replaceChildren(...o.priority.filter((x) => x.is_active || (card && x.id === card.priority_id))
      .map((x) => h("option", { value: x.id }, x.name + (x.is_active ? "" : " (отключено)"))));
    renderFormFields(card);
    const pick = (sel, name) => { if (!sel) return; const x = Array.from(sel.options).find((op) => op.textContent === name); if (x) sel.value = x.value; };
    if (!card) {
      pick(F("priority_id"), "Средний"); pick(F("category_id"), "Прочее"); pick(F("server_id"), "Тестовый");
      if (F("frequency")) F("frequency").value = "unknown";
    }

    const assigneeField = $("#cf-assignee-field");
    const role = state.data.my_role;
    const canPick = !card && (state.data.can_manage || role === "developer");
    assigneeField.hidden = !canPick;
    if (canPick) {
      const users = state.data.can_manage ? state.data.users : state.data.users.filter((u) => u.id === me.id);
      F("assignee_id").replaceChildren(h("option", { value: "" }, "— не назначать —"), ...users.map((u) => h("option", { value: u.id },
        (u.id === me.id ? `${u.name} (я)` : u.name) + (u.board_role ? ` · ${V.roleLabel(u.board_role)}` : ""))));
    }
    $("#tag-suggestions").replaceChildren(...state.data.tags.map((t) => h("option", { value: t.name })));
  }

  function openForm(card, opener) {
    state.formCard = card || null;
    state.pendingFiles = [];
    renderPendingFiles();
    els.form.reset();
    fillFormSelects(card);
    showFieldErrors(els.form, {});
    $("#form-error").hidden = true;
    $("#similar").hidden = true;
    $("#form-title").textContent = card ? `Редактирование #${card.id}` : `Новая карточка · ${state.data.board.name}`;
    $("#form-submit").textContent = card ? "Сохранить" : "Создать";
    $("#cf-files-field").hidden = !!card;
    $("#form-hint").textContent = card ? "Изменения увидят все участники доски" : "Черновик сохраняется автоматически";
    $("#draft-note").hidden = true;

    if (card) {
      const editable = new Set(card.permissions.editable);
      setInput("title", card.title); setInput("description", card.description); setInput("priority_id", card.priority_id);
      for (const name of CORE_FIELDS) F(name).disabled = !editable.has(name);
      for (const f of fields()) { setInput(f.key, fieldValue(card, f)); if (F(f.key)) F(f.key).disabled = !editable.has(f.key); }
      state.formTags = card.tags.map((t) => t.name);
      $("#cf-tag-input").disabled = !editable.has("tags");
    } else {
      for (const name of formKeys()) if (F(name)) F(name).disabled = false;
      $("#cf-tag-input").disabled = false;
      state.formTags = [];
      if (F("steps")) F("steps").value = STEPS_TEMPLATE;
      restoreDraft();
    }
    renderTags();
    openModal(els.formModal, opener);
    (card ? Array.from(els.form.querySelectorAll("input:not(:disabled), textarea:not(:disabled)"))[0] : F("title"))?.focus();
  }

  // черновик (у каждой доски свой)
  function saveDraft() {
    if (state.formCard || !els.formModal.open) return;
    const draft = { tags: state.formTags };
    for (const name of formKeys()) draft[name] = getInput(name);
    const empty = !draft.title && !draft.description && (!draft.steps || draft.steps === STEPS_TEMPLATE);
    try { empty ? localStorage.removeItem(draftKey()) : localStorage.setItem(draftKey(), JSON.stringify(draft)); } catch (e) { /* хранилище недоступно */ }
  }
  function restoreDraft() {
    let draft = null;
    try { draft = JSON.parse(localStorage.getItem(draftKey()) || "null"); } catch (e) { draft = null; }
    if (!draft) return;
    for (const name of formKeys()) {
      const el = F(name);
      if (!el || draft[name] === undefined) continue;
      if (el.tagName === "SELECT" && !Array.from(el.options).some((o) => o.value === String(draft[name]))) continue;
      setInput(name, draft[name]);
    }
    state.formTags = Array.isArray(draft.tags) ? draft.tags : [];
    $("#draft-note").hidden = false;
  }
  function clearDraft() { try { localStorage.removeItem(draftKey()); } catch (e) { /* */ } }
  $("#draft-clear").addEventListener("click", () => {
    clearDraft();
    openForm(null);
    toast("Форма очищена", "info");
  });
  els.form.addEventListener("input", () => { saveDraft(); });
  els.form.addEventListener("change", () => { saveDraft(); });

  // теги
  function renderTags() {
    const disabled = $("#cf-tag-input").disabled;
    $("#cf-tag-chips").replaceChildren(...state.formTags.map((t, i) => h("li", { class: "tag-chip" }, "#" + t,
      disabled ? null : h("button", { type: "button", "aria-label": `Убрать тег ${t}`, onclick: () => { state.formTags.splice(i, 1); renderTags(); saveDraft(); } }, "✕"))));
  }
  function addTag(raw) {
    const name = raw.replace(/^#/, "").trim().replace(/\s+/g, " ");
    if (!name) return;
    if (name.length > 30 || !/^[\p{L}\p{N}_\-+.# ]+$/u.test(name)) { toast("Тег: до 30 символов, буквы, цифры, пробел и - + . #", "error"); return; }
    if (state.formTags.length >= 10) { toast("Не больше 10 тегов", "error"); return; }
    if (!state.formTags.some((t) => t.toLowerCase() === name.toLowerCase())) {
      const existing = state.data.tags.find((t) => t.name.toLowerCase() === name.toLowerCase());
      state.formTags.push(existing ? existing.name : name);
    }
    renderTags();
    saveDraft();
  }
  const tagInput = $("#cf-tag-input");
  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTag(tagInput.value); tagInput.value = ""; }
    else if (e.key === "Backspace" && !tagInput.value && state.formTags.length) { state.formTags.pop(); renderTags(); saveDraft(); }
  });
  tagInput.addEventListener("change", () => { // выбор из подсказки datalist
    if (state.data.tags.some((t) => t.name === tagInput.value)) { addTag(tagInput.value); tagInput.value = ""; }
  });
  tagInput.addEventListener("blur", () => { if (tagInput.value.trim()) { addTag(tagInput.value); tagInput.value = ""; } });

  // похожие карточки (поиск дублей)
  let similarTimer = null;
  F("title").addEventListener("input", () => {
    clearTimeout(similarTimer);
    const q = F("title").value.trim();
    const box = $("#similar");
    if (q.length < 4) { box.hidden = true; return; }
    similarTimer = setTimeout(async () => {
      try {
        const res = await api("GET", `/api/similar?board_id=${state.boardId}&q=${encodeURIComponent(q)}&exclude=${state.formCard ? state.formCard.id : 0}`);
        if (!res.cards.length) { box.hidden = true; return; }
        box.replaceChildren(h("strong", null, "Похожие карточки — возможно, это дубль:"),
          h("ul", null, res.cards.map((c) => h("li", null, h("a", { href: `/card/${c.id}`, onclick: (e) => { e.preventDefault(); openCard(c.id); } },
            `#${c.id} ${c.title}`), h("span", { class: "muted" }, ` · ${c.column}`)))));
        box.hidden = false;
      } catch (e) { box.hidden = true; }
    }, 400);
  });

  // скриншоты в форме создания
  function renderPendingFiles() {
    $("#pending-files").replaceChildren(...state.pendingFiles.map((f, i) => h("li", { class: "thumb" },
      h("img", { src: f.url, alt: f.file.name || "Скриншот" }),
      h("button", { type: "button", class: "rm", "aria-label": "Убрать скриншот", onclick: () => { URL.revokeObjectURL(f.url); state.pendingFiles.splice(i, 1); renderPendingFiles(); } }, "✕"))));
  }
  function addPendingFiles(files) {
    for (const file of files) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) { toast(`${file.name || "Файл"}: только PNG, JPG, GIF или WebP`, "error"); continue; }
      if (file.size > 5 * 1024 * 1024) { toast(`${file.name || "Файл"}: больше 5 МБ`, "error"); continue; }
      if (state.pendingFiles.length >= 10) { toast("Не больше 10 скриншотов", "error"); break; }
      state.pendingFiles.push({ file, url: URL.createObjectURL(file) });
    }
    renderPendingFiles();
  }
  const dropzone = $("#dropzone");
  const fileInput = $("#file-input");
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener("change", () => { addPendingFiles(Array.from(fileInput.files)); fileInput.value = ""; });
  dropzone.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); dropzone.classList.add("over"); } });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
  dropzone.addEventListener("drop", (e) => { e.preventDefault(); dropzone.classList.remove("over"); addPendingFiles(Array.from(e.dataTransfer.files)); });

  // Ctrl+V: в форме создания — в список, в окне подробностей — сразу на карточку
  document.addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData ? e.clipboardData.files : []).filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    if (els.formModal.open && !state.formCard) { e.preventDefault(); addPendingFiles(files); toast("Скриншот добавлен", "info", 1800); }
    else if (els.details.open && state.detail && state.detail.permissions.can_upload) { e.preventDefault(); uploadToCard(state.detail.id, files); }
  });

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const card = state.formCard;
    const payload = { fields: {} };
    for (const name of CORE_FIELDS) if (!F(name).disabled) payload[name] = F(name).value;
    for (const f of fields()) {
      const el = F(f.key);
      if (!el || el.disabled) continue;
      if (f.builtin) payload[f.key] = getInput(f.key);
      else payload.fields[String(f.id)] = getInput(f.key);
    }
    if (!$("#cf-tag-input").disabled) {
      if (tagInput.value.trim()) { addTag(tagInput.value); tagInput.value = ""; }
      payload.tags = state.formTags;
    }
    // проверка до отправки — сервер всё равно проверит ещё раз
    const errors = {};
    if (!F("title").disabled && !(payload.title || "").trim()) errors.title = "Обязательное поле.";
    if (!F("description").disabled && !(payload.description || "").trim()) errors.description = "Обязательное поле.";
    for (const f of fields()) {
      const el = F(f.key);
      if (!el || el.disabled) continue;
      const v = (getInput(f.key) || "").trim();
      if (f.required && !v && !(f.key === "steps" && v === STEPS_TEMPLATE.trim())) errors[f.key] = "Обязательное поле.";
      if (v && f.input === "url" && !/^https?:\/\/\S+$/.test(v)) errors[f.key] = "Ссылка должна начинаться с http:// или https://";
      if (v && f.input === "number" && Number.isNaN(Number(v.replace(",", ".").replace(/\s/g, "")))) errors[f.key] = "Введите число.";
    }
    if (payload.steps !== undefined && payload.steps.trim() === STEPS_TEMPLATE.trim()) {
      payload.steps = "";
      if (fieldByKey("steps") && fieldByKey("steps").required) errors.steps = "Обязательное поле.";
    }
    showFieldErrors(els.form, errors);
    if (Object.keys(errors).length) return;
    if (!card && !$("#cf-assignee-field").hidden && F("assignee_id").value) payload.assignee_id = Number(F("assignee_id").value);

    const errBox = $("#form-error");
    errBox.hidden = true;
    busy($("#form-submit"), async () => {
      try {
        let saved;
        if (card) {
          payload.version = card.version;
          saved = await api("PATCH", `/api/cards/${card.id}`, payload);
          toast("Изменения сохранены");
        } else {
          payload.board_id = state.boardId;
          saved = await api("POST", "/api/cards", payload);
          clearDraft();
          if (state.pendingFiles.length) {
            const { last } = await uploadFiles(saved.id, state.pendingFiles.map((f) => f.file));
            if (last) saved = last;
            state.pendingFiles.forEach((f) => URL.revokeObjectURL(f.url));
            state.pendingFiles = [];
          }
          toast(`Карточка #${saved.id} создана`);
        }
        closeModal(els.formModal);
        replaceCard(saved);
        flashCard(saved.id);
        if (card && els.details.open) renderDetails(saved);
        if (!card) {
          // новый тег мог появиться — обновим справочники
          load({ quiet: true });
        }
      } catch (ex) {
        showFieldErrors(els.form, ex.fields);
        errBox.textContent = ex.message;  // введённый текст остаётся в форме
        errBox.hidden = false;
        errBox.scrollIntoView({ block: "nearest" });
      }
    });
  });

  // ------------------------------------------------------------ архив

  async function loadArchive() {
    els.archive.replaceChildren(h("p", { class: "muted" }, "Загрузка архива…"));
    try {
      const res = await api("GET", `/api/archive?board_id=${state.boardId}`);
      state.archive = res.cards;
      renderArchive();
    } catch (e) {
      els.archive.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, "Не удалось загрузить архив"), e.message));
    }
  }

  function renderArchive() {
    const q = state.filters.q.trim().toLowerCase().replace(/^#/, "");
    const cards = (state.archive || []).filter((c) => !q || String(c.id) === q || `${c.title}\n${c.description}`.toLowerCase().includes(q));
    els.count.textContent = `В архиве: ${cards.length}`;
    if (!cards.length) {
      els.archive.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, q ? "Ничего не найдено" : "Архив пуст"),
        `Сюда попадают закрытые задачи старше ${state.data.settings.done_visible_days || 14} дней и карточки, убранные руководителем доски.`));
      return;
    }
    els.archive.replaceChildren(h("ul", { class: "archive-list" }, cards.map((c, i) => h("li", { style: `--i:${Math.min(i, 20)}` },
      h("button", { type: "button", class: "archive-row", onclick: (e) => openCard(c.id, e.currentTarget) },
        h("span", { class: "card-id muted" }, `#${c.id}`),
        h("span", { class: "t" }, c.title),
        h("span", { class: "pill" }, c.archived_at ? "Архивирована" : optName(c.resolution_id)),
        h("span", { class: "muted small" }, V.relDate(c.archived_at || c.closed_at)))))));
  }

  function setView(view) {
    state.view = view;
    $$("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    const isArchive = view === "archive";
    els.board.hidden = isArchive;
    els.tabs.hidden = isArchive;
    els.archive.hidden = !isArchive;
    $(".quick-tabs").hidden = isArchive;
    $(".filters").hidden = isArchive;
    writeUrl();
    if (isArchive) loadArchive(); else { state.animate = true; render(); }
  }

  // ------------------------------------------------------------ события панели

  let searchTimer = null;
  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.q = els.search.value;
      writeUrl();
      state.view === "archive" ? renderArchive() : render();
    }, 120);
  });
  els.search.addEventListener("keydown", (e) => { if (e.key === "Escape" && els.search.value) { els.search.value = ""; els.search.dispatchEvent(new Event("input")); } });

  $$("select[data-filter]").forEach((sel) => sel.addEventListener("change", () => {
    state.filters[sel.dataset.filter] = sel.value;
    sel.classList.toggle("active", !!sel.value);
    writeUrl();
    state.animate = true;
    render();
  }));
  $$("button[data-tab]", els.quick).forEach((b) => b.addEventListener("click", () => {
    state.filters.tab = b.dataset.tab;
    writeUrl();
    state.animate = true;
    render();
  }));
  els.reset.addEventListener("click", () => {
    state.filters = EMPTY_FILTERS();
    els.search.value = "";
    $$("select[data-filter]").forEach((s) => { s.value = ""; s.classList.remove("active"); });
    writeUrl();
    render();
    toast("Фильтры сброшены", "info", 1800);
  });
  $$("[data-view]").forEach((b) => b.addEventListener("click", () => smooth(() => setView(b.dataset.view))));
  $("#create-btn").addEventListener("click", (e) => { if (state.data) openForm(null, e.currentTarget); });

  // Горячие клавиши (работают при любой раскладке)
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (e.code === "KeyN" && state.data) { e.preventDefault(); openForm(null, $("#create-btn")); }
    else if (e.code === "Slash") { e.preventDefault(); els.search.focus(); els.search.select(); }
  });

  // ------------------------------------------------------------ старт

  window.VBoard = { openCard };
  state.boardId = pickInitialBoard();
  readUrl();
  els.board.replaceChildren(...skeleton());
  if (!state.boardId) {
    els.board.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, "Вас ещё не добавили ни на одну доску"),
      "Напишите администратору или руководителю — он добавит вас в «Настройки → Участники»."));
  } else {
    load().then(() => {
      if (state.view === "archive") setView("archive");
      else writeUrl(BOOT.open_card ? `/card/${BOOT.open_card}` : undefined);
      if (BOOT.open_card) openCard(BOOT.open_card);
    });
    startPolling();
  }
})();
