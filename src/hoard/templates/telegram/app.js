(() => {
  "use strict";

  const catalog = window.HOARD_CATALOG || {posts: [], profiles: [], videos: []};
  const PAGE_SIZE = 80;
  const byId = (id) => document.getElementById(id);
  const content = byId("content");
  const query = byId("query");
  const sort = byId("sort");
  const direction = byId("direction");
  const state = {view: "posts", detail: null, page: 0, descending: true};
  const posts = Array.isArray(catalog.posts) ? catalog.posts : [];
  const profiles = Array.isArray(catalog.profiles) ? catalog.profiles : [];
  const videos = Array.isArray(catalog.videos) ? catalog.videos : [];
  const postMap = new Map(posts.map((item) => [Number(item.source_id), item]));
  const profileMap = new Map(profiles.map((item) => [Number(item.author_id), item]));
  const videoMap = new Map(videos.map((item) => [String(item.video_id), item]));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clean(value, fallback = "—") {
    const result = String(value || "").trim();
    return result || fallback;
  }

  function dateText(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("ru-RU");
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
    } catch (_) { /* invalid external link stays plain text */ }
    return element("span", "muted", label);
  }

  function localMediaURL(url) {
    const value = String(url || "");
    if (!value || value.startsWith("//")) return "";
    if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !value.startsWith("file:")) return "";
    return value;
  }

  function initials(profile) {
    const source = clean(profile.display_name || profile.username || profile.author_id, "?");
    return source.slice(0, 2).toUpperCase();
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

  function meta(items) {
    const line = element("div", "meta");
    items.filter(Boolean).forEach((text) => line.append(element("span", "", text)));
    return line;
  }

  function profileSearch(profile) {
    const history = (profile.username_history || []).map((item) => item.username).join(" ");
    const related = (profile.post_ids || []).map((id) => {
      const post = postMap.get(Number(id)) || {};
      return `${post.channel_title || ""} ${post.channel_username || ""} ${post.url || ""} ${post.post_id || ""}`;
    }).join(" ");
    return `${profile.author_id} ${profile.username || ""} ${profile.display_name || ""} ${history} ${related}`.toLocaleLowerCase();
  }

  function postSearch(post) {
    const authors = (post.profile_ids || []).map((id) => {
      const profile = profileMap.get(Number(id));
      return profile ? profileSearch(profile) : "";
    }).join(" ");
    return `${post.source_id} ${post.post_id} ${post.channel_title || ""} ${post.channel_username || ""} ${post.url || ""} ${post.text || ""} ${authors}`.toLocaleLowerCase();
  }

  profiles.forEach((item) => { item._search = profileSearch(item); });
  posts.forEach((item) => { item._search = postSearch(item); });

  const profileSorts = [
    ["name", "Имя"], ["username", "Username"], ["id", "Telegram ID"],
    ["discovered", "Дата обнаружения"], ["video_count", "Количество кружков"],
    ["last_active", "Последняя активность"],
  ];
  const postSorts = [
    ["published", "Дата публикации"], ["checked", "Последняя проверка"],
    ["channel", "Канал"], ["post_id", "ID поста"], ["video_count", "Количество кружков"],
  ];

  function setSortOptions() {
    const choices = state.view === "posts" && !state.detail ? postSorts : profileSorts;
    const old = sort.value;
    sort.replaceChildren(...choices.map(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      return option;
    }));
    sort.value = choices.some(([value]) => value === old) ? old : choices[0][0];
  }

  function compareValue(item, key, profile) {
    if (profile) {
      if (key === "id") return Number(item.author_id);
      if (key === "username") return String(item.username || "").toLocaleLowerCase();
      if (key === "discovered") return item.first_seen || "";
      if (key === "video_count") return (item.video_ids || []).length;
      if (key === "last_active") return item.last_activity || item.last_seen || "";
      return String(item.display_name || "").toLocaleLowerCase();
    }
    if (key === "post_id") return Number(item.post_id);
    if (key === "checked") return item.last_checked_at || "";
    if (key === "channel") return String(item.channel_title || item.channel_username || "").toLocaleLowerCase();
    if (key === "video_count") return Number(item.video_note_count || 0);
    return item.published_at || "";
  }

  function filteredSorted(items, profile) {
    const needle = query.value.trim().replace(/^@/, "").toLocaleLowerCase();
    const selected = needle ? items.filter((item) => item._search.includes(needle)) : [...items];
    selected.sort((left, right) => {
      const a = compareValue(left, sort.value, profile);
      const b = compareValue(right, sort.value, profile);
      const result = typeof a === "number" ? a - b : String(a).localeCompare(String(b), "ru");
      return state.descending ? -result : result;
    });
    return selected;
  }

  function page(items) {
    const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    state.page = Math.min(state.page, pages - 1);
    const start = state.page * PAGE_SIZE;
    byId("pager").hidden = items.length <= PAGE_SIZE;
    byId("pager").style.display = items.length <= PAGE_SIZE ? "none" : "flex";
    byId("page-label").textContent = `${state.page + 1} / ${pages}`;
    byId("previous").disabled = state.page === 0;
    byId("next").disabled = state.page >= pages - 1;
    return items.slice(start, start + PAGE_SIZE);
  }

  function empty(label) {
    content.replaceChildren(element("p", "muted", label));
  }

  function profileRow(profile) {
    const row = element("article", "row");
    const button = element("button", "row-action");
    button.type = "button";
    const line = element("div", "profile-line");
    line.append(avatar(profile));
    const body = element("div", "row-main");
    body.append(element("div", "row-title", clean(profile.display_name, `ID ${profile.author_id}`)));
    body.append(meta([
      profile.username ? `@${profile.username}` : "без username",
      `ID ${profile.author_id}`,
      `последнее обнаружение: ${dateText(profile.last_seen)}`,
    ]));
    line.append(body);
    button.append(line);
    button.addEventListener("click", () => showProfile(profile.author_id));
    row.append(button);
    row.append(element("div", "count", `${(profile.video_ids || []).length} круж.`));
    return row;
  }

  function postRow(post) {
    const row = element("article", "row");
    const button = element("button", "row-action");
    button.type = "button";
    const body = element("div", "row-main");
    body.append(element("div", "row-title", clean(post.channel_title, `@${post.channel_username}`)));
    if (post.text) body.append(element("div", "excerpt", post.text.slice(0, 280)));
    body.append(meta([
      `пост ${post.post_id}`,
      `опубликован: ${dateText(post.published_at)}`,
      `проверен: ${dateText(post.last_checked_at)}`,
      clean(post.last_error || post.last_result, post.enabled ? "ожидает проверки" : "отключён"),
    ]));
    const line = element("div", (post.media || []).length ? "post-line" : "");
    line.append(body);
    const preview = (post.media || []).find((item) => item.available);
    if (preview) {
      const media = mediaNode(preview, preview.kind, "Медиа поста");
      media.classList.add("post-thumb");
      line.append(media);
    }
    button.append(line);
    button.addEventListener("click", () => showPost(post.source_id));
    row.append(button);
    const count = element("div", "count");
    count.append(element("div", "state", `${post.video_note_count || 0} круж.`));
    count.append(element("div", "muted", `${post.author_count || 0} авторов`));
    row.append(count);
    return row;
  }

  function mediaNode(item, kind, label) {
    const url = item && item.available ? localMediaURL(item.url) : "";
    if (!url) return element("div", "missing", label || "Локальный файл недоступен");
    if (kind === "video" || /video|round/i.test(kind || "")) {
      const video = element("video", "media");
      video.controls = true;
      video.preload = "none";
      video.src = url;
      return video;
    }
    const image = element("img", "media");
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = label || "Локальное медиа";
    image.src = url;
    return image;
  }

  function renderList() {
    state.detail = null;
    byId("show-posts").classList.toggle("active", state.view === "posts");
    byId("show-profiles").classList.toggle("active", state.view === "profiles");
    byId("title").textContent = state.view === "posts" ? "Telegram archive" : "Все профили";
    const isProfiles = state.view === "profiles";
    const items = filteredSorted(isProfiles ? profiles : posts, isProfiles);
    if (!items.length) return empty(query.value ? "Ничего не найдено" : "Каталог пока пуст");
    const list = element("div", "list");
    page(items).forEach((item) => list.append(isProfiles ? profileRow(item) : postRow(item)));
    content.replaceChildren(list);
  }

  function backButton(action) {
    const button = element("button", "back", "← Назад");
    button.type = "button";
    button.addEventListener("click", action);
    return button;
  }

  function showPost(sourceId) {
    const post = postMap.get(Number(sourceId));
    if (!post) return;
    state.detail = {type: "post", id: Number(sourceId)};
    state.page = 0;
    setSortOptions();
    byId("title").textContent = clean(post.channel_title, `@${post.channel_username}`);
    const root = element("div", "detail");
    root.append(backButton(() => { state.detail = null; setSortOptions(); renderList(); }));
    const heading = element("div", "detail-head");
    const body = element("div", "row-main");
    body.append(originalLink(post.url, `Открыть исходный пост #${post.post_id}`));
    if (post.text) body.append(element("p", "excerpt", post.text));
    body.append(meta([
      `опубликован: ${dateText(post.published_at)}`,
      `проверен: ${dateText(post.last_checked_at)}`,
      `${post.video_note_count || 0} кружков`, `${post.author_count || 0} авторов`,
    ]));
    heading.append(body);
    root.append(heading);
    if ((post.media || []).length) {
      root.append(element("h2", "section-title", "Медиа поста"));
      const media = element("div", "media-grid");
      post.media.forEach((item) => media.append(mediaNode(item, item.kind, "Медиа поста")));
      root.append(media);
    }
    root.append(element("h2", "section-title", "Найденные профили"));
    let related = (post.profile_ids || []).map((id) => profileMap.get(Number(id))).filter(Boolean);
    related = filteredSorted(related, true);
    const list = element("div", "list");
    page(related).forEach((profile) => list.append(profileRow(profile)));
    root.append(list);
    if (!related.length) root.append(element("p", "muted", "Профили пока не найдены"));
    content.replaceChildren(root);
  }

  function showProfile(authorId) {
    const profile = profileMap.get(Number(authorId));
    if (!profile) return;
    const previous = state.detail;
    state.detail = {type: "profile", id: Number(authorId), previous};
    byId("pager").hidden = true;
    byId("pager").style.display = "none";
    byId("title").textContent = clean(profile.display_name, `ID ${profile.author_id}`);
    const root = element("div", "detail");
    root.append(backButton(() => {
      if (previous && previous.type === "post") showPost(previous.id);
      else { state.detail = null; setSortOptions(); renderList(); }
    }));
    const heading = element("div", "detail-head");
    heading.append(avatar(profile, true));
    const identity = element("div", "row-main");
    identity.append(element("div", "row-title", clean(profile.display_name, "Имя не сохранено")));
    identity.append(element("div", "state", profile.username ? `@${profile.username}` : "username не задан"));
    identity.append(meta([
      `Telegram ID ${profile.author_id}`,
      `впервые: ${dateText(profile.first_seen)}`,
      `последнее обнаружение: ${dateText(profile.last_seen)}`,
    ]));
    heading.append(identity);
    root.append(heading);
    const history = profile.username_history || [];
    root.append(element("h2", "section-title", "Наблюдавшиеся username"));
    if (!history.length) root.append(element("p", "muted", "Username не наблюдался"));
    else {
      const list = element("ul", "history");
      history.forEach((item) => list.append(element(
        "li", "", `@${item.username} · наблюдался ${dateText(item.first_seen)} — ${dateText(item.last_seen)}`
      )));
      root.append(list);
    }
    root.append(element("h2", "section-title", "Посты"));
    const linked = element("div", "meta");
    (profile.post_ids || []).forEach((id) => {
      const post = postMap.get(Number(id));
      if (post) linked.append(originalLink(post.url, `${clean(post.channel_title, post.channel_username)} #${post.post_id}`));
    });
    root.append(linked.childNodes.length ? linked : element("p", "muted", "Связанные посты не сохранены"));
    root.append(element("h2", "section-title", "Фотографии профиля"));
    const photos = element("div", "media-grid");
    (profile.photos || []).forEach((photo) => photos.append(mediaNode(photo, "image", "Фото профиля")));
    root.append(photos.childNodes.length ? photos : element("p", "muted", profile.photo_error || "Доступных фотографий нет"));
    root.append(element("h2", "section-title", "Видеокружки"));
    const profileVideos = (profile.video_ids || [])
      .map((id) => videoMap.get(String(id))).filter(Boolean);
    if (!profileVideos.length) root.append(element("p", "muted", "Видеокружков нет"));
    else {
      const videoSection = element("div", "");
      function renderVideoPage(videoPage) {
        const media = element("div", "media-grid");
        const start = videoPage * PAGE_SIZE;
        profileVideos.slice(start, start + PAGE_SIZE).forEach((video) => {
          const card = element("div", "media-card");
          card.append(mediaNode(video, "video", "Видеокружок недоступен"));
          card.append(meta([`комментарий ${video.message_id}`, dateText(video.message_date)]));
          card.append(originalLink(video.comment_url, "Исходный комментарий"));
          media.append(card);
        });
        const pieces = [media];
        const pages = Math.ceil(profileVideos.length / PAGE_SIZE);
        if (pages > 1) {
          const controls = element("div", "pager");
          controls.style.display = "flex";
          const previousPage = element("button", "", "Назад");
          const nextPage = element("button", "", "Дальше");
          previousPage.disabled = videoPage === 0;
          nextPage.disabled = videoPage >= pages - 1;
          previousPage.addEventListener("click", () => renderVideoPage(videoPage - 1));
          nextPage.addEventListener("click", () => renderVideoPage(videoPage + 1));
          controls.append(previousPage, element("span", "", `${videoPage + 1} / ${pages}`), nextPage);
          pieces.push(controls);
        }
        videoSection.replaceChildren(...pieces);
      }
      renderVideoPage(0);
      root.append(videoSection);
    }
    content.replaceChildren(root);
  }

  function rerender(resetPage = true) {
    if (resetPage) state.page = 0;
    if (state.detail && state.detail.type === "post") showPost(state.detail.id);
    else if (state.detail && state.detail.type === "profile") showProfile(state.detail.id);
    else renderList();
  }

  byId("summary").textContent = `${posts.length} постов · ${profiles.length} профилей · ${videos.length} кружков`;
  byId("generated").textContent = `Снимок: ${dateText(catalog.generated_at)}`;
  byId("notice").hidden = true;
  query.addEventListener("input", () => rerender());
  sort.addEventListener("change", () => rerender());
  direction.addEventListener("click", () => {
    state.descending = !state.descending;
    direction.textContent = state.descending ? "↓" : "↑";
    direction.title = state.descending ? "По убыванию" : "По возрастанию";
    rerender();
  });
  byId("show-posts").addEventListener("click", () => {
    state.view = "posts"; state.detail = null; setSortOptions(); rerender();
  });
  byId("show-profiles").addEventListener("click", () => {
    state.view = "profiles"; state.detail = null; setSortOptions(); rerender();
  });
  byId("home").addEventListener("click", () => {
    state.view = "posts"; state.detail = null; query.value = ""; setSortOptions(); rerender();
  });
  byId("previous").addEventListener("click", () => { state.page -= 1; rerender(false); });
  byId("next").addEventListener("click", () => { state.page += 1; rerender(false); });

  setSortOptions();
  renderList();
})();
