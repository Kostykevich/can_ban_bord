/* Канбан-доска: отрисовка, фильтры, перетаскивание, подробности, форма, автообновление. */
"use strict";

(() => {
  const { $, $$, h, api, busy, toast, BOOT, fullDate, timeEl, rich, openModal, closeModal,
          confirmDialog, showFieldErrors, attachMentions, setMentionUsers, setUnread, plural } = V;

  const me = BOOT.user;
  const isAdmin = me.role === "admin";
  const DRAFT_KEY = `vektor:draft:${me.id}`;
  const STEPS_TEMPLATE = "1. \n2. \n3. ";

  const state = {
    data: null,
    view: "board",
    archive: null,
    filters: { q: "", tab: "all", priority: "", category: "", assignee: "", tag: "", server: "" },
    mobileCol: null,
    dragging: null,
    detail: null,      // открытая карточка (полные данные)
    detailTab: "comments",
    formCard: null,    // редактируемая карточка; null — создание
    formTags: [],
    pendingFiles: [],
    moveCtx: null,
    loading: false,
  };

  const els = {
    board: $("#board"), archive: $("#archive"), tabs: $("#column-tabs"), count: $("#result-count"),
    search: $("#search"), reset: $("#reset-filters"), quick: $("#quick-tabs"),
    details: $("#details-modal"), detailsBody: $("#details-body"),
    formModal: $("#form-modal"), form: $("#card-form"), moveModal: $("#move-modal"), moveForm: $("#move-form"),
    imageModal: $("#image-modal"),
  };

  // ------------------------------------------------------------ справочники

  const optMap = () => {
    const map = new Map();
    for (const list of Object.values(state.data.options)) for (const o of list) map.set(o.id, o);
    return map;
  };
  let OPT = new Map();
  let COL = new Map();
  const optName = (id) => (OPT.get(id) || {}).name || "—";
  const colOf = (card) => COL.get(card.column_id);
  const priorityLevel = (id) => (OPT.get(id) || {}).level || 1;

  function transition(fromCol, toCol) {
    const isReturn = ["review", "done"].includes(fromCol.kind) && ["backlog", "work"].includes(toCol.kind);
    return { needsComment: isReturn, needsResolution: toCol.kind === "done", isReturn };
  }

  // ------------------------------------------------------------ загрузка

  async function load({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api("GET", "/api/board");
      state.data = data;
      OPT = optMap();
      COL = new Map(data.columns.map((c) => [c.id, c]));
      setMentionUsers(data.users);
      setUnread(data.unread);
      fillFilterSelects();
      if (!state.mobileCol || !COL.has(state.mobileCol)) state.mobileCol = data.columns[0] && data.columns[0].id;
      render();
      if (state.view === "archive") loadArchive();
      checkDetailFresh();
    } catch (e) {
      if (!quiet) {
        toast(e.message, "error");
        els.board.replaceChildren(h("div", { class: "empty-state" }, h("strong", null, "Не удалось загрузить доску"), e.message,
          h("p", null, h("button", { type: "button", class: "btn", onclick: () => load() }, "Повторить"))));
      }
    } finally {
      state.loading = false;
    }
  }

  // Автообновление: раз в N секунд спрашиваем короткую «отметку» изменений
  function startPolling() {
    setInterval(async () => {
      if (document.visibilityState !== "visible" || state.dragging || !state.data) return;
      try {
        const s = await api("GET", "/api/board/stamp");
        setUnread(s.unread);
        if (s.stamp !== state.data.stamp) await load({ quiet: true });
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
    for (const [k, v] of Object.entries(state.filters)) if (v && !(k === "tab" && v === "all")) p.set(k, v);
    if (state.view === "archive") p.set("view", "archive");
    const base = path || (state.detail && els.details.open ? `/card/${state.detail.id}` : "/");
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
    if (f.category && String(card.category_id) !== f.category) return false;
    if (f.server && String(card.server_id) !== f.server) return false;
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
        isAdmin ? h("a", { href: "/admin/settings" }, "Создайте их в настройках") : "Попросите администратора создать колонки."));
      els.tabs.replaceChildren();
      return;
    }

    const byCol = new Map(data.columns.map((c) => [c.id, []]));
    for (const card of visible) if (byCol.has(card.column_id)) byCol.get(card.column_id).push(card);

    els.board.replaceChildren(...data.columns.map((col) => columnEl(col, sortCards(byCol.get(col.id), col),
      data.cards.filter((c) => c.column_id === col.id).length)));

    // вкладки колонок для телефона
    els.tabs.replaceChildren(...data.columns.map((col) => h("button", {
      type: "button", role: "tab", "aria-selected": String(col.id === state.mobileCol),
      onclick: () => { state.mobileCol = col.id; render(); },
    }, `${col.name} · ${byCol.get(col.id).length}`)));
    els.tabs.setAttribute("role", "tablist");

    if (visible.length === 0 && total > 0 && filtersActive()) {
      els.count.textContent = "Ничего не найдено. Измените запрос или сбросьте фильтры.";
    }
  }

  function columnEl(col, cards, totalInCol) {
    const over = col.wip_limit && totalInCol > col.wip_limit;
    const filtered = filtersActive() && cards.length !== totalInCol;
    const countText = (filtered ? `${cards.length} из ${totalInCol}` : String(totalInCol)) + (col.wip_limit ? ` / ${col.wip_limit}` : "");
    const body = h("div", { class: "column-body" },
      cards.length ? cards.map(cardEl)
        : h("div", { class: "column-empty" }, filtered ? "Нет подходящих карточек" : emptyText(col)));
    const el = h("section", {
      class: "column" + (col.id === state.mobileCol ? " mobile-active" : ""), "aria-labelledby": `col-${col.id}`,
      dataset: { col: col.id },
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

  function cardEl(card) {
    const level = priorityLevel(card.priority_id);
    const col = colOf(card);
    const el = h("div", {
      class: `card lvl-${level}`, role: "button", tabindex: "0", draggable: "true",
      "aria-label": `#${card.id} ${card.title}. Приоритет: ${optName(card.priority_id)}. ${card.assignee ? "Исполнитель: " + card.assignee.name : "Без исполнителя"}`,
      dataset: { id: card.id },
    },
      h("div", { class: "card-top" },
        h("span", { class: "card-id" }, `#${card.id}`),
        prioEl(card.priority_id),
        h("span", null, "· " + optName(card.category_id)),
        col && col.kind === "done" && card.resolution_id ? h("span", { class: "pill" }, optName(card.resolution_id)) : null),
      h("p", { class: "card-title" }, card.title),
      card.description ? h("p", { class: "card-desc" }, card.description) : null,
      card.tags.length ? h("div", { class: "card-tags" }, card.tags.map((t) => h("span", { class: "tag" }, "#" + t.name))) : null,
      h("div", { class: "card-foot" },
        h("span", { class: "card-meta", title: "Автор" }, card.author ? card.author.name : "Автор неизвестен"),
        card.assignee ? h("span", { class: "card-meta", title: "Исполнитель" }, "→ " + card.assignee.name) : null,
        h("span", { class: "spacer" }),
        card.reopen_count ? h("span", { class: "card-meta card-returns", title: "Сколько раз возвращали в работу" }, `↩ ${card.reopen_count}`) : null,
        card.attachment_count ? h("span", { class: "card-meta", title: "Скриншоты" }, svg(ICON_IMAGE), card.attachment_count) : null,
        card.comment_count ? h("span", { class: "card-meta", title: "Комментарии" }, svg(ICON_COMMENT), card.comment_count) : null,
        timeEl(card.created_at)));

    el.addEventListener("click", () => openCard(card.id, el));
    el.addEventListener("keydown", (e) => {
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
    const visibleOnBoard = !full.archived_at && col;
    if (i >= 0 && visibleOnBoard) cards[i] = full;
    else if (i >= 0) cards.splice(i, 1);
    else if (visibleOnBoard) cards.push(full);
    render();
    // получим свежую «отметку», чтобы автообновление не перерисовывало лишний раз
    api("GET", "/api/board/stamp").then((s) => { state.data.stamp = s.stamp; }).catch(() => {});
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

  els.details.addEventListener("close", () => { writeUrl("/"); state.detail = null; });

  function section(title, content) {
    if (content === null || content === undefined || content === "") return null;
    return h("section", { class: "details-section" }, h("h3", null, title),
      typeof content === "string" ? h("p", { class: "prewrap" }, rich(content)) : content);
  }

  function renderDetails(card) {
    state.detail = card;
    const p = card.permissions;
    const col = COL.get(card.column_id);
    const freq = BOOT.frequencies[card.frequency] || "—";

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

    const assigneeEl = isAdmin && !card.archived_at
      ? h("select", { "aria-label": "Исполнитель", onchange: (e) => assign(card, e.target.value ? Number(e.target.value) : null, e.target) },
          h("option", { value: "" }, "— не назначен —"),
          state.data.users.filter((u) => u.role !== "pending").map((u) => h("option", { value: u.id, selected: card.assignee && card.assignee.id === u.id }, u.name)))
      : (card.assignee ? card.assignee.name : "не назначен");

    const meta = h("dl", { class: "meta-list" },
      dt("Колонка"), h("dd", null, col ? col.name : "—"),
      card.resolution_id ? [dt("Резолюция"), h("dd", null, optName(card.resolution_id))] : null,
      dt("Приоритет"), h("dd", null, prioEl(card.priority_id)),
      dt("Категория"), h("dd", null, optName(card.category_id)),
      dt("Сервер"), h("dd", null, optName(card.server_id)),
      dt("Частота"), h("dd", null, freq),
      card.build_version ? [dt("Сборка"), h("dd", null, card.build_version)] : null,
      card.coords ? [dt("Координаты"), h("dd", null, h("code", null, card.coords), " ",
        h("button", { type: "button", class: "link-btn small", onclick: () => copyText(card.coords, "Координаты скопированы") }, "копировать"))] : null,
      card.static_id ? [dt("Static ID"), h("dd", null, card.static_id)] : null,
      card.occurred_at ? [dt("Произошло"), h("dd", null, card.occurred_at.replace("T", " ") + " МСК")] : null,
      dt("Автор"), h("dd", null, card.author ? card.author.name : "Автор неизвестен"),
      dt("Исполнитель"), h("dd", null, assigneeEl),
      card.tags.length ? [dt("Теги"), h("dd", { class: "card-tags" }, card.tags.map((t) => h("span", { class: "tag" }, "#" + t.name)))] : null,
      dt("Создана"), h("dd", { title: fullDate(card.created_at) }, fullDate(card.created_at)),
      dt("Изменена"), h("dd", null, timeEl(card.updated_at)),
      card.closed_at ? [dt("Закрыта"), h("dd", null, fullDate(card.closed_at))] : null,
      card.reopen_count ? [dt("Возвратов"), h("dd", null, String(card.reopen_count))] : null);

    els.detailsBody.replaceChildren(
      h("header", { class: "modal-head" },
        h("div", null,
          h("div", { class: "details-top" },
            h("span", { class: "card-id" }, `#${card.id}`),
            col ? h("span", { class: "pill" + (col.kind === "done" ? " pill-strong" : "") }, col.name) : null,
            card.archived_at ? h("span", { class: "pill" }, "В архиве") : null),
          h("h2", { id: "details-title", tabindex: "-1" }, card.title)),
        h("button", { type: "button", class: "icon-btn", "data-close": true, "aria-label": "Закрыть" }, "✕")),
      h("div", { id: "details-notice" }),
      actions,
      h("div", { class: "details-grid" },
        h("div", null,
          section("Описание", card.description),
          section("Шаги воспроизведения", card.steps),
          section("Ожидаемый результат", card.expected),
          section("Фактический результат", card.actual),
          card.video_url ? section("Видео", h("p", null, h("a", { href: card.video_url, target: "_blank", rel: "noopener noreferrer nofollow" }, card.video_url))) : null,
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
      h("span", { class: "avatar", "aria-hidden": "true" }, (c.author ? c.author.name : "?")[0].toUpperCase()),
      h("div", { class: "feed-body" },
        h("div", { class: "feed-head" }, h("strong", null, c.author ? c.author.name : "Удалённый пользователь"), timeEl(c.created_at)),
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

  const ACTION_TEXT = {
    created: (x) => `создал(а) карточку в «${x.new_value}»`,
    moved: (x) => `перенёс(ла): ${x.old_value} → ${x.new_value}`,
    resolved: (x) => `резолюция: ${x.new_value}`,
    assigned: (x) => x.new_value === "никто" ? `снял(а) исполнителя (${x.old_value})` : `исполнитель: ${x.new_value}`,
    edited: (x) => {
      const labels = { title: "название", description: "описание", steps: "шаги", expected: "ожидаемый результат", actual: "фактический результат",
        build_version: "сборку", coords: "координаты", static_id: "Static ID", occurred_at: "время", frequency: "частоту", video_url: "видео",
        server_id: "сервер", priority_id: "приоритет", category_id: "категорию", tags: "теги" };
      const what = labels[x.field] || x.field;
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
      h("strong", null, x.user ? x.user.name : "Система"), " ", (ACTION_TEXT[x.action] || (() => x.action))(x), timeEl(x.created_at))));
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
  const FORM_FIELDS = ["title", "description", "steps", "expected", "actual", "priority_id", "category_id", "server_id",
    "frequency", "build_version", "coords", "static_id", "occurred_at", "video_url"];

  function fillFormSelects(card) {
    const o = state.data.options;
    const opts = (list, current) => list.filter((x) => x.is_active || x.id === current).map((x) => h("option", { value: x.id }, x.name + (x.is_active ? "" : " (отключено)")));
    F("priority_id").replaceChildren(...opts(o.priority, card && card.priority_id));
    F("category_id").replaceChildren(...opts(o.category, card && card.category_id));
    F("server_id").replaceChildren(...opts(o.server, card && card.server_id));
    F("frequency").replaceChildren(...Object.entries(BOOT.frequencies).map(([v, l]) => h("option", { value: v }, l)));
    const pick = (sel, name) => { const x = Array.from(sel.options).find((op) => op.textContent === name); if (x) sel.value = x.value; };
    if (!card) { pick(F("priority_id"), "Средний"); pick(F("category_id"), "Прочее"); pick(F("server_id"), "Тестовый"); F("frequency").value = "unknown"; }

    const assigneeField = $("#cf-assignee-field");
    const canPick = !card && (isAdmin || me.role === "developer");
    assigneeField.hidden = !canPick;
    if (canPick) {
      const users = isAdmin ? state.data.users.filter((u) => u.role !== "pending") : [state.data.users.find((u) => u.id === me.id)].filter(Boolean);
      F("assignee_id").replaceChildren(h("option", { value: "" }, "— не назначать —"), ...users.map((u) => h("option", { value: u.id }, u.id === me.id ? `${u.name} (я)` : u.name)));
    }
    $("#tag-suggestions").replaceChildren(...state.data.tags.map((t) => h("option", { value: t.name })));
  }

  function openForm(card, opener) {
    state.formCard = card || null;
    state.pendingFiles = [];
    renderPendingFiles();
    els.form.reset();
    showFieldErrors(els.form, {});
    $("#form-error").hidden = true;
    $("#similar").hidden = true;
    fillFormSelects(card);
    $("#form-title").textContent = card ? `Редактирование #${card.id}` : "Новая карточка";
    $("#form-submit").textContent = card ? "Сохранить" : "Создать";
    $("#cf-files-field").hidden = !!card;
    $("#form-hint").textContent = card ? "Изменения увидят все участники" : "Черновик сохраняется автоматически";
    $("#draft-note").hidden = true;

    const editable = card ? new Set(card.permissions.editable) : null;
    if (card) {
      for (const name of FORM_FIELDS) { F(name).value = card[name] == null ? "" : card[name]; F(name).disabled = !editable.has(name); }
      state.formTags = card.tags.map((t) => t.name);
      $("#cf-tag-input").disabled = !editable.has("tags");
    } else {
      for (const name of FORM_FIELDS) F(name).disabled = false;
      $("#cf-tag-input").disabled = false;
      state.formTags = [];
      F("steps").value = STEPS_TEMPLATE;
      restoreDraft();
    }
    renderTags();
    openModal(els.formModal, opener);
    (card ? Array.from(els.form.querySelectorAll("input:not(:disabled), textarea:not(:disabled)"))[0] : F("title"))?.focus();
  }

  // черновик
  function saveDraft() {
    if (state.formCard) return;
    const draft = { tags: state.formTags };
    for (const name of FORM_FIELDS) draft[name] = F(name).value;
    const empty = !draft.title && !draft.description && (!draft.steps || draft.steps === STEPS_TEMPLATE);
    try { empty ? localStorage.removeItem(DRAFT_KEY) : localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch (e) { /* хранилище недоступно */ }
  }
  function restoreDraft() {
    let draft = null;
    try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (e) { draft = null; }
    if (!draft) return;
    for (const name of FORM_FIELDS) {
      if (draft[name] === undefined) continue;
      const el = F(name);
      if (el.tagName === "SELECT" && !Array.from(el.options).some((o) => o.value === String(draft[name]))) continue;
      el.value = draft[name];
    }
    state.formTags = Array.isArray(draft.tags) ? draft.tags : [];
    $("#draft-note").hidden = false;
  }
  function clearDraft() { try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* */ } }
  $("#draft-clear").addEventListener("click", () => {
    clearDraft();
    openForm(null);
    toast("Форма очищена", "info");
  });
  els.form.addEventListener("input", () => { saveDraft(); });

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
        const res = await api("GET", `/api/similar?q=${encodeURIComponent(q)}&exclude=${state.formCard ? state.formCard.id : 0}`);
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
    const payload = {};
    for (const name of FORM_FIELDS) if (!F(name).disabled) payload[name] = F(name).value;
    if (!$("#cf-tag-input").disabled) {
      if (tagInput.value.trim()) { addTag(tagInput.value); tagInput.value = ""; }
      payload.tags = state.formTags;
    }
    // проверка до отправки — сервер всё равно проверит ещё раз
    const errors = {};
    if (!card || !F("title").disabled) { if (!payload.title || !payload.title.trim()) errors.title = "Обязательное поле."; }
    if (!card || !F("description").disabled) { if (!payload.description || !payload.description.trim()) errors.description = "Обязательное поле."; }
    if (payload.video_url && !/^https?:\/\/\S+$/.test(payload.video_url.trim())) errors.video_url = "Ссылка должна начинаться с http:// или https://";
    showFieldErrors(els.form, errors);
    if (Object.keys(errors).length) return;
    if (payload.steps === STEPS_TEMPLATE) payload.steps = "";
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
      const res = await api("GET", "/api/archive");
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
        `Сюда попадают закрытые задачи старше ${state.data.settings.done_visible_days || 14} дней и карточки, убранные администратором.`));
      return;
    }
    els.archive.replaceChildren(h("ul", { class: "archive-list" }, cards.map((c) => h("li", null,
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
    if (isArchive) loadArchive(); else render();
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
    render();
  }));
  $$("button[data-tab]", els.quick).forEach((b) => b.addEventListener("click", () => {
    state.filters.tab = b.dataset.tab;
    writeUrl();
    render();
  }));
  els.reset.addEventListener("click", () => {
    state.filters = { q: "", tab: "all", priority: "", category: "", assignee: "", tag: "", server: "" };
    els.search.value = "";
    $$("select[data-filter]").forEach((s) => { s.value = ""; s.classList.remove("active"); });
    writeUrl();
    render();
    toast("Фильтры сброшены", "info", 1800);
  });
  $$("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#create-btn").addEventListener("click", (e) => openForm(null, e.currentTarget));

  // Горячие клавиши (работают при любой раскладке)
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (e.code === "KeyN") { e.preventDefault(); openForm(null, $("#create-btn")); }
    else if (e.code === "Slash") { e.preventDefault(); els.search.focus(); els.search.select(); }
  });

  // ------------------------------------------------------------ старт

  window.VBoard = { openCard };
  readUrl();
  load().then(() => {
    if (state.view === "archive") setView("archive");
    if (BOOT.open_card) openCard(BOOT.open_card);
  });
  startPolling();
})();
