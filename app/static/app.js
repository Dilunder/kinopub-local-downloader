"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
let noticeTimer;
function notice(message, error = false, authHelp = false) {
  const node = $("#notice");
  node.textContent = message;
  if (authHelp) {
    const link = document.createElement("a");
    link.href = "/settings#auth"; link.className = "secondary auth-help-link";
    link.textContent = "Настроить авторизацию →";
    node.append(document.createElement("br"), link);
  }
  node.classList.toggle("error", error);
  node.hidden = false;
  clearTimeout(noticeTimer);
  if (!error) noticeTimer = setTimeout(() => { node.hidden = true; }, 7000);
}
async function api(path, options = {}) {
  const headers = {"X-CSRF-Token": csrf, ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  let data;
  try { data = await response.json(); } catch { throw new Error(`Ошибка сервера (${response.status})`); }
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    error.code = data.code;
    throw error;
  }
  return data;
}
async function busy(button, action) {
  button.disabled = true;
  const text = button.textContent;
  button.textContent = "Подождите…";
  try { return await action(); } catch (error) { notice(error.message, true, error.code?.startsWith("cookies_")); }
  finally { button.disabled = false; button.textContent = text; }
}
function bytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function eta(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.round(value));
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин` : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

// HTMX handles health polling; this fallback keeps diagnostics available without CDNs.
async function refreshHealth() {
  const node = $("#health");
  if (!node || window.htmx) return;
  try {
    const state = await api("/api/health");
    node.replaceChildren();
    [["yt-dlp", state.yt_dlp], ["Плагин KinoPub", state.plugin && state.plugin_loadable], ["ffmpeg", state.ffmpeg]].forEach(([label, ready]) => {
      const row = document.createElement("div"); row.className = "health-row";
      const name = document.createElement("span"); name.textContent = label;
      const status = document.createElement("span"); status.textContent = ready ? "● Готово" : "○ Не найден"; status.className = ready ? "good" : "bad";
      row.append(name, status); node.append(row);
    });
    const note = document.createElement("p"); note.className = "hint";
    note.textContent = state.ok ? "Зависимости установлены. Для доступа нужны действующие cookies." : "Установите недостающие зависимости и перезапустите сервис.";
    node.append(note);
    if (state.dev_mode) { const warning = document.createElement("p"); warning.className = "bad"; warning.textContent = "Включён dev-режим"; node.append(warning); }
  } catch (error) { node.textContent = error.message; }
}
refreshHealth();
setInterval(refreshHealth, 30000);

if (document.body.dataset.page === "queue") {
  let metadata = null, inspectedUrl = "", filter = "", page = 0, polling = false;
  const openLogs = new Set();
  const labels = {queued: "В очереди", running: "Загружается", done: "Готово", error: "Ошибка", canceled: "Отменено"};
  if (new URLSearchParams(location.search).get("view") === "history") {
    $("#add-section").hidden = true;
    $("#page-title").textContent = "История загрузок.";
    $(".subheading").textContent = "Завершённые задачи, ошибки и сохранённые логи.";
    $$('nav a').forEach(a => { a.removeAttribute("aria-current"); if (a.getAttribute("href") === "/?view=history") a.setAttribute("aria-current", "page"); });
  }
  api("/api/settings").then(settings => {
    for (const key of ["quality", "audio", "subtitles"]) $(`#job-${key}`).value = settings[key];
    $("#job-template").value = settings.filename_template;
  }).catch(error => notice(error.message, true));

  $("#video-url").addEventListener("input", () => { metadata = null; $("#metadata-result").hidden = true; });
  $("#metadata-form").addEventListener("submit", event => {
    event.preventDefault();
    busy($("#metadata-button"), async () => {
      inspectedUrl = $("#video-url").value.trim();
      metadata = null;
      $("#metadata-result").hidden = true;
      $("#metadata-help").textContent = "Получаем информацию; большой сериал может обрабатываться несколько минут…";
      try {
        const result = await api("/api/metadata", {method: "POST", body: JSON.stringify({url: inspectedUrl})});
        if ($("#video-url").value.trim() !== inspectedUrl) throw new Error("Ссылка изменилась. Запросите информацию ещё раз");
        metadata = result;
        $("#media-title").textContent = result.title;
        $("#media-kind").textContent = result.kind === "series" ? "СЕРИАЛ / ПЛЕЙЛИСТ" : "ФИЛЬМ / ЭПИЗОД";
        $("#media-detail").textContent = result.episodes.length ? `${result.episodes.length} эпизодов · выбор ниже` : (result.duration ? `Продолжительность: ${eta(result.duration)}` : "Готово к добавлению в очередь");
        $("#available-info").textContent = `Качества: ${result.qualities.map(q => q + "p").join(", ") || "не указаны"}. Субтитры: ${result.subtitles.join(", ") || "не указаны"}. Состав дорожек может различаться у эпизодов.`;
        $("#audio-options").replaceChildren();
        for (const item of result.audio) { const option = document.createElement("option"); option.value = item.value; option.label = item.label; $("#audio-options").append(option); }
        $("#subtitle-options").replaceChildren();
        for (const lang of [...result.subtitles, "all"]) { const option = document.createElement("option"); option.value = lang; $("#subtitle-options").append(option); }
        $("#episode-section").hidden = !result.episodes.length;
        $("#episode-mode").value = "all";
        $("#episode-picker").hidden = true;
        $("#episodes").replaceChildren();
        for (const episode of result.episodes) {
          const label = document.createElement("label"); label.className = "episode-label";
          const box = document.createElement("input"); box.type = "checkbox"; box.value = episode.index; box.checked = true;
          const text = document.createElement("span"); text.textContent = `${episode.season ? `S${episode.season} ` : ""}${episode.episode ? `E${episode.episode} · ` : `${episode.index}. `}${episode.title}`;
          label.append(box, text); $("#episodes").append(label);
        }
        $("#metadata-result").hidden = false;
      } finally { $("#metadata-help").textContent = "Ссылка на сериал добавляет все сезоны. Эпизоды можно выбрать перед загрузкой."; }
    });
  });
  $("#episode-mode").addEventListener("change", event => { $("#episode-picker").hidden = event.target.value !== "selected"; });
  $("#select-all").onclick = () => $$('#episodes input').forEach(box => { box.checked = true; });
  $("#select-none").onclick = () => $$('#episodes input').forEach(box => { box.checked = false; });
  $("#job-form").addEventListener("submit", event => {
    event.preventDefault();
    busy($('button[type=submit]', event.target), async () => {
      if (!metadata) throw new Error("Сначала получите информацию");
      const body = Object.fromEntries(new FormData(event.target));
      body.url = inspectedUrl; body.title = metadata.title;
      body.episodes = $("#episode-mode").value === "selected" ? $$('#episodes input:checked').map(box => Number(box.value)) : [];
      if ($("#episode-mode").value === "selected" && !body.episodes.length) throw new Error("Выберите хотя бы один эпизод");
      await api("/api/jobs", {method: "POST", body: JSON.stringify(body)});
      notice("Задача добавлена в очередь"); page = 0; await loadJobs();
    });
  });
  $$('[data-filter]').forEach(button => button.addEventListener("click", () => {
    filter = button.dataset.filter; page = 0;
    $$('[data-filter]').forEach(item => item.classList.toggle("active", item === button));
    loadJobs();
  }));
  $("#prev-page").onclick = () => { page = Math.max(0, page - 1); loadJobs(); };
  $("#next-page").onclick = () => { page++; loadJobs(); };

  async function loadLog(id, card) {
    const log = $(".job-log", card);
    try { const data = await api(`/api/jobs/${id}/log`); log.textContent = data.log; }
    catch (error) { log.textContent = error.message; }
  }
  async function loadJobs() {
    if (polling) return;
    polling = true;
    try {
      const jobs = await api(`/api/jobs?limit=25&offset=${page * 25}${filter ? "&status=" + filter : ""}`);
      $("#poll-status").textContent = "Обновление каждые 2 сек.";
      $("#job-count").textContent = String(jobs.length) + (jobs.length === 25 ? "+" : "");
      $("#empty-state").hidden = !!jobs.length;
      $("#prev-page").disabled = page === 0;
      $("#next-page").disabled = jobs.length < 25;
      $("#page-number").textContent = `Страница ${page + 1}`;
      const keep = new Set(jobs.map(job => String(job.id)));
      $$('#jobs .job-card').forEach(card => { if (!keep.has(card.dataset.id)) card.remove(); });
      for (const job of jobs) {
        let card = $(`#jobs [data-id="${job.id}"]`);
        if (!card) {
          card = $("#job-template-card").content.firstElementChild.cloneNode(true);
          card.dataset.id = job.id;
          $("#jobs").append(card);
        }
        card.className = "job-card status-" + job.status;
        $("h3", card).textContent = job.title;
        $(".job-meta", card).textContent = `#${job.id} · ${new Date(job.created_at).toLocaleString("ru-RU")} · ${job.files.length ? job.files.length + " файл(ов)" : "MKV"}`;
        $(".status-badge", card).textContent = labels[job.status] || job.status;
        $(".progress-track>div", card).style.width = `${Math.max(0, Math.min(100, job.progress))}%`;
        $(".phase", card).textContent = `${job.phase} · ${job.progress.toFixed(1)}%`;
        $(".transfer", card).textContent = job.status === "running" ? `${bytes(job.downloaded)} / ${bytes(job.total)} · ${bytes(job.speed)}/с · ETA ${eta(job.eta)}` : (job.status === "done" ? `${bytes(job.downloaded)} · ${job.files.length} файл(ов)` : "");
        $(".job-error", card).hidden = !job.error;
        $(".job-error", card).textContent = job.error || "";
        $('[data-action="cancel"]', card).hidden = !["queued", "running"].includes(job.status);
        $('[data-action="retry"]', card).hidden = !["error", "canceled"].includes(job.status);
        $('[data-action="delete"]', card).hidden = ["queued", "running"].includes(job.status);
        $(".job-log", card).hidden = !openLogs.has(job.id);
        $('[data-action="log"]', card).textContent = openLogs.has(job.id) ? "Скрыть лог" : "Показать лог";
        if (openLogs.has(job.id)) await loadLog(job.id, card);
      }
      // Put newly discovered cards in descending task order without recreating controls.
      jobs.forEach(job => { const card = $(`#jobs [data-id="${job.id}"]`); $("#jobs").append(card); });
    } catch (error) { $("#poll-status").textContent = "Нет связи: " + error.message; }
    finally { polling = false; }
  }
  $("#jobs").addEventListener("click", event => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const card = button.closest(".job-card"), id = Number(card.dataset.id), action = button.dataset.action;
    if (action === "log") {
      if (openLogs.has(id)) openLogs.delete(id); else openLogs.add(id);
      $(".job-log", card).hidden = !openLogs.has(id);
      button.textContent = openLogs.has(id) ? "Скрыть лог" : "Показать лог";
      if (openLogs.has(id)) loadLog(id, card);
      return;
    }
    busy(button, async () => {
      const endpoint = action === "folder" ? "open-folder" : action;
      await api(`/api/jobs/${id}${action === "delete" ? "" : "/" + endpoint}`, {method: action === "delete" ? "DELETE" : "POST"});
      if (action === "delete") notice("Запись удалена. Скачанные файлы сохранены");
      await loadJobs();
    });
  });
  loadJobs();
  setInterval(() => { if (!document.hidden) loadJobs(); }, 2000);
}

if (document.body.dataset.page === "settings") {
  const form = $("#settings-form");
  const saveButton = $('button[type="submit"]', form);
  saveButton.disabled = true;
  function authVisibility() {
    const fromBrowser = $("#auth-source").value === "browser";
    $("#browser-field").hidden = !fromBrowser;
    $("#browser-cookie-note").hidden = !fromBrowser || !["chrome", "edge", "chromium", "brave", "opera", "vivaldi", "whale"].includes($("#browser").value);
  }
  api("/api/settings").then(settings => {
    for (const [key, value] of Object.entries(settings)) { const input = form.elements.namedItem(key); if (input) input.value = value; }
    authVisibility(); saveButton.disabled = false;
  }).catch(error => notice(error.message, true));
  $("#auth-source").addEventListener("change", authVisibility);
  $("#browser").addEventListener("change", authVisibility);
  if (location.hash === "#auth") $("#cookie-header-import").open = true;
  form.addEventListener("submit", event => {
    event.preventDefault();
    busy(saveButton, async () => {
      const body = Object.fromEntries(new FormData(form));
      body.parallel_downloads = Number(body.parallel_downloads);
      await api("/api/settings", {method: "PUT", body: JSON.stringify(body)});
      notice("Настройки сохранены");
    });
  });
  $("#upload-cookies").addEventListener("click", event => busy(event.target, async () => {
    const file = $("#cookies-file").files[0];
    if (!file) throw new Error("Выберите свой cookies.txt");
    const body = new FormData(); body.append("file", file);
    const result = await api("/api/settings/cookies", {method: "POST", body});
    $("#cookies-file").value = "";
    $("#auth-source").value = "file"; authVisibility();
    notice(result.message);
  }));
  $("#import-cookie-header").addEventListener("click", event => busy(event.target, async () => {
    const input = $("#cookie-header");
    if (!input.value.trim()) throw new Error("Вставьте Cookie из Request Headers страницы KinoPub");
    const result = await api("/api/settings/cookies/header", {method:"POST", body:JSON.stringify({domain:$("#cookie-domain").value,cookie_header:input.value,user_agent:$("#user-agent").value})});
    input.value = "";
    $("#auth-source").value = "file"; authVisibility();
    notice(result.message);
  }));
}
