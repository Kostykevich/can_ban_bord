/* Профиль: имя, каналы уведомлений (Discord, Telegram), события, пароль. */
"use strict";

(() => {
  const { $, h, api, busy, toast, BOOT, confirmDialog, avatarEl, showFieldErrors } = V;
  const p = BOOT.profile;
  const LOGIN_RE = /^[A-Za-z0-9_.\-]{2,32}$/;

  function renderIdentity() {
    $("#p-name").value = p.display_name;
    $("#p-username").value = p.username;
    $("#p-mention").textContent = "@" + p.username;
    const aliases = $("#p-aliases");
    aliases.hidden = !p.aliases.length;
    aliases.textContent = p.aliases.length ? "Прежние логины (тоже работают в упоминаниях): " + p.aliases.map((a) => "@" + a).join(", ") : "";
    renderAvatar();
  }

  // ------------------------------------------------------------ аватарка

  function renderAvatar() {
    $("#p-avatar").replaceChildren(avatarEl({ name: p.display_name, avatar: p.avatar }, "avatar-xl"));
    $("#p-avatar-remove").hidden = !p.avatar;
    // аватарка в шапке сайта
    const head = document.getElementById("header-avatar");
    if (head) {
      const fresh = avatarEl({ name: p.display_name, avatar: p.avatar });
      fresh.id = "header-avatar";
      head.replaceWith(fresh);
    }
  }

  /* Обрезаем картинку до квадрата 256×256 прямо в браузере — файл получается маленьким */
  function cropSquare(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const size = Math.min(img.naturalWidth, img.naturalHeight);
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 256;
        canvas.getContext("2d").drawImage(img, (img.naturalWidth - size) / 2, (img.naturalHeight - size) / 2, size, size, 0, 0, 256, 256);
        URL.revokeObjectURL(url);
        canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Не удалось обработать картинку"))), "image/png");
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Файл не похож на картинку")); };
      img.src = url;
    });
  }

  const avatarInput = $("#p-avatar-file");
  $("#p-avatar-pick").addEventListener("click", () => avatarInput.click());
  avatarInput.addEventListener("change", () => {
    const file = avatarInput.files[0];
    avatarInput.value = "";
    if (!file) return;
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) { toast("Только PNG, JPG, GIF или WebP", "error"); return; }
    if (file.size > 15 * 1024 * 1024) { toast("Файл слишком большой", "error"); return; }
    busy($("#p-avatar-pick"), async () => {
      try {
        const blob = await cropSquare(file);
        const fd = new FormData();
        fd.append("file", blob, "avatar.png");
        p.avatar = (await api("POST", "/api/profile/avatar", fd, true)).avatar;
        renderAvatar();
        toast("Аватарка обновлена");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });
  $("#p-avatar-remove").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    try {
      await api("DELETE", "/api/profile/avatar");
      p.avatar = null;
      renderAvatar();
      toast("Аватарка убрана");
    } catch (ex) { toast(ex.message, "error"); }
  }));

  // ------------------------------------------------------------ имя и логин

  $("#p-username").addEventListener("input", () => {
    const v = $("#p-username").value.trim();
    $("#p-mention").textContent = "@" + (v || p.username);
  });

  $("#profile-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const name = $("#p-name").value.trim();
    const login = $("#p-username").value.trim();
    if (!name) { toast("Имя не может быть пустым", "error"); return; }
    if (!LOGIN_RE.test(login)) { showFieldErrors(form, { username: "2–32 символа: латиница, цифры, _ . -" }); return; }
    showFieldErrors(form, {});
    const data = { display_name: name };
    const loginChanged = login !== p.username;
    if (loginChanged) data.username = login;
    busy(e.submitter, async () => {
      try {
        const res = await api("PATCH", "/api/profile", data);
        if (loginChanged && !p.aliases.some((a) => a.toLowerCase() === p.username.toLowerCase())
            && p.username.toLowerCase() !== res.username.toLowerCase()) p.aliases.push(p.username);
        p.aliases = p.aliases.filter((a) => a.toLowerCase() !== res.username.toLowerCase());
        p.display_name = name;
        p.username = res.username;
        renderIdentity();
        document.querySelectorAll(".user-chip-text strong").forEach((el) => { el.textContent = name; });
        toast(loginChanged ? `Сохранено. Новый логин: @${res.username} — входите с ним` : "Сохранено");
      } catch (ex) {
        showFieldErrors(form, ex.fields);
        toast(ex.message, "error");
      }
    });
  });

  renderIdentity();

  // ------------------------------------------------------------ каналы

  function statusLine(on, text) {
    return [h("span", { class: "status-dot" + (on ? " on" : ""), "aria-hidden": "true" }), text];
  }

  function toggle(flag, label) {
    const input = h("input", { type: "checkbox", checked: !!p[flag] });
    input.addEventListener("change", async () => {
      try {
        await api("PATCH", "/api/profile", { [flag]: input.checked });
        p[flag] = input.checked;
        toast(input.checked ? "Уведомления включены" : "Уведомления выключены");
        renderChannels();
      } catch (ex) { input.checked = !input.checked; toast(ex.message, "error"); }
    });
    return h("label", { class: "check small" }, input, label);
  }

  function renderChannels() {
    // Discord
    const dStatus = $("#ch-discord-status");
    const dActions = $("#ch-discord-actions");
    if (!BOOT.discord_bot) {
      dStatus.replaceChildren(...statusLine(false, "Бот не настроен на сервере — обратитесь к администратору"));
      dActions.replaceChildren();
    } else if (!p.discord_linked) {
      dStatus.replaceChildren(...statusLine(false, "Discord не привязан к аккаунту"));
      dActions.replaceChildren(BOOT.discord_login
        ? h("a", { class: "btn btn-discord btn-sm", href: "/auth/discord" }, "Привязать Discord")
        : h("span", { class: "muted small" }, "Попросите администратора указать ваш Discord ID"));
    } else {
      dStatus.replaceChildren(...statusLine(p.notify_discord, p.notify_discord
        ? "Бот пишет вам в личку. Должен быть общий сервер с ботом и открытые ЛС."
        : "Привязан, уведомления выключены"));
      dActions.replaceChildren(toggle("notify_discord", "Присылать"),
        p.has_password ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: unlinkDiscord }, "Отвязать") : null);
    }

    // Telegram
    const tStatus = $("#ch-telegram-status");
    const tActions = $("#ch-telegram-actions");
    if (!BOOT.telegram_bot) {
      tStatus.replaceChildren(...statusLine(false, "Бот не настроен на сервере — обратитесь к администратору"));
      tActions.replaceChildren();
    } else if (BOOT.telegram_problem && !p.telegram_linked) {
      // бот настроен, но не подключился к Telegram — показываем причину
      tStatus.replaceChildren(...statusLine(false, BOOT.telegram_problem));
      tActions.replaceChildren(h("button", { type: "button", class: "btn btn-sm", onclick: () => location.reload() }, "Проверить ещё раз"));
    } else if (!p.telegram_linked) {
      tStatus.replaceChildren(...statusLine(false, "Не подключён"));
      tActions.replaceChildren(h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: linkTelegram }, "Подключить Telegram"));
    } else {
      tStatus.replaceChildren(...statusLine(p.notify_telegram, p.notify_telegram ? "Подключён — уведомления приходят" : "Подключён, уведомления выключены"));
      tActions.replaceChildren(toggle("notify_telegram", "Присылать"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: unlinkTelegram }, "Отключить"));
    }
  }

  let pollTimer = null;
  async function linkTelegram(e) {
    const btn = e.currentTarget;
    await busy(btn, async () => {
      try {
        const res = await api("POST", "/api/profile/telegram/link");
        window.open(res.url, "_blank", "noopener");
        $("#ch-telegram-status").replaceChildren(...statusLine(false, "Откройте бота в Telegram и нажмите «Start». Ждём подтверждения…"));
        $("#ch-telegram-actions").replaceChildren(h("a", { class: "btn btn-sm", href: res.url, target: "_blank", rel: "noopener" }, "Открыть бота ещё раз"));
        // проверяем каждые 3 секунды, подключился ли Telegram (до 15 минут)
        clearInterval(pollTimer);
        const until = Date.now() + 15 * 60 * 1000;
        pollTimer = setInterval(async () => {
          if (Date.now() > until) { clearInterval(pollTimer); renderChannels(); return; }
          try {
            const s = await api("GET", "/api/profile/telegram/status");
            if (s.linked) {
              clearInterval(pollTimer);
              p.telegram_linked = true;
              p.notify_telegram = true;
              renderChannels();
              toast("Telegram подключён!");
            }
          } catch (ex) { /* подождём */ }
        }, 3000);
      } catch (ex) { toast(ex.message, "error"); }
    });
  }

  async function unlinkTelegram() {
    if (!await confirmDialog("Уведомления в Telegram перестанут приходить.", "Отключить", "Отключить Telegram?")) return;
    try {
      await api("DELETE", "/api/profile/telegram");
      p.telegram_linked = false;
      renderChannels();
      toast("Telegram отключён");
    } catch (ex) { toast(ex.message, "error"); }
  }

  async function unlinkDiscord() {
    if (!await confirmDialog("Вход через Discord и уведомления в Discord для этого аккаунта отключатся.", "Отвязать", "Отвязать Discord?")) return;
    try {
      await api("DELETE", "/api/profile/discord");
      p.discord_linked = false;
      renderChannels();
      toast("Discord отвязан");
    } catch (ex) { toast(ex.message, "error"); }
  }

  $("#test-notify").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    try {
      const res = await api("POST", "/api/profile/test-notify");
      toast(`Отправлено: ${res.channels.join(", ")}. Если не пришло через минуту — проверьте настройки.`);
    } catch (ex) { toast(ex.message, "error"); }
  }));

  // ------------------------------------------------------------ события

  const list = $("#events-list");
  for (const [key, label] of Object.entries(BOOT.events)) {
    list.append(h("label", { class: "check" }, h("input", { type: "checkbox", value: key, checked: p.notify_events.includes(key) }), label));
  }
  $("#events-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const events = Array.from(list.querySelectorAll("input:checked")).map((i) => i.value);
    busy(e.submitter, async () => {
      try {
        await api("PATCH", "/api/profile", { notify_events: events });
        toast("Настройки уведомлений сохранены");
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  // ------------------------------------------------------------ пароль

  $("#p-current-field").hidden = !p.has_password;
  $("#password-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const current = $("#p-current").value;
    const next = $("#p-new").value;
    if (next.length < 10) { toast("Новый пароль — от 10 символов", "error"); $("#p-new").focus(); return; }
    busy(e.submitter, async () => {
      try {
        await api("POST", "/api/profile/password", { current, new: next });
        $("#p-current").value = "";
        $("#p-new").value = "";
        p.has_password = true;
        $("#p-current-field").hidden = false;
        toast("Пароль изменён");
        renderChannels();
      } catch (ex) { toast(ex.message, "error"); }
    });
  });

  renderChannels();
})();
