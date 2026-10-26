(() => {
  "use strict";

  let catalog = window.HOARD_CATALOG || {};
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
  let posts = Array.isArray(catalog.posts) ? catalog.posts : [];
  let profiles = Array.isArray(catalog.profiles) ? catalog.profiles : [];
  let videos = Array.isArray(catalog.videos) ? catalog.videos : [];
  let postMap = new Map(posts.map((item) => [Number(item.source_id), item]));
  let profileMap = new Map(profiles.map((item) => [Number(item.author_id), item]));
  let videoMap = new Map(videos.map((item) => [String(item.video_id), item]));
  let profileSearchIndex = new Map();
  let postSearchIndex = new Map();
  const sectionPages = new Map();
  const observers = [];
  const selectedProfiles = new Set();
  const validProfileId = (value) => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  let currentRoute = null;
  let inputTimer = null;
  let catalogToken = String(interactive && interactive.catalog || "");
  let catalogUpdateTimer = null;
  let catalogUpdateFailures = 0;
  let catalogErrorShown = false;
  let pendingCatalogRender = false;

  const emptyFilters = () => ({
    source: "", photos: "any", videos: "any", psl: "any",
    rawMin: "", rawMax: "", boosted: "", ageMin: "", ageMax: "",
  });
  const lists = {
    posts: {query: "", sort: "published", descending: true, visible: PAGE_SIZE},
    profiles: {
      query: "", sort: "last_active", descending: true, visible: PAGE_SIZE,
      filters: interactiveEnabled ? {...emptyFilters(), photos: "yes"} : emptyFilters(),
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
        const restored = Number(source.visible ?? source.page ?? 0);
        lists[view].visible = Math.max(PAGE_SIZE, Number.isFinite(restored) ? restored : PAGE_SIZE);
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

  const SVG_NS = "http://www.w3.org/2000/svg";
  function icon(name, size = 14) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", String(size));
    svg.setAttribute("height", String(size));
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("aria-hidden", "true");
    const paths = {
      photo: ["M3 3h18v18H3z", "M8 8h.01", "m3 17 5-5 4 4 4-6 5 7"],
      video: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "m10 8 6 4-6 4z"],
      link: ["M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7.1-7L12 5",
        "M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7.1 7L12 19"],
      arrow: ["m14 5-7 7 7 7"],
    };
    (paths[name] || []).forEach((value) => {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", value);
      svg.append(path);
    });
    return svg;
  }

  function sectionBlock(title, count, body, note = "") {
    const section = element("section", "section");
    const head = element("div", "section-head");
    head.append(element("h2", "", title));
    if (count !== null && count !== undefined) {
      head.append(element("span", "count", String(count)));
    }
    section.append(head);
    if (note) section.append(element("p", "section-desc", note));
    if (body) section.append(body);
    return section;
  }

  function disconnectObservers() {
    observers.splice(0).forEach((observer) => observer.disconnect());
  }

  function dateText(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("ru-RU");
  }

  function dayText(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleDateString("ru-RU");
  }

  function plural(value, forms) {
    const number = Math.abs(Math.trunc(Number(value) || 0)) % 100;
    const last = number % 10;
    if (number > 10 && number < 20) return forms[2];
    if (last === 1) return forms[0];
    if (last >= 2 && last <= 4) return forms[1];
    return forms[2];
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
    const close = element("button", "secondary", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть");
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

  function operationStorageKey(selector, imageIds, imageMode = "photos", videoIds = {}) {
    const authors = Array.isArray(selector.author_ids)
      ? selector.author_ids.map(Number).filter(validProfileId).sort((a, b) => a - b)
      : [];
    const images = Object.fromEntries(Object.keys(imageIds).sort().map((id) => [
      id, [...imageIds[id]].map(String),
    ]));
    const videos = Object.fromEntries(Object.keys(videoIds).sort().map((id) => [
      id, String(videoIds[id]),
    ]));
    return `hoard.ai.operation:${JSON.stringify({
      authors,
      source_id: Number.isSafeInteger(Number(selector.source_id)) ? Number(selector.source_id) : null,
      source_url: String(selector.source_url || ""),
      image_mode: String(imageMode || "photos"),
      images,
      videos,
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

  function previewPhotoURL(authorId, photoId) {
    const profile = profileMap.get(Number(authorId));
    const photo = ((profile && profile.photos) || []).find((item) => (
      String(item.photo_id) === String(photoId) && item.available && localMediaURL(item.url)
    ));
    return photo ? localMediaURL(photo.url) : "";
  }

  function previewFrameURL(image) {
    const video = videoMap.get(String(image.video_id || ""));
    if (!video) return "";
    const position = Number(image.position || 0);
    const frame = (video.frames || []).find((item) => (
      Number(item.position || 0) === position && item.available && localMediaURL(item.url)
    )) || (video.frames || [])[position - 1];
    return frame && frame.available ? localMediaURL(frame.url) : "";
  }

  function previewTile(label, url) {
    const tile = element("div", "preview-tile");
    if (url) {
      const image = element("img", "");
      image.loading = "lazy";
      image.decoding = "async";
      image.alt = label;
      image.src = url;
      tile.append(image);
    }
    tile.append(element("span", "", label));
    return tile;
  }

  function analysisPreview(preview, selector) {
    const imageMode = String(preview.image_mode || selector.image_mode || "photos");
    const selectedKey = imageMode === "photos" ? "selected_photo_ids" : "selected_image_ids";
    const title = preview.source
      ? clean(preview.source.channel_title || preview.source.title, `@${preview.source.username}`)
      : (preview.selected_count === 1
        ? clean(preview.items?.[0]?.display_name || preview.items?.[0]?.username, "Сохранённый профиль")
        : "Анализ выбранных профилей");
    const {dialog, body} = modal("Telegram AI · Предпросмотр");
    body.append(element("div", "kicker workflow-kicker", preview.source
      ? "ПУБЛИКАЦИЯ / ЛОКАЛЬНЫЕ ИЗОБРАЖЕНИЯ"
      : "ПРОФИЛЬ / ЛОКАЛЬНЫЕ ИЗОБРАЖЕНИЯ"));
    body.append(element("div", "workflow-title", title));
    body.append(element(
      "p", "muted",
      "Выбери уже сохранённые изображения. Предпросмотр не вызывает AI и не передаёт данные наружу.",
    ));
    if (preview.source) {
      body.append(element(
        "p", "muted",
        `${clean(preview.source.channel_title || preview.source.title, `@${preview.source.username}`)} #${preview.source.post_id}`,
      ));
    }

    const modeLabels = {
      photos: "Фото профиля",
      frames: "Кадры кружка",
      combined: "3 кадра + фото",
    };
    const modeChips = element("div", "mode-chips");
    ["photos", "frames", "combined"].forEach((mode) => {
      const button = element("button", `mode${mode === imageMode ? " active" : ""}`, modeLabels[mode]);
      button.type = "button";
      button.setAttribute("aria-pressed", mode === imageMode ? "true" : "false");
      button.addEventListener("click", () => {
        if (mode === imageMode) return;
        dialog.close();
        const base = {...selector};
        delete base.image_mode;
        loadAnalysis(base, mode);
      });
      modeChips.append(button);
    });
    body.append(modeChips);
    body.append(element(
      "p", "workflow-note",
      "Промпт: встроенный Telegram PSL. Модель и лимиты берутся из сохранённой конфигурации AI.",
    ));

    const modeLabel = modeLabels[imageMode] || "фотографии профиля";
    body.append(element(
      "p", "workflow-summary",
      `Выбрано ${preview.selected_count} · источник изображений: ${modeLabel}. `
        + "На этом шаге запрос к провайдеру не выполняется.",
    ));

    const pageLimit = Number(preview.limit || 25);
    const pages = new Map([[Number(preview.offset || 0), preview]]);
    const photoIds = {};
    const imageIds = {};
    const photoSnapshots = {};
    const imageSnapshots = {};
    const videoIds = {};
    let current = preview;
    let photoInputs = [];
    const tileStrip = element("div", "preview-strip");
    function ingest(page) {
      (page.items || []).forEach((item) => {
        if (!validProfileId(item.author_id)) return;
        const key = String(item.author_id);
        const selected = (item[selectedKey] || []).map(String);
        if (!selected.length) return;
        if (imageMode === "photos") photoIds[key] = [...selected];
        imageIds[key] = [...selected];
        const snapshot = String(item.image_snapshot || item.photo_snapshot || "");
        if (snapshot) {
          imageSnapshots[key] = snapshot;
          if (imageMode === "photos") photoSnapshots[key] = snapshot;
        }
        if (item.video_id && imageMode !== "photos") videoIds[key] = String(item.video_id);
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
          method: "POST",
          body: {...selector, image_mode: imageMode, limit: pageLimit, offset},
        });
        pages.set(offset, result.preview);
        ingest(result.preview);
      }
      return pages.get(offset);
    }
    function renderPreviewPage(page) {
      current = page;
      photoInputs = [];
      tileStrip.replaceChildren();
      const rows = [];
      const reasons = new Map();
      (page.items || []).forEach((item) => {
        const row = element("div", "workflow-row");
        row.append(element(
          "strong", "", clean(item.display_name || item.username, `ID ${item.author_id}`),
        ));
        const countLabel = imageMode === "photos"
          ? `${item.photo_count} фото`
          : `${item.frame_count || 0} кадров${imageMode === "combined" ? " + фото" : ""}`;
        row.append(element("span", "muted", `ID ${item.author_id} · ${countLabel}`));
        if (imageMode === "photos" && preview.selected_count === 1) {
          const choices = element("div", "photo-choices");
          const defaults = new Set(photoIds[String(item.author_id)] || []);
          (item.photos || []).filter((photo) => photo.available).forEach((photo) => {
            const label = element("label", "photo-choice");
            const input = element("input", "");
            input.type = "checkbox";
            input.value = String(photo.photo_id);
            input.checked = defaults.has(String(photo.photo_id));
            photoInputs.push(input);
            const url = previewPhotoURL(item.author_id, photo.photo_id);
            if (url) {
              const image = element("img", "");
              image.loading = "lazy";
              image.decoding = "async";
              image.alt = `Фото ${photo.photo_id}`;
              image.src = url;
              label.append(image);
            }
            label.append(input, element("span", "", `Фото · ${dayText(photo.date)}`));
            choices.append(label);
          });
          row.append(choices);
        }
        if (imageMode !== "photos") {
          if (item.video_id) {
            const choices = Array.isArray(item.videos) ? item.videos : [];
            if (preview.selected_count === 1 && choices.length > 1) {
              const videoField = element("label", "field");
              videoField.append(element("span", "", "Сохранённый видеокружок"));
              const select = element("select", "");
              choices.forEach((video) => {
                const option = element("option", "", `Кружок · сообщение #${video.message_id}`);
                option.value = String(video.video_id);
                option.selected = String(video.video_id) === String(item.video_id);
                select.append(option);
              });
              select.addEventListener("change", () => {
                const base = {...selector};
                delete base.image_mode;
                base.video_ids = {...(selector.video_ids || {}), [String(item.author_id)]: select.value};
                dialog.close();
                loadAnalysis(base, imageMode);
              });
              videoField.append(select);
              row.append(videoField);
            }
            const frames = (item.selected_images || [])
              .filter((image) => image.kind === "frame")
              .map((image) => `${Number(image.timestamp_seconds || 0).toFixed(2)} с`);
            row.append(element(
              "span", "muted",
              `видео ${item.video_id}` + (frames.length ? ` · ${frames.join(" · ")}` : ""),
            ));
            (item.selected_images || []).filter((image) => image.kind === "frame").forEach((image) => {
              if (preview.selected_count !== 1) return;
              const url = previewFrameURL(image);
              const label = `Кадр ${image.position} · ${Number(image.timestamp_seconds || 0).toFixed(2)} с`;
              tileStrip.append(previewTile(label, url));
            });
            (item.selected_images || []).filter((image) => image.kind === "profile_photo").forEach((image) => {
              if (preview.selected_count !== 1) return;
              const url = previewPhotoURL(item.author_id, image.image_id);
              tileStrip.append(previewTile("Фото профиля", url));
            });
          }
        }
        const previous = item.previous;
        if (previous) {
          row.append(element(
            "span", "muted",
            `Предыдущий результат: ${previous.result_status || previous.attempt_state || "—"}`,
          ));
        }
        if (!(item[selectedKey] || []).length) {
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
      pageActions.hidden = preview.selected_count <= pageLimit;
      tileStrip.hidden = !tileStrip.childNodes.length;
    }
    previousPage.type = nextPage.type = "button";
    previousPage.addEventListener("click", async () => {
      renderPreviewPage(await loadPage(Math.max(0, Number(current.offset || 0) - pageLimit)));
    });
    nextPage.addEventListener("click", async () => {
      renderPreviewPage(await loadPage(Number(current.offset || 0) + pageLimit));
    });
    const pageActions = element("div", "workflow-page-actions");
    const pageNavigation = element("div", "interactive-actions");
    pageNavigation.append(previousPage, nextPage);
    pageActions.append(pageLabel, pageNavigation);
    body.append(pageActions, list, skippedSummary, tileStrip);
    renderPreviewPage(preview);

    body.append(element(
      "p", "workflow-note",
      "Возрастной допуск 18+ применяется автоматически. Будут отправлены только выбранные "
        + "локальные изображения; запрос DeepSeek может быть платным, автоматического повтора нет.",
    ));
    const feedback = element("p", "notice-inline");
    const count = Number(
      imageMode === "photos" ? preview.selected_photo_count : preview.selected_image_count
    );
    const noun = imageMode === "photos" ? "фото" : "изображений";
    const footer = element("div", "workflow-footer");
    const start = element(
      "button", "primary-action", `Запустить анализ · ${count} ${noun}`,
    );
    start.type = "button";
    start.addEventListener("click", async () => {
      if (imageMode === "photos" && preview.selected_count === 1) {
        const selected = photoInputs.filter((input) => input.checked).map((input) => input.value);
        if (selected.length < 1 || selected.length > 4) {
          feedback.textContent = "Выбери от одной до четырёх фотографий.";
          return;
        }
        photoIds[String(preview.items[0].author_id)] = selected;
        imageIds[String(preview.items[0].author_id)] = selected;
        if (JSON.stringify(selected) !== JSON.stringify(preview.items[0].selected_photo_ids || [])) {
          delete photoSnapshots[String(preview.items[0].author_id)];
          delete imageSnapshots[String(preview.items[0].author_id)];
        }
      }
      if (!Object.keys(imageIds).length) {
        feedback.textContent = "Нет подходящих локальных профилей с изображениями.";
        return;
      }
      start.disabled = true;
      try {
        for (let offset = 0; offset < preview.selected_count; offset += pageLimit) {
          await loadPage(offset);
        }
        const storageKey = operationStorageKey(
          selector,
          imageMode === "photos" ? photoIds : imageIds,
          imageMode,
          videoIds,
        );
        const result = await api("/api/ai/start", {
          method: "POST",
          body: {
            ...selector,
            image_mode: imageMode,
            photo_ids: imageMode === "photos" ? photoIds : {},
            photo_snapshots: photoSnapshots,
            image_ids: imageIds,
            image_snapshots: imageSnapshots,
            video_ids: videoIds,
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
    feedback.addEventListener("click", () => { feedback.textContent = ""; });
    footer.append(feedback, start);
    body.append(footer);
  }

  async function loadAnalysis(selector, imageMode = "photos") {
    try {
      const result = await api("/api/ai/preview", {
        method: "POST", body: {...selector, image_mode: imageMode, limit: 25, offset: 0},
      });
      analysisPreview(result.preview, {...selector, image_mode: imageMode});
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  function beginAnalysis(selector, imageMode = "photos") {
    ensureAI(() => loadAnalysis(selector, imageMode));
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
    const topEnd = byId("top-end");
    if (topEnd) topEnd.append(settings);
    const footer = document.querySelector("footer .footer-row");
    if (footer) footer.append(element("span", "", "интерактивный localhost-режим"));
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
    const actions = element("div", "interactive-actions selection-row");
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
      if (global) {
        global.textContent = `Анализировать выбранные (${selectedProfiles.size})`;
        global.hidden = !selectedProfiles.size;
      }
    });
    label.append(checkbox, element("span", "", "Выбрать"));
    actions.append(label);
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

  function trackImageLoad(image) {
    image.addEventListener("load", () => { image.dataset.loaded = "true"; }, {once: true});
    image.addEventListener("error", () => { image.dataset.loadError = "true"; }, {once: true});
    return image;
  }

  function avatar(profile, large = false) {
    const photo = profilePhotos(profile)[0];
    const className = `avatar${large ? " large" : ""}`;
    if (!photo) return element("div", `${className} initials`, initials(profile));
    const image = element("img", className);
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = "";
    trackImageLoad(image);
    image.src = localMediaURL(photo.url);
    return image;
  }

  function profilePhotos(profile) {
    return (profile.photos || []).filter((item) => (
      item && item.available && localMediaURL(item.url)
    ));
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

  function rebuildSearchIndexes() {
    profileSearchIndex = new Map();
    postSearchIndex = new Map();
    profiles.forEach((profile) => profileSearchIndex.set(Number(profile.author_id), profileSearch(profile)));
    posts.forEach((post) => postSearchIndex.set(Number(post.source_id), postSearch(post)));
  }

  function playbackActive() {
    if (viewer.open) return true;
    return [...document.querySelectorAll("video")].some((video) => !video.paused && !video.ended);
  }

  function renderAfterCatalogUpdate() {
    const scrollY = window.scrollY;
    saveInteractiveState();
    renderRoute(currentRoute || parseRoute());
    window.requestAnimationFrame(() => window.scrollTo(0, scrollY));
  }

  function flushPendingCatalog() {
    if (!pendingCatalogRender || playbackActive()) return;
    pendingCatalogRender = false;
    renderAfterCatalogUpdate();
  }

  function applyCatalogSnapshot(snapshot, token) {
    if (!snapshot || typeof snapshot !== "object") return;
    catalog = snapshot;
    posts = Array.isArray(snapshot.posts) ? snapshot.posts : [];
    profiles = Array.isArray(snapshot.profiles) ? snapshot.profiles : [];
    videos = Array.isArray(snapshot.videos) ? snapshot.videos : [];
    postMap = new Map(posts.map((item) => [Number(item.source_id), item]));
    profileMap = new Map(profiles.map((item) => [Number(item.author_id), item]));
    videoMap = new Map(videos.map((item) => [String(item.video_id), item]));
    rebuildSearchIndexes();
    for (const id of [...selectedProfiles]) {
      if (!profileMap.has(Number(id))) selectedProfiles.delete(id);
    }
    const selectedButton = byId("analyze-selected");
    if (selectedButton) selectedButton.textContent = `Анализировать выбранные (${selectedProfiles.size})`;
    setSourceOptions();
    byId("summary").textContent = `${posts.length} публикаций · ${profiles.length} профилей · ${videos.length} кружков`;
    byId("generated").textContent = `Снимок: ${dateText(catalog.generated_at)}`;
    catalogToken = String(token || "");
    if (playbackActive()) {
      pendingCatalogRender = true;
      return;
    }
    renderAfterCatalogUpdate();
  }

  async function checkCatalogUpdate() {
    if (!interactiveEnabled) return;
    if (document.visibilityState === "visible") {
      try {
        const version = await api("/api/catalog/version");
        if (version.token && version.token !== catalogToken) {
          const snapshot = await api("/api/catalog/snapshot");
          if (snapshot.token && snapshot.catalog) {
            applyCatalogSnapshot(snapshot.catalog, snapshot.token);
          }
        }
        catalogUpdateFailures = 0;
        if (catalogErrorShown) {
          catalogErrorShown = false;
          showNotice("");
        }
      } catch (error) {
        catalogUpdateFailures += 1;
        if (!catalogErrorShown) {
          catalogErrorShown = true;
          showNotice(`Не удалось получить обновление каталога: ${error.message}`, true);
        }
      }
    }
    const visible = document.visibilityState === "visible";
    const delay = visible
      ? Math.min(30000, 3000 * (2 ** Math.min(catalogUpdateFailures, 4)))
      : 15000;
    window.clearTimeout(catalogUpdateTimer);
    catalogUpdateTimer = window.setTimeout(checkCatalogUpdate, delay);
  }

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
    const defaultView = interactiveEnabled ? "profiles" : "posts";
    return listRoute(parts[0] === "profiles" ? "profiles" : (parts[0] ? "posts" : defaultView));
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
    focusTitle();
  }

  function focusTitle() {
    const title = byId("title");
    if (!title || typeof title.focus !== "function") return;
    title.tabIndex = -1;
    title.focus({preventScroll: true});
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
    window.setTimeout(flushPendingCatalog, 0);
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
    byId("viewer-label").textContent = `Просмотр · ${viewerState.label}`;
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
    trackImageLoad(image);
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
      if (!boolFilter(filters.photos, profilePhotos(profile).length > 0)) return false;
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
    if (key === "media_count") return (profile.video_ids || []).length + profilePhotos(profile).length;
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
    const card = element("article", "card person-card");
    const button = element("button", "person-main");
    button.type = "button";
    const head = element("div", "person-head");
    head.append(avatar(profile));
    const info = element("div", "person-info");
    info.append(element("div", "person-name", clean(profile.display_name, `ID ${profile.author_id}`)));
    info.append(element("div", "person-handle", profile.username ? `@${profile.username}` : "без username"));
    info.append(element(
      "div", "person-id mono", `ID ${profile.author_id}`,
    ));
    head.append(info);
    const psl = latestPSL(profile);
    if (psl) head.append(element("span", "mini-psl", `PSL ${scoreText(psl.raw_psl_min)}–${scoreText(psl.raw_psl_max)}`));
    const stats = element("div", "person-stats");
    [
      ["photo", `${profilePhotos(profile).length} ${plural(profilePhotos(profile).length, ["фото", "фото", "фото"])}`],
      ["video", `${(profile.video_ids || []).length} ${plural((profile.video_ids || []).length, ["кружок", "кружка", "кружков"])}`],
      ["link", `${(profile.post_ids || []).length} ${plural((profile.post_ids || []).length, ["источник", "источника", "источников"])}`],
    ].forEach(([name, text]) => {
      const item = element("span", "person-stat");
      item.append(icon(name), element("span", "", text));
      stats.append(item);
    });
    const date = element("div", "person-date");
    date.append(
      element("em", "", `Активность: ${dateText(profile.last_activity || profile.last_seen)}`),
      element("span", "", "Открыть →"),
    );
    button.append(head, stats, date);
    button.addEventListener("click", () => navigate({type: "profile", id: Number(profile.author_id), origin}));
    card.append(button);
    if (interactiveEnabled) card.append(selectionControls(profile));
    return card;
  }

  function postPreview(post) {
    const item = (post.media || []).find(available);
    if (!item) return null;
    const cover = element("div", "source-cover");
    if (isVideoItem(item)) {
      cover.append(element("div", "video-placeholder", "▶ Видео"));
    } else {
      const image = element("img", "");
      image.loading = "lazy";
      image.decoding = "async";
      image.alt = "Медиа публикации";
      image.src = localMediaURL(item.url);
      cover.append(image);
    }
    cover.append(element("span", "source-id", `#${post.post_id}`));
    return cover;
  }

  function postCard(post) {
    const card = element("article", "source-card");
    const button = element("button", "source-main");
    button.type = "button";
    const preview = postPreview(post);
    if (preview) button.append(preview);
    const copy = element("div", "");
    const handle = [
      post.channel_username ? `@${post.channel_username}` : "Telegram",
      `#${post.post_id}`,
    ].join(" · ");
    copy.append(element("div", "source-handle", handle));
    copy.append(element("div", "source-title", clean(post.channel_title, `Пост #${post.post_id}`)));
    copy.append(element(
      "div", "excerpt source-desc",
      post.text || "Текст публикации не сохранён.",
    ));
    const stats = element("div", "source-stat");
    stats.append(
      element("span", "", `${post.author_count || 0} ${plural(post.author_count || 0, ["профиль", "профиля", "профилей"])}`),
      element("span", "", `${post.video_note_count || 0} ${plural(post.video_note_count || 0, ["кружок", "кружка", "кружков"])}`),
      element("span", "source-time", dateText(post.published_at)),
    );
    copy.append(stats);
    button.append(copy);
    button.addEventListener("click", () => navigate({type: "post", id: Number(post.source_id)}));
    card.append(button);
    const action = element("div", "source-action");
    if (interactiveEnabled) {
      const analyze = element("button", "source-ai", "Анализировать профили");
      analyze.type = "button";
      analyze.addEventListener("click", () => beginAnalysis({source_id: Number(post.source_id)}));
      action.append(analyze);
    } else {
      action.append(element("span", "", "Сохранённая публикация"));
    }
    const open = element("button", "source-open", "Открыть →");
    open.type = "button";
    open.addEventListener("click", () => navigate({type: "post", id: Number(post.source_id)}));
    action.append(open);
    card.append(action);
    return card;
  }

  function activeFilterCount() {
    const filters = lists.profiles.filters;
    return Object.values(filters).filter((value) => value && value !== "any").length;
  }

  function setNavActive(view) {
    byId("show-posts").classList.toggle("active", view === "posts");
    byId("show-profiles").classList.toggle("active", view === "profiles");
    byId("show-posts").setAttribute("aria-current", view === "posts" ? "page" : "false");
    byId("show-profiles").setAttribute("aria-current", view === "profiles" ? "page" : "false");
  }

  function syncToolbar(view) {
    toolbar.hidden = false;
    profileFilters.hidden = view !== "profiles";
    setNavActive(view);
    queryInput.placeholder = view === "profiles"
      ? "Имя, @username, Telegram ID или источник"
      : "Название, @канал или текст публикации";
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
    const analyzeSelected = byId("analyze-selected");
    if (analyzeSelected) {
      analyzeSelected.hidden = view !== "profiles" || !selectedProfiles.size;
    }
  }

  function incrementalBlock(items, pageSize, initial, onLoaded, renderItem, gridClass) {
    const root = element("div", "incremental-block");
    const grid = element("div", gridClass);
    const marker = element("div", "load-marker");
    const area = element("div", "load-area");
    area.setAttribute("aria-live", "polite");
    let loaded = Math.max(0, Math.min(items.length, Math.trunc(initial) || 0));
    if (!loaded && items.length) loaded = Math.min(items.length, pageSize);
    const draw = (from, to) => {
      for (let index = from; index < to; index += 1) {
        grid.append(renderItem(items[index], index));
      }
    };
    const fallback = element("button", "secondary load-more", "Показать ещё");
    fallback.type = "button";
    fallback.addEventListener("click", () => loadMore());
    const update = () => {
      onLoaded(loaded);
      area.replaceChildren();
      if (loaded < items.length) {
        area.append(
          element("div", "", "Подгружаем записи по мере прокрутки"),
          element("div", "sub", `Показано ${loaded} из ${items.length}`),
        );
        if (!("IntersectionObserver" in window)) area.append(fallback);
        marker.hidden = false;
      } else {
        area.append(
          element("div", "", `Показано ${items.length} записей`),
          element("div", "sub", "Конец списка"),
        );
        marker.hidden = true;
      }
    };
    const loadMore = () => {
      if (loaded >= items.length) return;
      const next = Math.min(items.length, loaded + pageSize);
      draw(loaded, next);
      loaded = next;
      update();
    };
    draw(0, loaded);
    update();
    if (loaded < items.length) {
      if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadMore();
        }, {rootMargin: "600px 0px"});
        observer.observe(marker);
        observers.push(observer);
      }
    }
    root.append(grid, marker, area);
    return root;
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
    disconnectObservers();
    syncToolbar(view);
    byId("list-meta").hidden = false;
    const analyzeSelected = byId("analyze-selected");
    if (analyzeSelected) analyzeSelected.hidden = view !== "profiles" || !selectedProfiles.size;
    const titleText = view === "posts" ? "Источники" : "Все профили";
    const source = view === "profiles" ? filteredProfiles() : filteredPosts();
    const items = sorted(source, view);
    const h1 = element("h1", "", titleText);
    h1.id = "title";
    const titleLine = element("div", "title-line");
    titleLine.append(h1, element("span", "number", String(items.length)));
    const head = element("div", "headrow");
    const left = element("div", "head-left");
    left.append(
      element("div", "kicker", "TELEGRAM / АРХИВ"),
      titleLine,
      element(
        "p", "head-sub",
        view === "profiles"
          ? "Сохранённые профили, локальные фотографии и видеокружки"
          : "Публикации, по которым Hoard сохраняет локальные материалы",
      ),
    );
    head.append(left);
    byId("page-head").replaceChildren(head);
    const listMeta = byId("list-meta");
    listMeta.replaceChildren(
      element("strong", "", `${view === "profiles" ? "Профили" : "Сохранённые публикации"} · ${items.length}`),
      element("span", "", "Локальные данные"),
    );
    if (!items.length) {
      const filtered = lists[view].query || (view === "profiles" && activeFilterCount());
      emptyState(filtered ? "Ничего не найдено" : "Каталог пока пуст", Boolean(filtered));
      return;
    }
    const origin = listRoute("profiles");
    const root = incrementalBlock(
      items,
      PAGE_SIZE,
      lists[view].visible,
      (loaded) => { lists[view].visible = loaded; saveInteractiveState(); },
      (item) => (view === "profiles" ? profileCard(item, origin) : postCard(item)),
      view === "profiles" ? "person-grid" : "source-grid",
    );
    content.replaceChildren(root);
  }

  function incrementalSection(items, pageSize, key, renderItem, gridClass = "media-grid") {
    const saved = Number(sectionPages.get(key) || 0);
    return incrementalBlock(
      items,
      pageSize,
      saved || pageSize,
      (loaded) => sectionPages.set(key, loaded),
      renderItem,
      gridClass,
    );
  }

  function frameStatusMessage(video, frameCount) {
    const status = clean(video.frame_status, "missing");
    if (status === "running") return ["Обработка запущена вручную", ""];
    if (status === "pending") return ["Ожидает ручной обработки", ""];
    if (status === "failed") {
      return [video.frame_error || "Кадры не извлечены; требуется ручной повтор", "error"];
    }
    if (status === "stale") {
      return [
        video.frame_error || "Кадры неполные или изменились; требуется повторное извлечение",
        "error",
      ];
    }
    if (status === "missing") return ["Кадры не извлечены", ""];
    if (status === "saved" && !frameCount) return ["Кадры недоступны; требуется повторное извлечение", "error"];
    return ["", ""];
  }

  function videoCard(video, gallery, index) {
    const card = element("article", "video-card");
    const preview = element("div", "video-preview");
    preview.append(mediaNode(video, "video", "Видеокружок", gallery, index));
    card.append(preview);
    const seconds = numberValue(video.frame_duration);
    const duration = seconds && seconds > 0
      ? `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.round(seconds % 60)).padStart(2, "0")}`
      : "";
    const label = element("div", "video-label");
    label.append(
      element("span", "", `Кружок · комментарий #${video.message_id}`),
      element("span", "", duration || dateText(video.message_date)),
    );
    card.append(label);
    const frames = (video.frames || []).filter((frame) => frame && frame.available && localMediaURL(frame.url));
    const [statusText, statusClass] = frameStatusMessage(video, frames.length);
    if (frames.length) {
      const strip = element("div", "frame-strip");
      frames.forEach((frame, frameIndex) => {
        const button = element("button", "frame");
        button.type = "button";
        const image = element("img", "frame-thumb");
        image.loading = "lazy";
        image.decoding = "async";
        image.alt = `Кадр ${frame.position}`;
        image.src = localMediaURL(frame.url);
        button.append(image);
        const percent = [20, 50, 80][Number(frame.position) - 1] || "";
        button.append(element("span", "frame-percent", `${percent}%`));
        button.title = `Кадр ${frame.position} · ${Number(frame.timestamp_seconds || 0).toFixed(2)} с`;
        button.addEventListener("click", () => openViewer(frames, frameIndex, `Кадр ${frame.position}`));
        strip.append(button);
      });
      card.append(strip);
      card.append(element("span", "video-status", "3 кадра одного видео: 20% · 50% · 80%"));
    } else if (statusText) {
      card.append(element("span", `video-status${statusClass ? " error" : ""}`, statusText));
    }
    const actions = element("div", "video-actions");
    const view = element("button", "secondary", "Просмотр");
    view.type = "button";
    view.addEventListener("click", () => openViewer(gallery, index, "Видеокружок"));
    actions.append(view);
    if (interactiveEnabled && validProfileId(video.author_id)) {
      const analyze = element("button", "ai-action", "AI по кадрам");
      analyze.type = "button";
      analyze.disabled = frames.length !== 3;
      analyze.title = frames.length === 3
        ? "Отправить три готовых кадра в существующий AI-предпросмотр"
        : "AI по кадрам доступен только для готового набора из трёх кадров";
      analyze.addEventListener("click", () => beginAnalysis(
        {author_ids: [Number(video.author_id)], video_ids: {[String(video.author_id)]: video.video_id}},
        "frames",
      ));
      actions.append(analyze);
    }
    card.append(actions);
    const footer = element("div", "meta");
    footer.append(
      element("span", "", dateText(video.message_date)),
      originalLink(video.comment_url, "Исходный комментарий"),
    );
    card.append(footer);
    return card;
  }

  function prepareDetail() {
    disconnectObservers();
    toolbar.hidden = true;
    profileFilters.hidden = true;
    byId("list-meta").hidden = true;
    byId("page-head").replaceChildren();
  }

  function detailHero(kicker, title, subtitle, actions, metrics, intro) {
    const hero = element("div", "detail-hero");
    hero.append(element("div", "kicker", kicker));
    const top = element("div", "detail-top");
    const identity = element("div", "detail-identity");
    const h1 = element("h1", "detail-name", title);
    h1.id = "title";
    identity.append(h1);
    if (subtitle) identity.append(element("div", "detail-handle", subtitle));
    top.append(identity);
    if (actions) top.append(actions);
    hero.append(top);
    if (intro) hero.append(element("p", "post-intro", intro));
    if (metrics) hero.append(metrics);
    return hero;
  }

  function metricChip(label, value) {
    const chip = element("span", "metric");
    if (value !== undefined) chip.append(element("b", "", String(value)));
    chip.append(document.createTextNode(label));
    return chip;
  }

  function showPost(sourceId) {
    const post = postMap.get(Number(sourceId));
    if (!post) {
      navigate(listRoute("posts"), true);
      return;
    }
    currentRoute = {type: "post", id: Number(sourceId)};
    prepareDetail();
    setNavActive("posts");
    const root = element("div", "detail");
    root.append(backButton(listRoute("posts"), "к источникам"));

    const actions = element("div", "detail-actions");
    if (interactiveEnabled) {
      const analyzeAll = element("button", "primary-action", "Анализировать профили");
      analyzeAll.type = "button";
      analyzeAll.setAttribute("aria-label", "Анализировать все локальные профили публикации");
      analyzeAll.addEventListener("click", () => beginAnalysis({source_id: Number(sourceId)}));
      actions.append(analyzeAll);
    }
    const metrics = element("div", "detail-metrics");
    metrics.append(
      metricChip(` ${plural(post.author_count || 0, ["профиль", "профиля", "профилей"])}`, post.author_count || 0),
      metricChip(` ${plural(post.video_note_count || 0, ["кружок", "кружка", "кружков"])}`, post.video_note_count || 0),
    );
    const checked = element("span", "metric");
    checked.append(document.createTextNode("Проверено: "), element("b", "", dateText(post.last_checked_at)));
    metrics.append(checked);
    if (post.published_at) {
      const published = element("span", "metric");
      published.append(document.createTextNode("Опубликовано: "), element("b", "", dateText(post.published_at)));
      metrics.append(published);
    }
    const subtitleParts = [post.channel_username ? `@${post.channel_username}` : "", `публикация #${post.post_id}`].filter(Boolean);
    root.append(detailHero(
      "TELEGRAM / СОХРАНЁННАЯ ПУБЛИКАЦИЯ",
      clean(post.channel_title, `Пост #${post.post_id}`),
      subtitleParts.join(" · "),
      actions,
      metrics,
      post.text || "",
    ));

    const columns = element("div", "detail-columns");
    const main = element("div", "detail-main");
    const aside = element("div", "detail-aside");

    const media = (post.media || []).filter(available);
    if (media.length) {
      const body = incrementalSection(
        media, MEDIA_PAGE_SIZE, `post-media:${sourceId}`,
        (item, index) => mediaNode(item, item.kind, "Медиа публикации", media, index),
        "media-grid",
      );
      main.append(sectionBlock("Медиа публикации", `${media.length} ${plural(media.length, ["файл", "файла", "файлов"])}`, body, ""));
    }

    const postVideos = (post.video_ids || []).map((id) => videoMap.get(String(id))).filter(Boolean);
    const videosBody = postVideos.length
      ? incrementalSection(
        postVideos, VIDEO_PAGE_SIZE, `post-videos:${sourceId}`,
        (video, index) => videoCard(video, postVideos, index),
        "note-grid",
      )
      : element("p", "muted", "Видеокружков пока нет");
    main.append(sectionBlock(
      "Видеокружки",
      `${postVideos.length} ${plural(postVideos.length, ["кружок", "кружка", "кружков"])}`,
      videosBody,
      "Готовые кадры из локально сохранённых MP4; извлечение запускается вручную в TUI.",
    ));

    const related = (post.profile_ids || []).map((id) => profileMap.get(Number(id))).filter(Boolean);
    const relatedBody = related.length
      ? incrementalSection(
        related, PAGE_SIZE, `post-profiles:${sourceId}`,
        (profile) => profileCard(profile, {type: "post", id: Number(sourceId)}),
        "source-profile-grid",
      )
      : element("p", "muted", "Профили пока не найдены");
    main.append(sectionBlock(
      "Связанные профили",
      `${related.length} ${plural(related.length, ["профиль", "профиля", "профилей"])} в архиве`,
      relatedBody,
      "Профили, сохранённые из комментариев этой публикации",
    ));
    columns.append(main);

    const sourceInfo = element("div", "properties aside-box");
    sourceInfo.append(element("div", "prop-heading", "Информация об источнике"));
    const list = element("dl", "");
    const rows = [
      ["Канал", post.channel_username ? `@${post.channel_username}` : "—"],
      ["ID публикации", String(post.post_id)],
      ["Профили", String(post.author_count || 0)],
      ["Видеокружки", String(post.video_note_count || 0)],
      ["Последняя проверка", dateText(post.last_checked_at)],
      ["Статус", clean(post.last_error || post.last_result, post.enabled ? "ожидает проверки" : "источник отключён")],
    ];
    rows.forEach(([label, value]) => {
      const row = element("div", "prop-row");
      row.append(element("dt", "", label), element("dd", "", value));
      list.append(row);
    });
    sourceInfo.append(list);
    if (post.url) sourceInfo.append(originalLink(post.url, `Открыть пост #${post.post_id}`));
    aside.append(sourceInfo);

    if (interactiveEnabled) {
      const analysis = element("div", "properties aside-box");
      analysis.append(element("div", "prop-heading", "Анализ публикации"));
      analysis.append(element(
        "p", "muted",
        "Выбор сохранённых профилей и локальных изображений перед AI-запросом. "
          + "Предпросмотр не отправляет данные провайдеру.",
      ));
      const open = element("button", "primary-action", "Открыть предпросмотр");
      open.type = "button";
      open.style.width = "100%";
      open.style.marginTop = ".7rem";
      open.addEventListener("click", () => beginAnalysis({source_id: Number(sourceId)}));
      analysis.append(open);
      const selected = element("button", "secondary", "Анализировать выбранные");
      selected.type = "button";
      selected.style.width = "100%";
      selected.style.marginTop = ".4rem";
      selected.addEventListener("click", () => {
        const relatedIds = new Set((post.profile_ids || []).map(Number));
        const ids = [...selectedProfiles].filter((id) => relatedIds.has(id));
        if (!ids.length) showNotice("Сначала выбери профили этой публикации.", true);
        else beginAnalysis({author_ids: ids});
      });
      analysis.append(selected);
      aside.append(analysis);
    }
    columns.append(aside);

    root.append(columns);
    content.replaceChildren(root);
  }

  function linkedPosts(profile) {
    const postsForProfile = (profile.post_ids || [])
      .map((id) => postMap.get(Number(id)))
      .filter(Boolean);
    if (!postsForProfile.length) return element("p", "muted", "Связанные публикации не сохранены");
    const root = element("div", "inline-list");
    postsForProfile.forEach((post) => {
      const button = element("button", "inline-item");
      button.type = "button";
      const copy = element("span", "");
      copy.append(
        element("strong", "", clean(post.channel_title, post.channel_username)),
        element("small", "", post.channel_username ? `@${post.channel_username} · #${post.post_id}` : `#${post.post_id}`),
      );
      button.append(copy, element("span", "", "↗"));
      button.addEventListener("click", () => navigate({type: "post", id: Number(post.source_id)}));
      root.append(button);
    });
    return root;
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
      item.image_mode === "frames" || item.image_mode === "combined"
        ? `кадры видео ${item.video_message_id || "—"}`
        : "фото профиля",
      item.image_mode === "combined" ? "3 кадра + фото" : "",
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
    prepareDetail();
    setNavActive("profiles");
    const root = element("div", "detail");
    root.append(backButton(fallback, fallback.type === "post" ? "к публикации" : "ко всем профилям"));

    const actions = element("div", "detail-actions");
    if (interactiveEnabled && validProfileId(authorId)) {
      const analyze = element("button", "primary-action", "AI по фото");
      analyze.type = "button";
      analyze.addEventListener("click", () => beginAnalysis({author_ids: [Number(authorId)]}));
      actions.append(analyze);
      if ((profile.video_ids || []).length) {
        const frameAnalyze = element("button", "secondary", "AI по кадрам");
        frameAnalyze.type = "button";
        frameAnalyze.addEventListener(
          "click", () => beginAnalysis({author_ids: [Number(authorId)]}, "frames")
        );
        actions.append(frameAnalyze);
        if (profilePhotos(profile).length) {
          const combined = element("button", "secondary", "3 кадра + фото");
          combined.type = "button";
          combined.addEventListener(
            "click", () => beginAnalysis({author_ids: [Number(authorId)]}, "combined")
          );
          actions.append(combined);
        }
      }
    }
    const latest = latestPSL(profile);
    const metrics = element("div", "detail-metrics");
    metrics.append(
      metricChip(` ${plural(profilePhotos(profile).length, ["фото", "фото", "фото"])}`, profilePhotos(profile).length),
      metricChip(` ${plural((profile.video_ids || []).length, ["кружок", "кружка", "кружков"])}`, (profile.video_ids || []).length),
      metricChip(` ${plural((profile.post_ids || []).length, ["источник", "источника", "источников"])}`, (profile.post_ids || []).length),
    );
    if (latest) {
      const badge = element("span", "psl-badge", `PSL ${scoreText(latest.raw_psl_min)}–${scoreText(latest.raw_psl_max)}`);
      metrics.append(badge);
    }
    const hero = element("div", "detail-hero");
    hero.append(element("div", "kicker", "TELEGRAM / СОХРАНЁННЫЙ ПРОФИЛЬ"));
    const top = element("div", "detail-top");
    const identity = element("div", "detail-identity");
    const h1 = element("h1", "detail-name", clean(profile.display_name, `ID ${profile.author_id}`));
    h1.id = "title";
    identity.append(h1);
    identity.append(element(
      "div", "detail-handle",
      `${profile.username ? `@${profile.username}` : "username не задан"} · ID ${profile.author_id}`,
    ));
    top.append(avatar(profile, true), identity, actions);
    hero.append(top, metrics);
    root.append(hero);

    const columns = element("div", "detail-columns");
    const main = element("div", "detail-main");
    const aside = element("div", "detail-aside");

    const photos = profilePhotos(profile);
    const photosBody = photos.length
      ? incrementalSection(
        photos, MEDIA_PAGE_SIZE, `profile-photos:${authorId}`,
        (photo, index) => mediaNode(photo, "image", "Фото профиля", photos, index),
        "photo-grid",
      )
      : element("p", "muted", profile.photo_error || "Доступных локальных фотографий нет");
    main.append(sectionBlock(
      "Фотографии",
      `${photos.length} ${plural(photos.length, ["фото", "фото", "фото"])}`,
      photosBody,
      "Локально сохранённые фотографии профиля",
    ));

    const profileVideos = (profile.video_ids || []).map((id) => videoMap.get(String(id))).filter(Boolean);
    const videosBody = profileVideos.length
      ? incrementalSection(
        profileVideos, VIDEO_PAGE_SIZE, `profile-videos:${authorId}`,
        (video, index) => videoCard(video, profileVideos, index),
        "note-grid",
      )
      : element("p", "muted", "Сохранённых видеокружков нет");
    main.append(sectionBlock(
      "Видеокружки",
      `${profileVideos.length} ${plural(profileVideos.length, ["кружок", "кружка", "кружков"])}`,
      videosBody,
      "Готовые кадры: 20% / 50% / 80%. Извлечение запускается вручную в TUI.",
    ));

    const usernames = profile.username_history || [];
    const historyBody = usernames.length
      ? (() => {
        const table = element("table", "history-table");
        const tbody = element("tbody", "");
        usernames.forEach((item) => {
          const row = element("tr", "");
          row.append(
            element("td", "", `@${item.username}`),
            element("td", "", item.username === profile.username ? "Текущий" : `Ранее · ${dateText(item.last_seen)}`),
          );
          tbody.append(row);
        });
        table.append(tbody);
        return table;
      })()
      : element("p", "muted", "Username не наблюдался");
    main.append(sectionBlock("История username", null, historyBody, ""));

    const analyses = Array.isArray(profile.psl_history) ? profile.psl_history : [];
    const pslBody = element("div", "");
    pslBody.append(element(
      "p", "subjective",
      "PSL — субъективная, ненаучная визуальная оценка. Она не является фактом или объективным измерением.",
    ));
    if (latest) {
      const latestBlock = element("div", "psl-latest");
      latestBlock.append(element("div", "card-title", "Последняя допустимая оценка"));
      latestBlock.append(element("div", "psl-value", `${scoreText(latest.raw_psl_min)}–${scoreText(latest.raw_psl_max)}`));
      latestBlock.append(pslMetrics(latest));
      latestBlock.append(meta([
        dateText(latest.analyzed_at),
        latest.actual_model ? `модель: ${latest.actual_model}` : "",
        latest.prompt_version_id ? `prompt: ${latest.prompt_version_id}` : "",
      ]));
      pslBody.append(latestBlock);
    } else {
      pslBody.append(element("p", "muted", "Допустимой завершённой оценки нет"));
    }
    pslBody.append(element("h3", "section-title", "История анализов"));
    if (!analyses.length) pslBody.append(element("p", "muted", "Анализы ещё не запускались"));
    else {
      pslBody.append(incrementalSection(
        analyses, HISTORY_PAGE_SIZE, `profile-psl:${authorId}`, pslHistoryEntry, "psl-history",
      ));
    }
    main.append(sectionBlock("История PSL-анализа", null, pslBody, ""));
    columns.append(main);

    const properties = element("div", "properties aside-box");
    properties.append(element("div", "prop-heading", "Сведения о профиле"));
    const info = element("dl", "");
    [
      ["Telegram ID", String(profile.author_id)],
      ["Username", profile.username ? `@${profile.username}` : "—"],
      ["Последняя активность", dateText(profile.last_activity || profile.last_seen)],
      ["Впервые", dateText(profile.first_seen)],
      ["Фотографии", String(photos.length)],
      ["Видеокружки", String(profileVideos.length)],
      ["Источники", String((profile.post_ids || []).length)],
    ].forEach(([label, value]) => {
      const row = element("div", "prop-row");
      row.append(element("dt", "", label), element("dd", "", value));
      info.append(row);
    });
    properties.append(info);
    aside.append(properties);

    const related = element("div", "aside-box");
    related.append(element("div", "prop-heading", "Связанные публикации"));
    related.append(linkedPosts(profile));
    aside.append(related);

    if (interactiveEnabled) {
      const aiBox = element("div", "properties aside-box");
      aiBox.append(element("div", "prop-heading", "AI по сохранённым данным"));
      aiBox.append(element(
        "p", "muted",
        "Фото профиля, три кадра видеокружка или 3 кадра + фото — только после явного запуска.",
      ));
      const combined = element("button", "secondary", "Открыть AI-предпросмотр");
      combined.type = "button";
      combined.style.width = "100%";
      combined.style.marginTop = ".7rem";
      combined.disabled = !photos.length && !profileVideos.length;
      combined.addEventListener("click", () => beginAnalysis({author_ids: [Number(authorId)]}));
      aiBox.append(combined);
      aside.append(aiBox);
    }
    columns.append(aside);
    root.append(columns);
    content.replaceChildren(root);
  }

  function renderRoute(route = parseRoute()) {
    disconnectObservers();
    if (route.type === "post") showPost(route.id);
    else if (route.type === "profile") showProfile(route.id, route.origin);
    else renderList(route.view);
  }

  function resetCurrentList() {
    const view = currentRoute?.type === "list" ? currentRoute.view : "posts";
    lists[view].query = "";
    lists[view].visible = PAGE_SIZE;
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
    const select = byId("filter-source");
    const previous = select.value;
    const first = element("option", "", "Все источники");
    first.value = "";
    const options = [first, ...posts.map((post) => {
      const option = element("option", "", `${clean(post.channel_title, post.channel_username)} #${post.post_id}`);
      option.value = String(post.source_id);
      return option;
    })];
    select.replaceChildren(...options);
    if (previous && options.some((option) => option.value === previous)) select.value = previous;
    else {
      select.value = "";
      if (lists.profiles.filters.source === previous) lists.profiles.filters.source = "";
    }
  }

  queryInput.addEventListener("input", () => {
    if (currentRoute?.type !== "list") return;
    const view = currentRoute.view;
    lists[view].query = queryInput.value;
    lists[view].visible = PAGE_SIZE;
    delayedListRender(view);
  });
  sortInput.addEventListener("change", () => {
    if (currentRoute?.type !== "list") return;
    const list = lists[currentRoute.view];
    list.sort = sortInput.value;
    list.visible = PAGE_SIZE;
    renderList(currentRoute.view);
  });
  direction.addEventListener("click", () => {
    if (currentRoute?.type !== "list") return;
    const list = lists[currentRoute.view];
    list.descending = !list.descending;
    list.visible = PAGE_SIZE;
    renderList(currentRoute.view);
  });
  byId("reset").addEventListener("click", resetCurrentList);
  byId("show-posts").addEventListener("click", () => navigate(listRoute("posts")));
  byId("show-profiles").addEventListener("click", () => navigate(listRoute("profiles")));
  byId("home").addEventListener("click", () => navigate(listRoute("posts")));

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
      lists.profiles.visible = PAGE_SIZE;
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
    focusTitle();
  });
  window.addEventListener("hashchange", () => {
    const parsed = parseRoute();
    if (!currentRoute || routeHash(parsed) !== routeHash(currentRoute)) {
      renderRoute(parsed);
      focusTitle();
    }
  });
  window.addEventListener("beforeunload", rememberScroll);
  document.addEventListener("ended", flushPendingCatalog, true);
  document.addEventListener("pause", flushPendingCatalog, true);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      window.clearTimeout(catalogUpdateTimer);
      catalogUpdateTimer = window.setTimeout(checkCatalogUpdate, 0);
    }
  });

  setSourceOptions();
  byId("summary").textContent = `${posts.length} публикаций · ${profiles.length} профилей · ${videos.length} кружков`;
  byId("generated").textContent = `Снимок: ${dateText(catalog.generated_at)}`;
  byId("notice").hidden = true;
  restoreInteractiveState();
  initInteractive();
  const initialRoute = parseRoute();
  history.replaceState({hoard: true, route: initialRoute, depth: 0, scrollY: 0}, "", routeHash(initialRoute));
  renderRoute(initialRoute);
  if (interactiveEnabled) {
    catalogUpdateTimer = window.setTimeout(checkCatalogUpdate, 2000);
  }
})();
