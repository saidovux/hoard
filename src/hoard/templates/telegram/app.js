(() => {
  "use strict";

  const catalog = window.HOARD_CATALOG || {};
  const interactive = window.HOARD_INTERACTIVE || null;
  const interactiveEnabled = Boolean(interactive && interactive.interactive && interactive.csrf);
  const PAGE_SIZE = 48;
  const MEDIA_PAGE_SIZE = 24;
  const VIDEO_PAGE_SIZE = 12;
  const HISTORY_PAGE_SIZE = 20;
  const byId = (id) => document.getElementById(id);
  const content = byId("content");
  const queryInput = byId("query");
  const sortInput = byId("sort");
  const direction = byId("direction");
  const toolbar = byId("toolbar");
  const profileFilters = byId("profile-filters");
  const posts = Array.isArray(catalog.posts) ? catalog.posts : [];
  const profiles = Array.isArray(catalog.profiles) ? catalog.profiles : [];
  const videos = Array.isArray(catalog.videos) ? catalog.videos : [];
  const postMap = new Map(posts.map((item) => [Number(item.source_id), item]));
  const profileMap = new Map(profiles.map((item) => [Number(item.author_id), item]));
  const videoMap = new Map(videos.map((item) => [String(item.video_id), item]));
  const profileSearchIndex = new Map();
  const postSearchIndex = new Map();
  const sectionPages = new Map();
  const selectedProfiles = new Set();
  const validProfileId = (value) => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  let currentRoute = null;
  let inputTimer = null;

  const emptyFilters = () => ({
    source: "", photos: "any", videos: "any", psl: "any",
    rawMin: "", rawMax: "", boosted: "", ageMin: "", ageMax: "",
  });
  const lists = {
    posts: {query: "", sort: "published", descending: true, page: 0},
    profiles: {
      query: "", sort: "last_active", descending: true, page: 0,
      filters: emptyFilters(),
    },
  };

  function restoreInteractiveState() {
    if (!interactiveEnabled) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem("hoard.web.state") || "null");
      if (!saved || typeof saved !== "object") return;
      ["posts", "profiles"].forEach((view) => {
        const source = saved.lists && saved.lists[view];
        if (!source || typeof source !== "object") return;
        lists[view].query = String(source.query || "");
        lists[view].sort = String(source.sort || lists[view].sort);
        lists[view].descending = Boolean(source.descending);
        lists[view].page = Math.max(0, Number(source.page || 0));
        if (view === "profiles" && source.filters) {
          lists.profiles.filters = {...emptyFilters(), ...source.filters};
        }
      });
      (saved.selectedProfiles || []).forEach((id) => {
        const value = Number(id);
        if (profileMap.has(value) && validProfileId(value)) selectedProfiles.add(value);
      });
    } catch (_) { /* Broken browser state is non-authoritative. */ }
  }

  function saveInteractiveState() {
    if (!interactiveEnabled) return;
    try {
      sessionStorage.setItem("hoard.web.state", JSON.stringify({
        lists,
        selectedProfiles: [...selectedProfiles],
      }));
    } catch (_) { /* Private browsing may disable sessionStorage. */ }
  }

  const profileSorts = [
    ["name", "Имя"], ["username", "Username"], ["id", "Telegram ID"],
    ["discovered", "Дата обнаружения"], ["last_active", "Последняя активность"],
    ["media_count", "Количество медиа"], ["raw_psl", "Raw PSL"],
    ["boosted_psl", "Boosted PSL"], ["age", "Возраст"],
  ];
  const postSorts = [
    ["published", "Дата публикации"], ["checked", "Последняя проверка"],
    ["channel", "Канал"], ["post_id", "ID поста"],
    ["video_count", "Количество кружков"],
  ];

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clean(value, fallback = "—") {
    const result = String(value ?? "").trim();
    return result || fallback;
  }

  function dateText(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("ru-RU");
  }

  function numberValue(value) {
    if (value === null || value === undefined || value === "") return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function scoreText(value) {
    const score = numberValue(value);
    return score === null ? "—" : score.toLocaleString("ru-RU", {maximumFractionDigits: 2});
  }

  function meta(items) {
    const line = element("div", "meta");
    items.filter(Boolean).forEach((text) => line.append(element("span", "", text)));
    return line;
  }

  function originalLink(url, label) {
    const link = element("a", "", label);
    try {
      const parsed = new URL(String(url));
      if (parsed.protocol === "https:" && parsed.hostname === "t.me") {
        link.href = parsed.href;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        return link;
      }
    } catch (_) { /* Invalid external links stay inert text. */ }
    return element("span", "muted", label);
  }

  function showNotice(message, error = false) {
    const notice = byId("notice");
    notice.hidden = !message;
    notice.textContent = String(message || "");
    notice.classList.toggle("success", !error);
  }

  async function api(path, options = {}) {
    if (!interactiveEnabled) throw new Error("Интерактивный режим не запущен");
    const method = options.method || "GET";
    const headers = {"Accept": "application/json"};
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Hoard-CSRF"] = interactive.csrf;
    }
    const response = await window.fetch.call(window, path, {
      method,
      headers,
      credentials: "same-origin",
      body: method === "GET" ? undefined : JSON.stringify(options.body || {}),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* handled below */ }
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function modal(title) {
    const dialog = element("dialog", "workflow-dialog");
    const bar = element("div", "workflow-head");
    bar.append(element("h2", "", title));
    const close = element("button", "secondary", "Закрыть ×");
    close.type = "button";
    close.addEventListener("click", () => dialog.close());
    bar.append(close);
    const body = element("div", "workflow-body");
    dialog.append(bar, body);
    document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove(), {once: true});
    dialog.showModal();
    return {dialog, body};
  }

  async function ensureAI(resume, forceSettings = false) {
    let config = {};
    try {
      config = await api("/api/config");
      if (config.configured && !forceSettings) {
        resume();
        return;
      }
    } catch (error) {
      showNotice(error.message, true);
      return;
    }
    const {dialog, body} = modal(config.configured ? "AI → Настройки" : "DeepSeek не подключён");
    const keySource = ({env: "переменная окружения", file: "локальный файл", none: "не задан"})[
      config.key_source || "none"
    ];
    body.append(element("p", "muted", `Текущий источник ключа: ${keySource}.`));
    body.append(element(
      "p", "muted",
      config.configured
        ? "Сохранённый ключ не показывается. Вставь новый ключ только если хочешь его заменить."
        : "Добавь один API-ключ. Модель, тарифы и другие настройки для начала не нужны.",
    ));
    const label = element("label", "field");
    label.append(element("span", "", "API-ключ DeepSeek"));
    const input = element("input", "");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = "sk-…";
    label.append(input);
    const feedback = element("p", "notice-inline");
    const save = element("button", "primary-action", "Сохранить");
    save.type = "button";
    save.addEventListener("click", async () => {
      if (!input.value.trim()) {
        feedback.textContent = "Вставь API-ключ DeepSeek.";
        return;
      }
      save.disabled = true;
      try {
        const result = await api("/api/config", {
          method: "POST", body: {api_key: input.value.trim()},
        });
        input.value = "";
        dialog.close();
        showNotice(result.message || "Ключ сохранён; проверка произойдёт при первом запросе.");
        resume();
      } catch (error) {
        feedback.textContent = error.message;
        save.disabled = false;
      }
    });
    body.append(label, feedback, save);
    input.focus();
  }

  function operationId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID().replaceAll("-", "");
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  }

  function operationStorageKey(selector, photoIds) {
    const authors = Array.isArray(selector.author_ids)
      ? selector.author_ids.map(Number).filter(validProfileId).sort((a, b) => a - b)
      : [];
    const photos = Object.fromEntries(Object.keys(photoIds).sort().map((id) => [
      id, [...photoIds[id]].map(String),
    ]));
    return `hoard.ai.operation:${JSON.stringify({
      authors,
      source_id: Number.isSafeInteger(Number(selector.source_id)) ? Number(selector.source_id) : null,
      source_url: String(selector.source_url || ""),
      photos,
    })}`;
  }

  function savedOperation(key) {
    let value = "";
    try { value = sessionStorage.getItem(key) || ""; } catch (_) { /* unavailable */ }
    if (!/^[a-f0-9]{32}$/.test(value)) {
      value = operationId();
      try { sessionStorage.setItem(key, value); } catch (_) { /* unavailable */ }
    }
    return value;
  }

  function batchSummary(batch) {
    const cost = batch.estimated_cost_usd === null || batch.estimated_cost_usd === undefined
      ? "стоимость неизвестна"
      : `≈ $${batch.estimated_cost_usd}`;
    return `Выбрано ${batch.selected_count || 0} · в очереди ${batch.queued || 0} · `
      + `готово ${batch.completed || 0} · пропущено ${batch.skipped || 0} · `
      + `ошибки ${batch.errors || 0} · ${cost}`;
  }

  function failureLabel(value) {
    return ({
      invalid_json: "AI вернул не JSON",
      json_schema_mismatch: "Ответ AI не соответствует JSON-схеме",
      invalid_psl_result: "Ответ AI нарушает PSL-контракт",
      provider_error: "Провайдер AI не выполнил запрос",
      outcome_unknown: "Результат запроса неизвестен — проверь перед ручным повтором",
    })[String(value || "")] || "";
  }

  function renderBatch(body, batch) {
    body.replaceChildren(element("p", "workflow-summary", batchSummary(batch)));
    const list = element("div", "workflow-list");
    (batch.items || []).forEach((item) => {
      const row = element("div", "workflow-row");
      const name = clean(item.display_name || item.current_username, `ID ${item.author_id}`);
      row.append(element("strong", "", name));
      row.append(element("span", "muted", item.state || "—"));
      if (item.reason) row.append(element("span", "workflow-reason", item.reason));
      if (item.failure_category) {
        row.append(element("span", "workflow-reason", failureLabel(item.failure_category)));
      }
      list.append(row);
    });
    body.append(list);
  }

  const ACTIVE_BATCH_KEY = "hoard.ai.active-batch";

  async function pollBatch(dialog, body, batchId, operationKey) {
    while (dialog.open) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      let result;
      try { result = await api(`/api/batches/${encodeURIComponent(batchId)}`); }
      catch (error) {
        body.append(element("p", "notice-inline", error.message));
        return;
      }
      const batch = result.batch;
      renderBatch(body, batch);
      if (batch.terminal) {
        try { sessionStorage.removeItem(operationKey); } catch (_) { /* unavailable */ }
        try { sessionStorage.removeItem(ACTIVE_BATCH_KEY); } catch (_) { /* unavailable */ }
        try {
          body.append(element("p", "muted", "Обновляем локальный каталог…"));
          await api("/api/catalog/refresh", {method: "POST", body: {}});
          saveInteractiveState();
          renderBatch(body, batch);
          body.append(element("p", "muted", "Локальный каталог обновлён. Итог группы сохранён."));
        } catch (error) {
          body.append(element("p", "notice-inline", error.message));
        }
        return;
      }
    }
  }

  function analysisPreview(preview, selector) {
    const {dialog, body} = modal(
      preview.source ? "Профили публикации" :
        (preview.selected_count === 1 ? "Анализ профиля" : "Анализ выбранных профилей"),
    );
    body.append(element(
      "p", "workflow-summary",
      `Выбрано ${preview.selected_count} · доступно ${preview.photo_count} локальных фото. `
        + "На этом шаге запрос в DeepSeek не выполняется.",
    ));
    if (preview.source) {
      const source = preview.source;
      body.append(element(
        "p", "muted",
        `${clean(source.channel_title || source.title, `@${source.username}`)} #${source.post_id}`,
      ));
    }
    const pageLimit = Number(preview.limit || 25);
    const pages = new Map([[Number(preview.offset || 0), preview]]);
    const photoIds = {};
    const photoSnapshots = {};
    let current = preview;
    let photoInputs = [];
    function ingest(page) {
      (page.items || []).forEach((item) => {
        if (!validProfileId(item.author_id) || !(item.selected_photo_ids || []).length) return;
        const key = String(item.author_id);
        if (!photoIds[key]) photoIds[key] = [...item.selected_photo_ids].map(String);
        if (item.photo_snapshot && !photoSnapshots[key]) {
          photoSnapshots[key] = String(item.photo_snapshot);
        }
      });
    }
    ingest(preview);
    const pageLabel = element("p", "muted");
    const list = element("div", "workflow-list");
    const skippedSummary = element("p", "workflow-reason");
    const previousPage = element("button", "secondary", "← Страница");
    const nextPage = element("button", "secondary", "Страница →");
    async function loadPage(offset) {
      if (!pages.has(offset)) {
        const result = await api("/api/ai/preview", {
          method: "POST", body: {...selector, limit: pageLimit, offset},
        });
        pages.set(offset, result.preview);
        ingest(result.preview);
      }
      return pages.get(offset);
    }
    function renderPreviewPage(page) {
      current = page;
      photoInputs = [];
      const rows = [];
      const reasons = new Map();
      (page.items || []).forEach((item) => {
        const row = element("div", "workflow-row");
        row.append(element(
          "strong", "", clean(item.display_name || item.username, `ID ${item.author_id}`),
        ));
        row.append(element("span", "muted", `ID ${item.author_id} · ${item.photo_count} фото`));
        if (preview.selected_count === 1) {
          const photos = element("div", "photo-choices");
          const defaults = new Set(photoIds[String(item.author_id)] || []);
          (item.photos || []).filter((photo) => photo.available).forEach((photo) => {
            const label = element("label", "check-row");
            const input = element("input", "");
            input.type = "checkbox";
            input.value = String(photo.photo_id);
            input.checked = defaults.has(String(photo.photo_id));
            photoInputs.push(input);
            label.append(input, element("span", "", `${photo.photo_id} · ${dateText(photo.date)}`));
            photos.append(label);
          });
          row.append(photos);
        }
        const previous = item.previous;
        if (previous) row.append(element(
          "span", "muted",
          `Предыдущий: ${previous.result_status || previous.attempt_state || "—"}`,
        ));
        if (!(item.selected_photo_ids || []).length) {
          const reason = item.reason || "Будет пропущен";
          reasons.set(reason, (reasons.get(reason) || 0) + 1);
        }
        rows.push(row);
      });
      list.replaceChildren(...rows);
      const start = page.items.length ? Number(page.offset || 0) + 1 : 0;
      pageLabel.textContent = `Профили ${start}–${Number(page.offset || 0) + page.items.length} из ${page.selected_count}`;
      skippedSummary.textContent = [...reasons].map(([reason, count]) => `${reason}: ${count}`).join(" · ");
      previousPage.disabled = Number(page.offset || 0) <= 0;
      nextPage.disabled = !page.has_more;
    }
    previousPage.type = nextPage.type = "button";
    previousPage.addEventListener("click", async () => {
      renderPreviewPage(await loadPage(Math.max(0, Number(current.offset || 0) - pageLimit)));
    });
    nextPage.addEventListener("click", async () => {
      renderPreviewPage(await loadPage(Number(current.offset || 0) + pageLimit));
    });
    const pageActions = element("div", "interactive-actions");
    pageActions.append(previousPage, nextPage);
    body.append(pageLabel, list, skippedSummary, pageActions);
    renderPreviewPage(preview);
    body.append(element(
      "p", "muted",
      "Возрастной допуск 18+ применяется автоматически. Будут отправлены только выбранные "
        + "локальные фото; запрос DeepSeek может быть платным, автоматического повтора нет.",
    ));
    const feedback = element("p", "notice-inline");
    const count = Number(preview.selected_photo_count || 0);
    const start = element(
      "button", "primary-action", `Отправить ${count} фото в DeepSeek и запустить анализ`,
    );
    start.type = "button";
    start.addEventListener("click", async () => {
      if (preview.selected_count === 1) {
        const selected = photoInputs.filter((input) => input.checked).map((input) => input.value);
        if (selected.length < 1 || selected.length > 4) {
          feedback.textContent = "Выбери от одной до четырёх фотографий.";
          return;
        }
        photoIds[String(preview.items[0].author_id)] = selected;
        if (JSON.stringify(selected) !== JSON.stringify(preview.items[0].selected_photo_ids || [])) {
          delete photoSnapshots[String(preview.items[0].author_id)];
        }
      }
      if (!Object.keys(photoIds).length) {
        feedback.textContent = "Нет подходящих пользовательских профилей с локальными фото.";
        return;
      }
      start.disabled = true;
      try {
        for (let offset = 0; offset < preview.selected_count; offset += pageLimit) {
          await loadPage(offset);
        }
        const storageKey = operationStorageKey(selector, photoIds);
        const result = await api("/api/ai/start", {
          method: "POST",
          body: {
            ...selector,
            photo_ids: photoIds,
            photo_snapshots: photoSnapshots,
            operation_id: savedOperation(storageKey),
            external_confirmed: true,
          },
        });
        try {
          sessionStorage.setItem(ACTIVE_BATCH_KEY, JSON.stringify({
            id: result.batch.id, operationKey: storageKey,
          }));
        } catch (_) { /* unavailable */ }
        renderBatch(body, result.batch);
        pollBatch(dialog, body, result.batch.id, storageKey);
      } catch (error) {
        feedback.textContent = error.message;
        start.disabled = false;
      }
    });
    body.append(feedback, start);
  }

  async function loadAnalysis(selector) {
    try {
      const result = await api("/api/ai/preview", {
        method: "POST", body: {...selector, limit: 25, offset: 0},
      });
      analysisPreview(result.preview, selector);
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  function beginAnalysis(selector) {
    ensureAI(() => loadAnalysis(selector));
  }

  async function restoreActiveBatch() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(ACTIVE_BATCH_KEY) || "null"); }
    catch (_) { return; }
    if (!saved || !/^[a-f0-9]{32}$/.test(String(saved.id || ""))) return;
    try {
      const result = await api(`/api/batches/${encodeURIComponent(saved.id)}`);
      const {dialog, body} = modal("Telegram AI · активная группа");
      renderBatch(body, result.batch);
      if (result.batch.terminal) {
        try { sessionStorage.removeItem(ACTIVE_BATCH_KEY); } catch (_) { /* unavailable */ }
      } else {
        pollBatch(dialog, body, saved.id, String(saved.operationKey || ""));
      }
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  function initInteractive() {
    if (!interactiveEnabled) return;
    const selected = element("button", "secondary", `Анализировать выбранные (${selectedProfiles.size})`);
    selected.id = "analyze-selected";
    selected.type = "button";
    selected.hidden = true;
    selected.addEventListener("click", () => {
      const ids = [...selectedProfiles];
      if (!ids.length) showNotice("Сначала выбери один или несколько профилей.", true);
      else beginAnalysis({author_ids: ids});
    });
    toolbar.append(selected);
    const settings = element("button", "", "AI настройки");
    settings.id = "ai-settings";
    settings.type = "button";
    settings.addEventListener("click", () => ensureAI(() => {
      showNotice("Новый API-ключ сохранён; он проверится при следующем запросе.");
    }, true));
    document.querySelector(".switcher").append(settings);
    const footer = document.querySelector("footer");
    if (footer) footer.append(" · интерактивный localhost-режим");
    api("/api/config").then((config) => {
      const labels = {
        not_configured: "Добавить API-ключ",
        key_saved: "Ключ сохранён",
        verified: "DeepSeek подключён",
        authorization_error: "Ошибка авторизации",
      };
      settings.textContent = labels[config.key_status]
        || (config.configured ? "AI настройки" : "Добавить API-ключ");
      if (!config.configured) showNotice("DeepSeek не подключён. Добавь API-ключ для анализа.");
      else if (config.key_status === "authorization_error") {
        showNotice("DeepSeek отклонил сохранённый ключ. Замени его в AI настройках.", true);
      }
    }).catch((error) => showNotice(error.message, true));
    restoreActiveBatch();
  }

  function selectionControls(profile) {
    const actions = element("div", "interactive-actions");
    if (!validProfileId(profile.author_id)) return actions;
    const label = element("label", "check-row");
    const checkbox = element("input", "");
    checkbox.type = "checkbox";
    checkbox.checked = selectedProfiles.has(Number(profile.author_id));
    checkbox.addEventListener("change", () => {
      const id = Number(profile.author_id);
      if (!validProfileId(id)) return;
      if (checkbox.checked) selectedProfiles.add(id);
      else selectedProfiles.delete(id);
      saveInteractiveState();
      const global = byId("analyze-selected");
      if (global) global.textContent = `Анализировать выбранные (${selectedProfiles.size})`;
    });
    label.append(checkbox, element("span", "", "Выбрать"));
    const analyze = element("button", "secondary", "Анализировать профиль");
    analyze.type = "button";
    analyze.addEventListener("click", () => beginAnalysis({author_ids: [Number(profile.author_id)]}));
    actions.append(label, analyze);
    return actions;
  }

  function localMediaURL(url) {
    const value = String(url || "").trim();
    if (!value || value.startsWith("//") || value.includes("\\") || /[\u0000-\u001f]/.test(value)) {
      return "";
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
      try {
        return new URL(value).protocol === "file:" ? value : "";
      } catch (_) {
        return "";
      }
    }
    if (value.startsWith("/")) {
      return interactiveEnabled && value.startsWith("/media/") ? value : "";
    }
    try {
      if (decodeURIComponent(value).split("/").some((part) => part === "..")) return "";
    } catch (_) {
      return "";
    }
    return value;
  }

  function latestPSL(profile) {
    const value = profile && profile.psl_latest;
    return value && value.status === "completed" ? value : null;
  }

  function initials(profile) {
    const source = clean(profile.display_name || profile.username || profile.author_id, "?");
    return source.slice(0, 2).toLocaleUpperCase("ru-RU");
  }

  function avatar(profile, large = false) {
    const photo = (profile.photos || []).find((item) => item.available && localMediaURL(item.url));
    if (!photo) return element("div", `avatar initials${large ? " large" : ""}`, initials(profile));
    const image = element("img", "avatar");
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = "";
    image.src = localMediaURL(photo.url);
    return image;
  }

  function profileSearch(profile) {
    const history = (profile.username_history || []).map((item) => item.username || "").join(" ");
    const related = (profile.post_ids || []).map((id) => {
      const post = postMap.get(Number(id)) || {};
      return `${post.channel_title || ""} ${post.channel_username || ""} ${post.post_id || ""}`;
    }).join(" ");
    return `${profile.author_id} ${profile.username || ""} ${profile.display_name || ""} ${history} ${related}`
      .toLocaleLowerCase("ru-RU");
  }

  profiles.forEach((profile) => profileSearchIndex.set(Number(profile.author_id), profileSearch(profile)));

  function postSearch(post) {
    const authors = (post.profile_ids || []).map((id) => profileSearchIndex.get(Number(id)) || "").join(" ");
    return `${post.source_id} ${post.post_id} ${post.channel_title || ""} ${post.channel_username || ""} ${post.url || ""} ${post.text || ""} ${authors}`
      .toLocaleLowerCase("ru-RU");
  }

  posts.forEach((post) => postSearchIndex.set(Number(post.source_id), postSearch(post)));

  function listRoute(view) {
    return {type: "list", view: view === "profiles" ? "profiles" : "posts"};
  }

  function routeHash(route) {
    if (route.type === "post") return `#post/${route.id}`;
    if (route.type === "profile") {
      if (route.origin && route.origin.type === "post") {
        return `#profile/${route.id}/post/${route.origin.id}`;
      }
      return `#profile/${route.id}/profiles`;
    }
    return route.view === "profiles" ? "#profiles" : "#posts";
  }

  function parseRoute() {
    const parts = location.hash.replace(/^#/, "").split("/").filter(Boolean);
    if (parts[0] === "post" && Number.isSafeInteger(Number(parts[1]))) {
      return {type: "post", id: Number(parts[1])};
    }
    if (parts[0] === "profile" && Number.isSafeInteger(Number(parts[1]))) {
      const origin = parts[2] === "post" && Number.isSafeInteger(Number(parts[3]))
        ? {type: "post", id: Number(parts[3])}
        : listRoute("profiles");
      return {type: "profile", id: Number(parts[1]), origin};
    }
    return listRoute(parts[0] === "profiles" ? "profiles" : "posts");
  }

  function rememberScroll() {
    if (!currentRoute) return;
    const previous = history.state || {};
    history.replaceState(
      {...previous, hoard: true, route: currentRoute, scrollY: window.scrollY},
      "",
      routeHash(currentRoute),
    );
  }

  function navigate(route, replace = false) {
    const destination = routeHash(route);
    if (currentRoute && destination === routeHash(currentRoute)) {
      renderRoute(route);
      return;
    }
    rememberScroll();
    const depth = replace ? Number(history.state?.depth || 0) : Number(history.state?.depth || 0) + 1;
    const method = replace ? "replaceState" : "pushState";
    history[method]({hoard: true, route, depth, scrollY: 0}, "", destination);
    renderRoute(route);
    window.scrollTo(0, 0);
  }

  function goBack(fallback) {
    if (history.state?.hoard && Number(history.state.depth || 0) > 0) history.back();
    else navigate(fallback, true);
  }

  function backButton(fallback, label) {
    const button = element("button", "back", `← ${label}`);
    button.type = "button";
    button.addEventListener("click", () => goBack(fallback));
    return button;
  }

  function available(item) {
    return Boolean(item && item.available && localMediaURL(item.url));
  }

  function isVideoItem(item) {
    return Boolean(item && (
      item.video_id
      || /video|round/i.test(String(item.kind || item.media_kind || ""))
      || /\.(mp4|webm)(?:$|[?#])/i.test(String(item.url || ""))
    ));
  }

  const viewer = byId("viewer");
  const viewerState = {items: [], index: 0, label: ""};

  function closeViewer() {
    byId("viewer-stage").replaceChildren();
    if (typeof viewer.close === "function" && viewer.open) viewer.close();
    else viewer.removeAttribute("open");
  }

  function renderViewer() {
    const item = viewerState.items[viewerState.index];
    const stage = byId("viewer-stage");
    stage.replaceChildren();
    if (!item || !available(item)) {
      stage.append(element("div", "missing", "Локальный файл недоступен"));
      return;
    }
    const url = localMediaURL(item.url);
    const isVideo = isVideoItem(item);
    if (isVideo) {
      const video = element("video", "");
      video.controls = true;
      video.preload = "none";
      video.src = url;
      stage.append(video);
    } else {
      const image = element("img", "");
      image.alt = viewerState.label;
      image.src = url;
      stage.append(image);
    }
    byId("viewer-label").textContent = viewerState.label;
    byId("viewer-position").textContent = `${viewerState.index + 1} / ${viewerState.items.length}`;
    byId("viewer-previous").disabled = viewerState.items.length < 2;
    byId("viewer-next").disabled = viewerState.items.length < 2;
  }

  function openViewer(items, index, label) {
    const safe = items.filter(available);
    const selected = items[index];
    const actual = Math.max(0, safe.indexOf(selected));
    if (!safe.length) return;
    viewerState.items = safe;
    viewerState.index = actual;
    viewerState.label = label;
    renderViewer();
    if (typeof viewer.showModal === "function") viewer.showModal();
    else viewer.setAttribute("open", "");
    byId("viewer-close").focus();
  }

  function moveViewer(change) {
    if (!viewerState.items.length) return;
    viewerState.index = (viewerState.index + change + viewerState.items.length) % viewerState.items.length;
    renderViewer();
  }

  function mediaNode(item, kind, label, gallery = [item], galleryIndex = 0) {
    const url = item && item.available ? localMediaURL(item.url) : "";
    if (!url) return element("div", "missing", label || "Локальный файл недоступен");
    const isVideo = kind === "video" || /video|round/i.test(kind || "");
    if (isVideo) {
      const card = element("div", "media-card");
      const video = element("video", "media");
      video.controls = true;
      video.preload = "none";
      video.src = url;
      const actions = element("div", "media-actions");
      const expand = element("button", "secondary", "Увеличить");
      expand.type = "button";
      expand.addEventListener("click", () => openViewer(gallery, galleryIndex, label));
      actions.append(expand);
      card.append(video, actions);
      return card;
    }
    const button = element("button", "media-button");
    button.type = "button";
    button.setAttribute("aria-label", `${label || "Изображение"}: открыть крупно`);
    const image = element("img", "media");
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = label || "Локальное изображение";
    image.src = url;
    button.append(image);
    button.addEventListener("click", () => openViewer(gallery, galleryIndex, label));
    return button;
  }

  function boolFilter(value, actual) {
    return value === "any" || (value === "yes") === actual;
  }

  function filteredProfiles() {
    const list = lists.profiles;
    const needle = list.query.trim().replace(/^@/, "").toLocaleLowerCase("ru-RU");
    const filters = list.filters;
    const rawMin = numberValue(filters.rawMin);
    const rawMax = numberValue(filters.rawMax);
    const boosted = numberValue(filters.boosted);
    const ageMin = numberValue(filters.ageMin);
    const ageMax = numberValue(filters.ageMax);
    return profiles.filter((profile) => {
      if (needle && !(profileSearchIndex.get(Number(profile.author_id)) || "").includes(needle)) return false;
      if (filters.source && !(profile.post_ids || []).some((id) => String(id) === filters.source)) return false;
      if (!boolFilter(filters.photos, (profile.photos || []).length > 0)) return false;
      if (!boolFilter(filters.videos, (profile.video_ids || []).length > 0)) return false;
      const psl = latestPSL(profile);
      if (!boolFilter(filters.psl, Boolean(psl))) return false;
      const low = psl ? numberValue(psl.raw_psl_min) : null;
      const high = psl ? numberValue(psl.raw_psl_max) : null;
      const boost = psl ? numberValue(psl.boosted_psl) : null;
      const age = psl ? numberValue(psl.estimated_age) : null;
      if (rawMin !== null && (low === null || low < rawMin)) return false;
      if (rawMax !== null && (high === null || high > rawMax)) return false;
      if (boosted !== null && (boost === null || boost < boosted)) return false;
      if (ageMin !== null && (age === null || age < ageMin)) return false;
      if (ageMax !== null && (age === null || age > ageMax)) return false;
      return true;
    });
  }

  function filteredPosts() {
    const needle = lists.posts.query.trim().replace(/^@/, "").toLocaleLowerCase("ru-RU");
    return needle
      ? posts.filter((post) => (postSearchIndex.get(Number(post.source_id)) || "").includes(needle))
      : [...posts];
  }

  function profileSortValue(profile, key) {
    const psl = latestPSL(profile);
    if (key === "id") return Number(profile.author_id);
    if (key === "username") return String(profile.username || "").toLocaleLowerCase("ru-RU");
    if (key === "discovered") return profile.first_seen || "";
    if (key === "last_active") return profile.last_activity || profile.last_seen || "";
    if (key === "media_count") return (profile.video_ids || []).length + (profile.photos || []).length;
    if (key === "raw_psl") {
      const low = psl ? numberValue(psl.raw_psl_min) : null;
      const high = psl ? numberValue(psl.raw_psl_max) : null;
      return low === null || high === null ? null : (low + high) / 2;
    }
    if (key === "boosted_psl") return psl ? numberValue(psl.boosted_psl) : null;
    if (key === "age") return psl ? numberValue(psl.estimated_age) : null;
    return String(profile.display_name || "").toLocaleLowerCase("ru-RU");
  }

  function postSortValue(post, key) {
    if (key === "post_id") return Number(post.post_id);
    if (key === "checked") return post.last_checked_at || "";
    if (key === "channel") return String(post.channel_title || post.channel_username || "").toLocaleLowerCase("ru-RU");
    if (key === "video_count") return Number(post.video_note_count || 0);
    return post.published_at || "";
  }

  function sorted(items, view) {
    const list = lists[view];
    const numeric = view === "profiles"
      ? new Set(["id", "media_count", "raw_psl", "boosted_psl", "age"])
      : new Set(["post_id", "video_count"]);
    const getter = view === "profiles" ? profileSortValue : postSortValue;
    return [...items].sort((left, right) => {
      const a = getter(left, list.sort);
      const b = getter(right, list.sort);
      if (numeric.has(list.sort)) {
        const aMissing = a === null || a === undefined;
        const bMissing = b === null || b === undefined;
        if (aMissing !== bMissing) return aMissing ? 1 : -1;
      }
      const comparison = typeof a === "number"
        ? a - b
        : String(a ?? "").localeCompare(String(b ?? ""), "ru");
      if (comparison) return list.descending ? -comparison : comparison;
      return Number(left.author_id || left.source_id || 0) - Number(right.author_id || right.source_id || 0);
    });
  }

  function profileCard(profile, origin) {
    const card = element("article", "card profile-card");
    const button = element("button", "card-action");
    button.type = "button";
    const body = element("div", "card-body");
    body.append(avatar(profile));
    const copy = element("div", "profile-copy");
    copy.append(element("div", "card-title", clean(profile.display_name, `ID ${profile.author_id}`)));
    copy.append(element("div", "eyebrow", profile.username ? `@${profile.username}` : "без username"));
    copy.append(meta([
      `ID ${profile.author_id}`,
      `${(profile.photos || []).length} фото`,
      `${(profile.video_ids || []).length} кружков`,
      `${(profile.post_ids || []).length} источников`,
      `активность: ${dateText(profile.last_activity || profile.last_seen)}`,
    ]));
    const psl = latestPSL(profile);
    if (psl) copy.append(element("span", "psl-badge", `PSL ${scoreText(psl.raw_psl_min)}–${scoreText(psl.raw_psl_max)}`));
    body.append(copy);
    button.append(body);
    button.addEventListener("click", () => navigate({type: "profile", id: Number(profile.author_id), origin}));
    card.append(button);
    if (interactiveEnabled) card.append(selectionControls(profile));
    return card;
  }

  function postPreview(post, container) {
    const item = (post.media || []).find(available);
    if (!item) return;
    const preview = element("div", "card-preview");
    const isVideo = isVideoItem(item);
    if (isVideo) {
      preview.append(element("div", "video-placeholder", "Видео · откройте публикацию"));
    } else {
      const button = element("button", "media-button");
      button.type = "button";
      button.setAttribute("aria-label", "Открыть изображение публикации");
      const image = element("img", "");
      image.loading = "lazy";
      image.decoding = "async";
      image.alt = "Медиа публикации";
      image.src = localMediaURL(item.url);
      button.append(image);
      button.addEventListener("click", () => openViewer(post.media || [], (post.media || []).indexOf(item), "Медиа публикации"));
      preview.append(button);
    }
    container.append(preview);
  }

  function postCard(post) {
    const card = element("article", "card post-card");
    const button = element("button", "card-action");
    button.type = "button";
    const body = element("div", "card-body");
    body.append(element("div", "eyebrow", post.channel_username ? `@${post.channel_username}` : "Telegram"));
    body.append(element("div", "card-title", clean(post.channel_title, `Пост #${post.post_id}`)));
    if (post.text) body.append(element("div", "excerpt", post.text));
    body.append(meta([
      `пост ${post.post_id}`,
      `опубликован: ${dateText(post.published_at)}`,
      `проверен: ${dateText(post.last_checked_at)}`,
    ]));
    const metrics = element("div", "metrics");
    metrics.append(
      element("span", "metric", `${post.author_count || 0} профилей`),
      element("span", "metric", `${post.video_note_count || 0} кружков`),
    );
    body.append(metrics);
    button.append(body);
    button.addEventListener("click", () => navigate({type: "post", id: Number(post.source_id)}));
    card.append(button);
    postPreview(post, card);
    if (interactiveEnabled) {
      const actions = element("div", "interactive-actions");
      const analyze = element("button", "secondary", "Анализировать профили");
      analyze.type = "button";
      analyze.addEventListener("click", () => beginAnalysis({source_id: Number(post.source_id)}));
      actions.append(analyze);
      card.append(actions);
    }
    return card;
  }

  function activeFilterCount() {
    const filters = lists.profiles.filters;
    return Object.values(filters).filter((value) => value && value !== "any").length;
  }

  function syncToolbar(view) {
    toolbar.hidden = false;
    profileFilters.hidden = view !== "profiles";
    byId("show-posts").classList.toggle("active", view === "posts");
    byId("show-profiles").classList.toggle("active", view === "profiles");
    const list = lists[view];
    if (queryInput.value !== list.query) queryInput.value = list.query;
    const choices = view === "profiles" ? profileSorts : postSorts;
    sortInput.replaceChildren(...choices.map(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      return option;
    }));
    sortInput.value = list.sort;
    direction.textContent = list.descending ? "↓" : "↑";
    direction.title = list.descending ? "По убыванию" : "По возрастанию";
    if (view === "profiles") {
      const filters = list.filters;
      byId("filter-source").value = filters.source;
      byId("filter-photos").value = filters.photos;
      byId("filter-videos").value = filters.videos;
      byId("filter-psl").value = filters.psl;
      byId("filter-raw-min").value = filters.rawMin;
      byId("filter-raw-max").value = filters.rawMax;
      byId("filter-boosted").value = filters.boosted;
      byId("filter-age-min").value = filters.ageMin;
      byId("filter-age-max").value = filters.ageMax;
      const count = activeFilterCount();
      byId("filter-count").textContent = count ? `· ${count}` : "";
    }
  }

  function updatePager(items, view) {
    const list = lists[view];
    const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    list.page = Math.max(0, Math.min(list.page, pages - 1));
    const pager = byId("pager");
    pager.hidden = items.length <= PAGE_SIZE;
    byId("page-label").textContent = `${list.page + 1} / ${pages}`;
    byId("previous").disabled = list.page === 0;
    byId("next").disabled = list.page >= pages - 1;
    return items.slice(list.page * PAGE_SIZE, (list.page + 1) * PAGE_SIZE);
  }

  function emptyState(label, allowReset) {
    const root = element("div", "empty-state");
    root.append(element("p", "", label));
    if (allowReset) {
      const reset = element("button", "secondary", "Сбросить фильтры");
      reset.type = "button";
      reset.addEventListener("click", resetCurrentList);
      root.append(reset);
    }
    content.replaceChildren(root);
  }

  function renderList(view) {
    currentRoute = listRoute(view);
    saveInteractiveState();
    syncToolbar(view);
    const analyzeSelected = byId("analyze-selected");
    if (analyzeSelected) analyzeSelected.hidden = view !== "profiles";
    byId("title").textContent = view === "posts" ? "Сохранённые публикации" : "Все профили";
    const source = view === "profiles" ? filteredProfiles() : filteredPosts();
    const items = sorted(source, view);
    if (!items.length) {
      byId("pager").hidden = true;
      const filtered = lists[view].query || (view === "profiles" && activeFilterCount());
      emptyState(filtered ? "Ничего не найдено" : "Каталог пока пуст", Boolean(filtered));
      return;
    }
    const grid = element("div", "card-grid");
    const origin = listRoute("profiles");
    updatePager(items, view).forEach((item) => {
      grid.append(view === "profiles" ? profileCard(item, origin) : postCard(item));
    });
    content.replaceChildren(grid);
  }

  function localPager(items, pageSize, key, renderItem, gridClass = "media-grid") {
    const root = element("div", "");
    const render = (requested) => {
      const pages = Math.max(1, Math.ceil(items.length / pageSize));
      const selected = Math.max(0, Math.min(requested, pages - 1));
      sectionPages.set(key, selected);
      const grid = element("div", gridClass);
      items.slice(selected * pageSize, (selected + 1) * pageSize)
        .forEach((item, offset) => grid.append(renderItem(item, selected * pageSize + offset)));
      const nodes = [grid];
      if (pages > 1) {
        const controls = element("nav", "pager");
        controls.setAttribute("aria-label", "Страницы раздела");
        const previous = element("button", "", "← Назад");
        const next = element("button", "", "Дальше →");
        previous.disabled = selected === 0;
        next.disabled = selected >= pages - 1;
        previous.addEventListener("click", () => render(selected - 1));
        next.addEventListener("click", () => render(selected + 1));
        controls.append(previous, element("span", "", `${selected + 1} / ${pages}`), next);
        nodes.push(controls);
      }
      root.replaceChildren(...nodes);
    };
    render(sectionPages.get(key) || 0);
    return root;
  }

  function videoCard(video, gallery, index) {
    const card = element("div", "media-card");
    card.append(mediaNode(video, "video", "Видеокружок", gallery, index));
    card.append(meta([`комментарий ${video.message_id}`, dateText(video.message_date)]));
    card.append(originalLink(video.comment_url, "Исходный комментарий"));
    return card;
  }

  function showPost(sourceId) {
    const post = postMap.get(Number(sourceId));
    if (!post) {
      navigate(listRoute("posts"), true);
      return;
    }
    currentRoute = {type: "post", id: Number(sourceId)};
    toolbar.hidden = true;
    byId("pager").hidden = true;
    byId("title").textContent = clean(post.channel_title, `@${post.channel_username}`);
    const root = element("div", "detail");
    root.append(backButton(listRoute("posts"), "к источникам"));
    const heading = element("div", "detail-head");
    const body = element("div", "profile-copy");
    body.append(element("div", "eyebrow", post.channel_username ? `@${post.channel_username}` : "Telegram"));
    body.append(originalLink(post.url, `Открыть исходный пост #${post.post_id}`));
    if (post.text) body.append(element("p", "excerpt", post.text));
    body.append(meta([
      `опубликован: ${dateText(post.published_at)}`,
      `проверен: ${dateText(post.last_checked_at)}`,
      `${post.video_note_count || 0} кружков`,
      `${post.author_count || 0} профилей`,
      clean(post.last_error || post.last_result, post.enabled ? "ожидает проверки" : "источник отключён"),
    ]));
    heading.append(body);
    root.append(heading);
    if (interactiveEnabled) {
      const actions = element("div", "detail-actions");
      const analyzeAll = element("button", "primary-action", "Анализировать все локальные профили публикации");
      analyzeAll.type = "button";
      analyzeAll.addEventListener("click", () => beginAnalysis({source_id: Number(sourceId)}));
      const analyzeSelected = element("button", "secondary", "Анализировать выбранные");
      analyzeSelected.type = "button";
      analyzeSelected.addEventListener("click", () => {
        const relatedIds = new Set((post.profile_ids || []).map(Number));
        const ids = [...selectedProfiles].filter((id) => relatedIds.has(id));
        if (!ids.length) showNotice("Сначала выбери профили этой публикации.", true);
        else beginAnalysis({author_ids: ids});
      });
      actions.append(analyzeAll, analyzeSelected);
      root.append(actions);
    }

    if ((post.media || []).length) {
      root.append(element("h2", "section-title", "Медиа публикации"));
      root.append(localPager(post.media, MEDIA_PAGE_SIZE, `post-media:${sourceId}`, (item, index) => (
        mediaNode(item, item.kind, "Медиа публикации", post.media || [], index)
      )));
    }

    const postVideos = (post.video_ids || []).map((id) => videoMap.get(String(id))).filter(Boolean);
    root.append(element("h2", "section-title", "Видеокружки"));
    if (!postVideos.length) root.append(element("p", "muted", "Видеокружков пока нет"));
    else root.append(localPager(postVideos, VIDEO_PAGE_SIZE, `post-videos:${sourceId}`, (video, index) => (
      videoCard(video, postVideos, index)
    )));

    const related = (post.profile_ids || []).map((id) => profileMap.get(Number(id))).filter(Boolean);
    root.append(element("h2", "section-title", "Связанные профили"));
    if (!related.length) root.append(element("p", "muted", "Профили пока не найдены"));
    else root.append(localPager(
      related,
      PAGE_SIZE,
      `post-profiles:${sourceId}`,
      (profile) => profileCard(profile, {type: "post", id: Number(sourceId)}),
      "card-grid",
    ));
    content.replaceChildren(root);
  }

  function linkedPosts(profile) {
    const root = element("div", "detail-links");
    (profile.post_ids || []).forEach((id) => {
      const post = postMap.get(Number(id));
      if (!post) return;
      const row = element("div", "detail-link");
      const button = element("button", "", `${clean(post.channel_title, post.channel_username)} #${post.post_id}`);
      button.type = "button";
      button.addEventListener("click", () => navigate({type: "post", id: Number(post.source_id)}));
      row.append(button, originalLink(post.url, "Оригинал"));
      root.append(row);
    });
    return root.childNodes.length ? root : element("p", "muted", "Связанные публикации не сохранены");
  }

  function pslMetrics(item) {
    const metrics = element("div", "metrics");
    metrics.append(
      element("span", "metric", `presentation: ${clean(item.presentation)}`),
      element("span", "metric", `возраст ≈ ${clean(item.estimated_age)}`),
      element("span", "metric", `Raw ${scoreText(item.raw_psl_min)}–${scoreText(item.raw_psl_max)}`),
      element("span", "metric", `Boosted ${scoreText(item.boosted_psl)}`),
    );
    return metrics;
  }

  function pslHistoryEntry(item) {
    const row = element("article", "psl-entry");
    row.append(element(
      "div", "card-title", failureLabel(item.failure_category) || clean(item.status, "unknown"),
    ));
    row.append(meta([
      dateText(item.analyzed_at),
      item.actual_model ? `модель: ${item.actual_model}` : "модель не сохранена",
      item.prompt_version_id ? `prompt: ${item.prompt_version_id}` : "",
      item.schema_version ? `schema v${item.schema_version}` : "",
      item.attempt_id ? `attempt: ${item.attempt_id}` : "",
    ]));
    if (item.status === "completed") row.append(pslMetrics(item));
    return row;
  }

  function showProfile(authorId, origin) {
    const profile = profileMap.get(Number(authorId));
    if (!profile) {
      navigate(listRoute("profiles"), true);
      return;
    }
    const fallback = origin?.type === "post" ? origin : listRoute("profiles");
    currentRoute = {type: "profile", id: Number(authorId), origin: fallback};
    toolbar.hidden = true;
    byId("pager").hidden = true;
    byId("title").textContent = clean(profile.display_name, `ID ${profile.author_id}`);
    const root = element("div", "detail");
    root.append(backButton(fallback, fallback.type === "post" ? "к публикации" : "ко всем профилям"));
    const heading = element("div", "detail-head");
    heading.append(avatar(profile, true));
    const identity = element("div", "profile-copy");
    identity.append(element("div", "card-title", clean(profile.display_name, "Имя не сохранено")));
    identity.append(element("div", "eyebrow", profile.username ? `@${profile.username}` : "username не задан"));
    identity.append(meta([
      `Telegram ID ${profile.author_id}`,
      `впервые: ${dateText(profile.first_seen)}`,
      `активность: ${dateText(profile.last_activity || profile.last_seen)}`,
      `${(profile.photos || []).length} фото`,
      `${(profile.video_ids || []).length} кружков`,
      `${(profile.post_ids || []).length} источников`,
    ]));
    const latest = latestPSL(profile);
    if (latest) identity.append(element("span", "psl-badge", `PSL ${scoreText(latest.raw_psl_min)}–${scoreText(latest.raw_psl_max)}`));
    heading.append(identity);
    root.append(heading);
    if (interactiveEnabled && validProfileId(authorId)) {
      const actions = element("div", "detail-actions");
      const analyze = element("button", "primary-action", "Анализировать профиль");
      analyze.type = "button";
      analyze.addEventListener("click", () => beginAnalysis({author_ids: [Number(authorId)]}));
      actions.append(analyze);
      root.append(actions);
    }

    root.append(element("h2", "section-title", "История username"));
    const usernames = profile.username_history || [];
    if (!usernames.length) root.append(element("p", "muted", "Username не наблюдался"));
    else {
      const history = element("ul", "history");
      usernames.forEach((item) => history.append(element(
        "li", "", `@${item.username} · ${dateText(item.first_seen)} — ${dateText(item.last_seen)}`,
      )));
      root.append(history);
    }

    root.append(element("h2", "section-title", "Связанные публикации"), linkedPosts(profile));

    root.append(element("h2", "section-title", "Фотографии"));
    const photos = profile.photos || [];
    if (!photos.length) root.append(element("p", "muted", profile.photo_error || "Сохранённых фотографий нет"));
    else root.append(localPager(photos, MEDIA_PAGE_SIZE, `profile-photos:${authorId}`, (photo, index) => (
      mediaNode(photo, "image", "Фото профиля", photos, index)
    )));

    root.append(element("h2", "section-title", "Видеокружки"));
    const profileVideos = (profile.video_ids || []).map((id) => videoMap.get(String(id))).filter(Boolean);
    if (!profileVideos.length) root.append(element("p", "muted", "Видеокружков нет"));
    else root.append(localPager(profileVideos, VIDEO_PAGE_SIZE, `profile-videos:${authorId}`, (video, index) => (
      videoCard(video, profileVideos, index)
    )));

    root.append(element("h2", "section-title", "PSL-анализ"));
    root.append(element(
      "p", "subjective",
      "PSL — субъективная, ненаучная визуальная оценка. Она не является фактом или объективным измерением.",
    ));
    if (latest) {
      const latestBlock = element("div", "psl-latest");
      latestBlock.append(element("div", "card-title", "Последняя допустимая оценка"));
      latestBlock.append(pslMetrics(latest));
      latestBlock.append(meta([
        dateText(latest.analyzed_at),
        latest.actual_model ? `модель: ${latest.actual_model}` : "",
        latest.prompt_version_id ? `prompt: ${latest.prompt_version_id}` : "",
      ]));
      root.append(latestBlock);
    } else root.append(element("p", "muted", "Допустимой завершённой оценки нет"));

    const analyses = Array.isArray(profile.psl_history) ? profile.psl_history : [];
    root.append(element("h3", "section-title", "История анализов"));
    if (!analyses.length) root.append(element("p", "muted", "Анализы ещё не запускались"));
    else root.append(localPager(
      analyses, HISTORY_PAGE_SIZE, `profile-psl:${authorId}`, pslHistoryEntry, "psl-history",
    ));
    content.replaceChildren(root);
  }

  function renderRoute(route = parseRoute()) {
    if (route.type === "post") showPost(route.id);
    else if (route.type === "profile") showProfile(route.id, route.origin);
    else renderList(route.view);
  }

  function resetCurrentList() {
    const view = currentRoute?.type === "list" ? currentRoute.view : "posts";
    lists[view].query = "";
    lists[view].page = 0;
    lists[view].descending = true;
    lists[view].sort = view === "profiles" ? "last_active" : "published";
    if (view === "profiles") lists.profiles.filters = emptyFilters();
    renderList(view);
    window.scrollTo(0, 0);
  }

  function delayedListRender(view) {
    window.clearTimeout(inputTimer);
    inputTimer = window.setTimeout(() => {
      if (currentRoute?.type === "list" && currentRoute.view === view) renderList(view);
    }, 160);
  }

  function setSourceOptions() {
    const first = element("option", "", "Все источники");
    first.value = "";
    const options = [first, ...posts.map((post) => {
      const option = element("option", "", `${clean(post.channel_title, post.channel_username)} #${post.post_id}`);
      option.value = String(post.source_id);
      return option;
    })];
    byId("filter-source").replaceChildren(...options);
  }

  queryInput.addEventListener("input", () => {
    if (currentRoute?.type !== "list") return;
    const view = currentRoute.view;
    lists[view].query = queryInput.value;
    lists[view].page = 0;
    delayedListRender(view);
  });
  sortInput.addEventListener("change", () => {
    if (currentRoute?.type !== "list") return;
    const list = lists[currentRoute.view];
    list.sort = sortInput.value;
    list.page = 0;
    renderList(currentRoute.view);
  });
  direction.addEventListener("click", () => {
    if (currentRoute?.type !== "list") return;
    const list = lists[currentRoute.view];
    list.descending = !list.descending;
    list.page = 0;
    renderList(currentRoute.view);
  });
  byId("reset").addEventListener("click", resetCurrentList);
  byId("show-posts").addEventListener("click", () => navigate(listRoute("posts")));
  byId("show-profiles").addEventListener("click", () => navigate(listRoute("profiles")));
  byId("home").addEventListener("click", () => navigate(listRoute("posts")));
  byId("previous").addEventListener("click", () => {
    if (currentRoute?.type !== "list") return;
    lists[currentRoute.view].page -= 1;
    renderList(currentRoute.view);
    window.scrollTo(0, 0);
  });
  byId("next").addEventListener("click", () => {
    if (currentRoute?.type !== "list") return;
    lists[currentRoute.view].page += 1;
    renderList(currentRoute.view);
    window.scrollTo(0, 0);
  });

  const filterBindings = [
    ["filter-source", "source"], ["filter-photos", "photos"],
    ["filter-videos", "videos"], ["filter-psl", "psl"],
    ["filter-raw-min", "rawMin"], ["filter-raw-max", "rawMax"],
    ["filter-boosted", "boosted"], ["filter-age-min", "ageMin"],
    ["filter-age-max", "ageMax"],
  ];
  filterBindings.forEach(([id, key]) => {
    const node = byId(id);
    const eventName = node.tagName === "INPUT" ? "input" : "change";
    node.addEventListener(eventName, () => {
      lists.profiles.filters[key] = node.value;
      lists.profiles.page = 0;
      if (eventName === "input") delayedListRender("profiles");
      else renderList("profiles");
    });
  });

  byId("viewer-close").addEventListener("click", closeViewer);
  byId("viewer-previous").addEventListener("click", () => moveViewer(-1));
  byId("viewer-next").addEventListener("click", () => moveViewer(1));
  viewer.addEventListener("close", () => byId("viewer-stage").replaceChildren());
  viewer.addEventListener("click", (event) => { if (event.target === viewer) closeViewer(); });
  document.addEventListener("keydown", (event) => {
    if (!viewer.open) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); moveViewer(-1); }
    if (event.key === "ArrowRight") { event.preventDefault(); moveViewer(1); }
    if (event.key === "Escape") closeViewer();
  });

  window.addEventListener("popstate", (event) => {
    const route = event.state?.route || parseRoute();
    renderRoute(route);
    window.requestAnimationFrame(() => window.scrollTo(0, Number(event.state?.scrollY || 0)));
  });
  window.addEventListener("hashchange", () => {
    const parsed = parseRoute();
    if (!currentRoute || routeHash(parsed) !== routeHash(currentRoute)) renderRoute(parsed);
  });
  window.addEventListener("beforeunload", rememberScroll);

  setSourceOptions();
  byId("summary").textContent = `${posts.length} публикаций · ${profiles.length} профилей · ${videos.length} кружков`;
  byId("generated").textContent = `Снимок: ${dateText(catalog.generated_at)}`;
  byId("notice").hidden = true;
  restoreInteractiveState();
  initInteractive();
  const initialRoute = parseRoute();
  history.replaceState({hoard: true, route: initialRoute, depth: 0, scrollY: 0}, "", routeHash(initialRoute));
  renderRoute(initialRoute);
})();
