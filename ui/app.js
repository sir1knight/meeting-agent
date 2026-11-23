"use strict";

// ---------- связь с сервером и оболочкой ----------

const TOKEN = (() => {
  const m = location.hash.match(/token=([\w-]+)/);
  try {
    if (m) sessionStorage.setItem("token", m[1]);
    return m ? m[1] : sessionStorage.getItem("token") || "";
  } catch (_) {
    return m ? m[1] : "";
  }
})();

async function api(path, { method = "GET", body, headers = {} } = {}) {
  const opts = { method, headers: { "X-Token": TOKEN, ...headers } };
  if (body instanceof Blob) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const r = await fetch("/api/" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`);
  return data;
}

// Команды нативной оболочке: на Mac — Swift (запись там, чтобы разрешения macOS были у приложения),
// на Windows — Python (pywebview, winapp.py).
function native(cmd, args = {}) {
  const pw = window.pywebview && window.pywebview.api;
  if (pw && pw.command) {
    pw.command({ cmd, ...args });
    return true;
  }
  const h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.app;
  if (!h) {
    toast(window.pywebview === undefined && /Windows/.test(navigator.userAgent)
      ? "Запись — через значок Meeting Agent в трее, рядом с часами"
      : "Запись доступна только в приложении Meeting Agent");
    return false;
  }
  h.postMessage({ cmd, ...args });
  return true;
}

// Windows или Mac: подписи, команды установки, сочетания клавиш.
const IS_WIN = /Windows/.test(navigator.userAgent);
const PC = IS_WIN ? "этом компьютере" : "этом Mac";
const CMD_KEY = IS_WIN ? "Ctrl+" : "⌘";
if (IS_WIN) {
  const rb = document.getElementById("revealBtn");
  if (rb) { rb.textContent = "Показать в Проводнике"; rb.title = "Показать файл заметки в Проводнике"; }
}

// Цветные аватары спикеров и иконки проигрывания образцов голоса.
const SPEAKER_COLORS = ["#4c82f7", "#34b37e", "#9b6df3", "#e09132", "#e25d8a", "#2ca8b8", "#6d7cf2", "#d96b43"];
function speakerColor(name) {
  const s = String(name || "").trim();
  if (!s || s === "Я") return "var(--muted)";
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return SPEAKER_COLORS[Math.abs(h) % SPEAKER_COLORS.length];
}
function speakerInitials(name) {
  const s = String(name || "").replace(/[*?]+$/g, "").trim();
  if (!s) return "?";
  if (s === "Я") return "Я";
  const m = /^Собеседник\s+(\d+)$/i.exec(s);
  if (m) return "С" + m[1];
  const parts = s.split(/\s+/).filter(Boolean);
  return parts.length >= 2 ? (parts[0][0] + parts[1][0]).toUpperCase() : s.slice(0, 2).toUpperCase();
}
const PLAY_ICO = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4.5 2.75l9 5.25-9 5.25z"/></svg>';
const PAUSE_ICO = '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="2.8" width="3.2" height="10.4" rx="1"/><rect x="9.3" y="2.8" width="3.2" height="10.4" rx="1"/></svg>';

// ---------- утилиты ----------

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let toastTimer;
function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
  "сентября", "октября", "ноября", "декабря"];

function parseDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?/.exec(s || "");
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0)) : null;
}

function dayLabel(d) {
  if (!d) return "Без даты";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "Сегодня";
  if (diff === 1) return "Вчера";
  const year = day.getFullYear() === today.getFullYear() ? "" : " " + day.getFullYear();
  return `${day.getDate()} ${MONTHS[day.getMonth()]}${year}`;
}

const hhmm = (d) => d ? d.toTimeString().slice(0, 5) : "";

function fmtBytes(n) {
  if (!n) return "0";
  return n >= 1e9 ? (n / 1e9).toFixed(1).replace(".", ",") + " ГБ" : Math.round(n / 1e6) + " МБ";
}

// ---------- Markdown (то, что пишет модель: заголовки, списки, таблицы, жирный) ----------

function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function markdown(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m;
    if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) {
      const level = Math.min(m[1].length + 1, 4);
      out.push(`<h${level}>${inline(m[2])}</h${level}>`); i++; continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push("<table><thead><tr>" + head.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, "")));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((x) => `<li>${x}</li>`).join("") + `</${tag}>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*\||\s*([-*•]|\d+[.)])\s+|-{3,})/.test(lines[i])) {
      para.push(inline(lines[i])); i++;
    }
    if (para.length) out.push("<p>" + para.join("<br>") + "</p>");
    else i++;
  }
  return out.join("\n");
}

function fmtTs(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return [h, m, s].map((x) => String(x).padStart(2, "0")).join(":");
}

// Слова-паразиты: только показ и выгрузка — сама расшифровка не меняется.
const FILLER_ONLY = /^(?:угу|ага|ну|ммм*|м-м|мм|э+|э-э|эм+|хм+|ага-ага|угу-угу|ну да|да-да)[\s.,!?…]*$/i;
function cleanFillers(t) {
  let x = " " + t + " ";
  x = x.replace(/([\s,])(?:э+(?:-э+)?|м-м|мм+|эм+|хм+)(?=[\s,.!?…])/gi, "$1");
  x = x.replace(/([\s,])(?:как бы|типа|короче|в общем-то|в общем|так сказать|это самое|скажем так|вот)\s*,/gi, "$1");
  x = x.replace(/,\s*(?:как бы|типа|короче|так сказать|это самое)(?=[\s,.!?…])/gi, "");
  x = x.replace(/(^\s*|[.!?…]\s+)(?:ну|вот|значит|так|короче)\s*,\s*/gi, "$1");
  x = x.replace(/(?<!\p{L})(\p{L}+)(?:\s+\1)+(?!\p{L})/giu, "$1");  // «я я я» → «я»
  x = x.replace(/\s+([,.!?…])/g, "$1").replace(/,{2,}/g, ",").replace(/\s{2,}/g, " ").trim().replace(/^[,.\s]+/, "");
  return x ? x[0].toUpperCase() + x.slice(1) : x;
}
let CLEAN = false;
try { CLEAN = localStorage.getItem("ma.clean") === "1"; } catch (_) {}
function shownText(s) { return CLEAN ? cleanFillers(s.text) : s.text; }
function visibleSegs(segs) { return CLEAN ? segs.filter((s) => !FILLER_ONLY.test(s.text.trim())) : segs; }

function segmentsHtml(segs, playable = false, chapters = [], editable = false) {
  let ci = 0;
  return visibleSegs(segs).map((s) => {
    let head = "";
    while (ci < chapters.length && chapters[ci].start <= s.start + 0.5) {
      const c = chapters[ci++];
      const t = fmtTs(c.start).replace(/^00:/, "");
      head += `<div class="chapter-h" id="ch${ci}" data-start="${c.start}"><span>${esc(c.title)}</span>${playable
        ? `<button class="tc" data-t="${c.start}">▶ ${t}</button>` : `<span class="tc-off">${t}</span>`}</div>`;
    }
    const spCol = speakerColor(s.speaker);
    return head + `<div class="utt" data-start="${s.start}" data-i="${s.i}">${playable ? `<button class="ts ts-play" title="Слушать с этого места"><span class="ts-icon">▶</span>${fmtTs(s.start)}</button>` : `<span class="ts">${fmtTs(s.start)}</span>`}${s.label
      ? `<button class="who who-btn" data-seg="${s.i}" style="color:${spCol}" title="Это сказал другой человек?">${esc(s.speaker)}:</button>`
      : `<span class="who" style="color:${spCol}">${esc(s.speaker)}:</span>`} <span class="utt-text">${esc(shownText(s))}</span>${editable
      ? `<button class="edit-btn" data-edit="${s.i}" title="Исправить текст реплики">✎</button>` : ""}</div>`;
  }).join("");
}

// Время [00:12:40] в протоколе, версиях и ответах чата → кнопка «▶ 12:40».
const TIME_RE = /\[((?:\d{1,2}:)?\d{1,2}:\d{2})(?:\s*[–-]\s*(?:\d{1,2}:)?\d{1,2}:\d{2})?\]/g;
function secOf(t) { return t.split(":").reduce((a, x) => a * 60 + +x, 0); }
function linkTimes(html, playable) {
  return html.replace(TIME_RE, (_, t) => playable
    ? `<button class="tc" data-t="${secOf(t)}" title="Слушать с этого места">▶ ${t.replace(/^00:/, "")}</button>`
    : `<span class="tc-off">${t.replace(/^00:/, "")}</span>`);
}
function stripTimes(md) { return md.replace(/\s*\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\s*[–-]\s*(?:\d{1,2}:)?\d{1,2}:\d{2})?\]/g, ""); }

function transcriptHtml(src) {
  const out = [];
  for (const block of src.split(/\n\s*\n/)) {
    const m = /^\*\*\[(\d\d:\d\d:\d\d)\]\s*(.+?):\*\*\s*([\s\S]*)$/.exec(block.trim());
    if (m) {
      out.push(`<div class="utt"><span class="ts">${m[1]}</span><span class="who">${esc(m[2])}:</span> ${esc(m[3])}</div>`);
    } else if (block.trim()) {
      out.push(`<p>${esc(block.trim())}</p>`);
    }
  }
  return out.join("") || '<p class="muted">Расшифровка пуста.</p>';
}

// ---------- состояние ----------

const S = {
  meetings: [],
  current: null,      // открытая встреча (полная)
  tab: "summary",
  view: "meeting",    // meeting | settings | setup
  status: null,
  settings: null,
  doneJobs: new Set(),
  setupDismissed: false,
};

function closeMoreMenu() {
  const m = $("moreMenu"), b = $("moreMenuBtn");
  if (m) m.hidden = true;
  if (b) b.setAttribute("aria-expanded", "false");
}
if ($("moreMenuBtn")) {
  $("moreMenuBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const m = $("moreMenu");
    const open = m.hidden;
    m.hidden = !open;
    $("moreMenuBtn").setAttribute("aria-expanded", String(open));
  });
  $("moreMenu").addEventListener("click", () => closeMoreMenu());
  document.addEventListener("click", (e) => {
    if ($("moreMenu") && !$("moreMenu").hidden && !e.target.closest(".more-wrap")) closeMoreMenu();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMoreMenu(); });
}

function show(view) {
  S.view = view;
  closeMoreMenu();
  for (const id of ["empty", "meeting", "settings", "setup", "voices", "ask"]) $(id).hidden = true;
  stopAudio();
  if (view === "meeting") (S.current ? $("meeting") : $("empty")).hidden = false;
  else $(view).hidden = false;
  if ($("voicesBtn")) $("voicesBtn").classList.toggle("active", view === "voices");
  if ($("settingsBtn")) $("settingsBtn").classList.toggle("active", view === "settings");
  renderList();
}

// ---------- список встреч ----------

async function loadMeetings() {
  try {
    S.meetings = await api("meetings");
  } catch (e) {
    toast(e.message);
  }
  renderFilters();
  renderList();
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const items = S.meetings.filter((m) => passesFilter(m) && (!q || (m.title + " " + m.participants + " " + (m.topics || []).join(" ")).toLowerCase().includes(q)));
  const list = $("list");
  if (!items.length) {
    list.innerHTML = `<div class="list-empty">${q || FILTER.person || FILTER.topic ? "Ничего не нашлось" : "Пока нет ни одной встречи"}</div>`;
    return;
  }
  let html = "", group = null;
  for (const m of items) {
    const d = parseDate(m.date);
    const g = dayLabel(d);
    if (g !== group) { html += `<div class="group">${esc(g)}</div>`; group = g; }
    const active = S.view === "meeting" && S.current && S.current.id === m.id ? " active" : "";
    const sub = [hhmm(d), m.duration].filter(Boolean).join(" · ");
    html += `<button class="item${active}" data-id="${esc(m.id)}">
      <div class="i-title">${esc(m.title)}</div><div class="i-sub">${esc(sub)}</div></button>`;
  }
  list.innerHTML = html;
}

$("list").addEventListener("click", (e) => {
  const b = e.target.closest(".item");
  if (b) openMeeting(b.dataset.id);
});
$("search").addEventListener("input", renderList);
$("search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const q = $("search").value.trim();
  if (q.split(/\s+/).length >= 2 || q.endsWith("?")) {  // похоже на вопрос — в умный поиск
    $("search").value = "";
    renderList();
    openAsk(q);
  }
});

// ---------- умный поиск: вопросы по встречам ----------

const ASK = { history: [], busy: false };

function openAsk(q) {
  show("ask");
  renderAskNote();
  if (q) sendAsk(q);
  $("askInput").focus();
}

function renderAskNote() {
  const s = S.settings || {};
  const b = (s.ask_backends || {})[s.ask_backend] || "";
  $("askNote").innerHTML = b ? `Отвечает: ${esc(b)}${s.ask_backend === "local" ? "" : " — найденные фрагменты встреч уходят в облако"} ·
    <button class="link small" id="askSettings">сменить</button>` : "";
  if ($("askSettings")) $("askSettings").onclick = () => $("settingsBtn").click();
}

function renderAskLog() {
  $("askLog").innerHTML = ASK.history.map((h, i) => {
    if (h.pending) return `<div class="q">${esc(h.q)}</div><div class="a muted"><span class="spinner"></span> Ищу во встречах и формулирую ответ…</div>`;
    if (h.error) return `<div class="q">${esc(h.q)}</div><div class="a err">${esc(h.error)}</div>`;
    const cited = new Set([...h.a.matchAll(/\[(\d+)\]/g)].map((m) => +m[1]));
    const answer = markdown(h.a).replace(/\[(\d+)\]/g, (m, n) =>
      h.sources.some((s) => s.n === +n) ? `<button class="cite" data-h="${i}" data-n="${n}">${n}</button>` : m);
    const src = h.sources.filter((s) => cited.has(s.n));
    return `<div class="q">${esc(h.q)}</div><div class="a md">${answer}
      ${src.length ? `<div class="srcs">${src.map((s) => `<button class="src" data-h="${i}" data-n="${s.n}">
        <b>${s.n}</b> ${esc(dayLabel(parseDate(s.date)))}, ${esc(hhmm(parseDate(s.date)))} — ${esc(s.title)}
        <span class="muted">· ${s.ts ? esc(s.ts.replace(/^00:/, "")) : esc(s.section || "протокол")}</span></button>`).join("")}</div>` : ""}</div>`;
  }).join("");
  $("askLog").lastElementChild && $("askLog").lastElementChild.scrollIntoView({ block: "end" });
}

async function sendAsk(q) {
  if (ASK.busy || !q.trim()) return;
  ASK.busy = true;
  const item = { q, pending: true };
  ASK.history.push(item);
  renderAskLog();
  try {
    const past = ASK.history.filter((h) => h.a).map((h) => ({ q: h.q, a: h.a }));
    const r = await api("ask", { method: "POST", body: { question: q, history: past } });
    Object.assign(item, { pending: false, a: r.answer, sources: r.sources || [] });
  } catch (e) {
    Object.assign(item, { pending: false, error: e.message });
  }
  ASK.busy = false;
  renderAskLog();
}

$("askForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("askInput").value.trim();
  $("askInput").value = "";
  sendAsk(q);
});
$("askInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("askForm").requestSubmit(); }
});
$("askNew").addEventListener("click", () => { ASK.history = []; renderAskLog(); $("askInput").focus(); });

// Ссылка на источник: открыть встречу; для реплики — расшифровку на этом месте.
$("askLog").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-n]");
  if (!b) return;
  const src = ASK.history[+b.dataset.h].sources.find((s) => s.n === +b.dataset.n);
  if (!src) return;
  S.tab = src.ts ? "transcript" : "summary";
  await openMeeting(src.note);
  if (src.ts) {
    const utts = [...document.querySelectorAll("#mBody .utt[data-start]")];
    const t = utts.reduce((best, el) => Math.abs(+el.dataset.start - src.seconds) < Math.abs(+best.dataset.start - src.seconds) ? el : best, utts[0]);
    if (t) { t.scrollIntoView({ block: "center" }); t.classList.add("flash"); setTimeout(() => t.classList.remove("flash"), 2500); }
  }
});

// ---------- встреча ----------

async function openMeeting(id) {
  if (!S.current || S.current.id !== id) stopPlayer();
  try {
    S.current = await api("meeting?id=" + encodeURIComponent(id));
  } catch (e) {
    toast(e.message);
    S.current = null;
    await loadMeetings();
  }
  show("meeting");
  renderMeeting();
}

function renderMeeting() {
  const m = S.current;
  if (!m) return;
  $("mTitle").textContent = m.title;
  const d = parseDate(m.date);
  const llm = S.settings && S.settings.backends[m.llm];
  const tpl = S.settings && (S.settings.templates.find((t) => t.id === m.template) || {}).title;
  $("mMeta").innerHTML = [
    d ? `${dayLabel(d)}, ${hhmm(d)}` : "",
    m.duration,
    m.participants ? "Участники: " + m.participants : "",
    tpl ? "Шаблон: " + tpl : "",
    llm ? "Протокол: " + llm : "",
    m.asr && S.settings.asr_engines[m.asr] ? "Распознавание: " + S.settings.asr_engines[m.asr].title : "",
  ].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("")
    + ((m.topics || []).length ? ` <span class="topics">${m.topics.map((t) => `<button class="topic-tag" data-topic="${esc(t)}" title="Все встречи на эту тему">#${esc(t)}</button>`).join("")}</span>` : "")
    + (m.source ? ` <button class="link small" id="mPeople">${m.expected && m.expected.length
      ? "По списку: " + esc(m.expected.join(", ")) + " · изменить" : "＋ Указать участников"}</button>` : "");
  if ($("mPeople")) $("mPeople").onclick = () => openPeople("meeting");
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === S.tab));
  const stale = m.protocol_error
    ? `<div class="stale">Протокол не составлен: ${esc(m.protocol_error.slice(0, 200))}. Расшифровка сохранена.
       <button class="ghost" id="staleRedo">Составить протокол</button>${protocolFallbackHtml(m)}</div>`
    : m.protocol_stale
    ? `<div class="stale">Разметка собеседников изменена — протокол составлен по старой.
       <button class="ghost" id="staleRedo">Пересоздать протокол</button></div>` : "";
  const playing = $("audio").src && !$("audio").paused;
  $("player").hidden = !(m.can_play && (S.tab === "transcript" || playing));
  if (S.tab === "summary") {
    $("mBody").innerHTML = versionsBarHtml(m) + (VER.sel[m.id] ? versionBodyHtml(m)
      : SUMEDIT.id === m.id ? summaryEditorHtml(m)
      : stale + summaryToolsHtml(m) + linkTimes(markdown(m.summary), m.can_play));
  } else if (S.tab === "chat") {
    $("mBody").innerHTML = meetingChatHtml(m);
    const inp = $("mcInput");
    if (inp) inp.value = MCHAT.draft[m.id] || "";
  } else {
    $("mBody").innerHTML = (m.segments && m.segments.length)
      ? (stale || `<p class="muted small">${m.can_play ? "Нажмите на время у реплики, чтобы послушать с этого места. " : ""}Ошибка в тексте — ✎ у реплики (отмена — ⌘Z). Подпись неверна — нажмите на имя.</p>`)
        + transcriptToolsHtml(m) + chaptersTocHtml(m)
        + segmentsHtml(m.segments, m.can_play, m.chapters || [], !!m.source)
      : transcriptHtml(m.transcript);
    highlightPlaying(true);
  }
  if ($("staleRedo")) $("staleRedo").onclick = () => redoProtocol();
  if ($("staleLight")) $("staleLight").onclick = () => redoProtocol("gemini", $("staleLight").dataset.model);
  if ($("staleQwen")) $("staleQwen").onclick = () => redoProtocol("local");
  $("redoBtn").disabled = !m.source;
  if (SUMEDIT.id && SUMEDIT.id !== m.id) SUMEDIT.id = null;
  renderSpeakers();
  if (S.tab === "summary") linkContacts($("mBody"));
}

// ---------- плеер: звук встречи синхронно с расшифровкой ----------

function stopPlayer() {
  const a = $("audio");
  a.pause();
  a.removeAttribute("src");
  a.load();
  a.dataset.id = "";
  $("player").hidden = true;
}

function playFrom(sec) {
  const a = $("audio"), m = S.current;
  if (!m || !m.can_play) return;
  if (a.dataset.id !== m.id) {
    a.preload = "auto";
    a.src = `/api/audio?id=${encodeURIComponent(m.id)}&token=${encodeURIComponent(TOKEN)}`;
    a.dataset.id = m.id;
    a.load();
    toast("Готовлю звук встречи — первый раз это занимает несколько секунд", 3000);
  }
  $("player").hidden = false;
  const go = () => { a.currentTime = Math.max(0, sec - 0.3); a.play().catch(() => {}); };
  a.readyState >= 1 ? go() : a.addEventListener("loadedmetadata", go, { once: true });
}

let lastPlaying = null;
function highlightPlaying(noScroll = false) {
  const a = $("audio");
  if (S.tab !== "transcript" || !S.current || a.dataset.id !== S.current.id) return;
  const t = a.currentTime;
  const utts = document.querySelectorAll("#mBody .utt[data-start]");
  let cur = null;
  for (const el of utts) { if (+el.dataset.start <= t + 0.25) cur = el; else break; }
  if (cur === lastPlaying && cur && cur.classList.contains("playing")) return;
  document.querySelectorAll("#mBody .utt.playing").forEach((el) => el.classList.remove("playing"));
  lastPlaying = cur;
  if (!cur) return;
  cur.classList.add("playing");
  if (!noScroll && !a.paused && $("follow").checked) cur.scrollIntoView({ block: "center", behavior: "smooth" });
}
$("audio").addEventListener("timeupdate", () => highlightPlaying());
$("audio").addEventListener("seeked", () => highlightPlaying());
$("audio").addEventListener("error", () => {
  if ($("audio").getAttribute("src")) toast("Не удалось открыть звук встречи: аудио могло быть удалено после обработки", 5000);
});

// ---------- чат по одной встрече: вопросы, письма, таблицы ----------

const MCHAT = { log: {}, draft: {} };
const QUICK = [
  ["Письмо-итог участникам", "Составь короткое деловое письмо участникам по итогам встречи: благодарность за встречу, ключевые решения, задачи с ответственными и сроками, следующие шаги. Тема письма — первой строкой."],
  ["Задачи таблицей", "Сведи все задачи и поручения со встречи в таблицу: Задача | Ответственный | Срок | Комментарий. Если ответственный или срок не названы — «не указан»."],
  ["Что не решено?", "Какие вопросы остались открытыми, спорными или отложенными? По каждому: суть, кто может решить, что нужно уточнить."],
  ["Рекомендации", "Дай 3–5 рекомендаций по итогам встречи: риски, что упустили, что стоит сделать дальше. Опирайся только на сказанное."],
  ["Кратко за 30 секунд", "Перескажи встречу в 5 пунктах для руководителя, который на ней не был."],
];

function meetingChatHtml(m) {
  const log = MCHAT.log[m.id] || [];
  const backend = S.settings && S.settings.ask_backends ? S.settings.ask_backends[S.settings.ask_backend] : "";
  return `<div class="mchat">
    <p class="muted small">Вопросы и задания по этой встрече: ответ по протоколу и расшифровке${backend ? ` · отвечает ${esc(backend)} (меняется в Настройках → Умный поиск)` : ""}.</p>
    <div class="quick">${QUICK.map(([t], i) => `<button class="ghost small" type="button" data-quick="${i}">${esc(t)}</button>`).join("")}</div>
    ${log.map((h, i) => `<div class="q">${esc(h.q)}</div>` + (h.pending
      ? '<div class="a muted"><span class="spinner"></span> Читаю встречу и пишу ответ…</div>'
      : h.error ? `<div class="a err">${esc(h.error)}${fallbackHtml(h.error, "chat", i)}</div>`
      : `<div class="a md"><button class="ghost small copy-a" type="button" data-copy-a="${i}">Копировать</button>${linkTimes(markdown(h.a), m.can_play)}</div>`)).join("")}
    <form class="ask-form" id="mcForm">
      <textarea id="mcInput" rows="2" placeholder="Например: что решили по срокам? Кто отвечает за API?"></textarea>
      <button class="primary" type="submit">Спросить</button>
    </form>
    ${log.length ? '<button class="link small" type="button" id="mcClear">Начать заново</button>' : ""}</div>`;
}

// Облачная модель не ответила (перегрузка, лимит, нет связи) — предложить модель полегче или Qwen на компьютере.
const GEMINI_LIGHT = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"];
function fallbackOptions(err) {
  if (!/перегруж|лимит|429|50[0234]|Нет связи|timed out|timeout|не ответил/i.test(err || "")) return [];
  const st = S.settings || {}, b = st.ask_backend || "local", opts = [];
  if (b === "gemini") {
    const cur = st.ask_gemini_model || st.gemini_model || "";
    const light = GEMINI_LIGHT.find((x) => x !== cur);
    if (light) opts.push({ label: `Повторить на ${light}`, backend: "gemini", gemini_model: light });
  }
  const qwen = S.status && S.status.models && S.status.models.llm && S.status.models.llm.status === "ready";
  if (b !== "local" && qwen) opts.push({ label: `Повторить на Qwen (на ${PC}, без интернета)`, backend: "local" });
  return opts;
}
function fallbackHtml(err, kind, idx) {
  const opts = fallbackOptions(err);
  return opts.length ? `<div class="fallback">${opts.map((o, j) =>
    `<button class="ghost small" type="button" data-fb="${kind}" data-fb-i="${idx}" data-fb-o="${j}">${esc(o.label)}</button>`).join("")}</div>` : "";
}

async function sendMeetingAsk(q, override = null) {
  const m = S.current;
  if (!m || !q.trim()) return;
  const log = MCHAT.log[m.id] = MCHAT.log[m.id] || [];
  const item = { q: q.trim(), pending: true };
  log.push(item);
  MCHAT.draft[m.id] = "";
  if (S.tab === "chat") renderMeeting();
  try {
    const history = log.filter((h) => h.a).slice(-3).map((h) => ({ q: h.q, a: h.a }));
    const r = await api("meeting_ask", { method: "POST", body: { id: m.id, question: item.q, history, ...(override || {}) } });
    item.a = r.answer;
  } catch (e) {
    item.error = e.message;
  }
  item.pending = false;
  if (S.current && S.current.id === m.id && S.tab === "chat") {
    renderMeeting();
    const last = document.querySelector("#mBody .mchat .a:last-of-type");
    if (last) last.scrollIntoView({ block: "nearest" });
  }
}

// ---------- скачать: Word, PDF, Markdown ----------

// Word: тот же текст, что на экране (версия, без времени, при желании без слов-паразитов).
function exportMd(m, withTranscript) {
  const v = currentVersion(m);
  let md = stripTimes(v ? v.text : m.summary);
  if (withTranscript && m.segments && m.segments.length) {
    md += "\n\n## Расшифровка\n\n" + visibleSegs(m.segments).map((s) => `**[${fmtTs(s.start)}] ${s.speaker}:** ${shownText(s)}`).join("\n\n");
  } else if (withTranscript) md += "\n\n## Расшифровка\n\n" + m.transcript;
  return { title: v ? v.title : m.title, md };
}

function exportHtml(m, withTranscript) {
  const sub = [m.date, m.duration, m.participants ? "Участники: " + m.participants : ""].filter(Boolean).join(" · ");
  const v = currentVersion(m);
  let html = `<h1>${esc(v ? v.title : m.title)}</h1><div class="sub">${esc(sub)}</div>` + markdown(stripTimes(v ? v.text : m.summary));
  if (withTranscript) {
    html += "<h2>Расшифровка</h2>" + ((m.segments && m.segments.length)
      ? visibleSegs(m.segments).map((s) => `<div class="utt"><span class="ts">${fmtTs(s.start)}</span><span class="who">${esc(s.speaker)}:</span> ${esc(shownText(s))}</div>`).join("")
      : transcriptHtml(m.transcript));
  }
  return html;
}

$("exportBtn").addEventListener("click", () => {
  if (!S.current) return;
  $("exportDialog").showModal();
});
$("exportDialog").addEventListener("close", async () => {
  if ($("exportDialog").returnValue !== "ok" || !S.current) return;
  const fmt = document.querySelector('input[name="exportFmt"]:checked').value;
  const withTr = $("exportTr").checked;
  toast(fmt === "pdf" ? "Готовлю PDF…" : "Сохраняю…", 2500);
  try {
    const r = await api("export", { method: "POST", body: {
      id: S.current.id, format: fmt, transcript: withTr, version: (currentVersion(S.current) || {}).id, clean: CLEAN,
      html: fmt === "pdf" ? exportHtml(S.current, withTr) : undefined,
      doc: fmt === "docx" ? exportMd(S.current, withTr) : undefined } });
    const name = r.path.split(/[\\/]/).pop();
    toast(r.manual ? "Страница открыта в браузере: выберите «Сохранить как PDF» в окне печати"
      : `Сохранено в «Загрузки»: ${name}`, 6000);
  } catch (e) {
    toast(e.message, 6000);
  }
});
document.querySelectorAll('input[name="exportFmt"]').forEach((r) => r.addEventListener("change", () => {
  $("exportTrRow").hidden = document.querySelector('input[name="exportFmt"]:checked').value === "md";
}));

// ---------- окно «Обновления»: все компоненты в одном месте ----------
// Код и интерфейс (GitVerse или GitLab), пакеты Python (Windows), модели (Hugging Face), ускорение NVIDIA, оболочка.

const UPD = { info: null, checking: false, busy: "", error: "", count: 0 };

function updCount() {
  const u = UPD.info || {}, g = S.gpu, models = (S.status && S.status.models) || {};
  let n = 0;
  if (u.code_available) n++;
  if (u.pending) n++;  // скачано и ждёт перезапуска — напоминание
  if ((u.platform === "windows" || !u.python_bundled) && (u.packages_changed || u.packages_pending)) n++;
  if (u.shell_download) n++;
  if (IS_WIN && g && g.nvidia && g.outdated) n++;
  n += Object.values(models).filter((m) => m.required && m.status !== "ready" && m.status !== "downloading").length;
  return n;
}

// Строка «Доступны обновления» над кнопками боковой панели — только когда есть что обновить
// (или скачанное ждёт перезапуска). Без обновлений окно открывается из Настроек.
function paintUpdBadge() {
  UPD.count = updCount();
  const u = UPD.info || {};
  $("updatesBtn").hidden = !UPD.count;
  $("updatesBtn").firstChild.textContent = u.pending ? "Обновление ждёт перезапуска " : "Доступны обновления ";
  $("updBadge").textContent = UPD.count;
}

// При запуске — тихая проверка (площадки — не чаще раза в сутки).
async function autoCheckUpdates() {
  try { UPD.info = await api("update/check", { method: "POST", body: { cached: true } }); }
  catch (_) { try { UPD.info = await api("update"); } catch (_) {} }
  if (IS_WIN && S.gpu === undefined) { S.gpu = null; await loadGpu(); }
  paintUpdBadge();
}

function updRow(title, state, body, action = "") {
  return `<div class="upd-row"><div class="grow"><div class="r-title">${title} ${state}</div>${body ? `<div class="muted small">${body}</div>` : ""}</div>${action}</div>`;
}

function updWindowHtml() {
  const u = UPD.info || {}, st = S.status || {}, models = st.models || {};
  const known = u.latest !== undefined;
  const out = [];
  out.push(`<p class="muted small">Обновления скачиваются по компонентам — без переустановки приложения.
    Код — с <a href="https://gitverse.ru/sir1knight/Meeting_agent" target="_blank">GitVerse</a>,
    <a href="https://gitlab.com/sir1knight/Meeting_agent" target="_blank">GitLab</a> или
    <a href="https://github.com/sir1knight/meeting-agent" target="_blank">GitHub</a> — откуда доступнее${u.source ? ` (сейчас — ${esc(u.source)})` : ""},
    модели — с Hugging Face${IS_WIN ? ", пакеты — с PyPI" : ""}.</p>`);
  if (UPD.error) out.push(`<div class="stale">${esc(UPD.error)}</div>`);

  // 1. Код и интерфейс
  let code;
  if (u.pending) {
    const what = u.pending === "bundled" ? "Возврат к версии из приложения" : `Скачана версия ${esc(u.pending)}`;
    code = updRow("Код и интерфейс", `<span class="badge warn">ждёт перезапуска</span>`,
      `${what} — применится при следующем запуске приложения. Можно продолжать работать; перезапустите, когда удобно
       (запись и обработка встреч при этом не должны идти).`,
      `<span class="row"><button class="primary small" type="button" data-upd="restart"${UPD.busy ? " disabled" : ""}>Перезапустить сейчас</button>
       <button class="link small" type="button" data-upd="cancel">Отменить</button></span>`);
  } else if (!known) code = updRow("Код и интерфейс", `<span class="muted small">версия ${esc(u.version || "…")}</span>`, "Нажмите «Проверить обновления».");
  else if (u.code_available && u.shell_needed) code = updRow("Код и интерфейс", '<span class="badge warn">нужно приложение целиком</span>',
    `Версии ${esc(u.latest)} нужна новая оболочка — скачайте приложение целиком со <a href="${esc(u.page)}" target="_blank">страницы проекта</a>.`);
  else if (u.code_available) code = updRow("Код и интерфейс", `<span class="badge warn">${esc(u.version)} → ${esc(u.latest)}</span>`,
    changesHtml(u), `<button class="primary small" type="button" data-upd="code"${UPD.busy ? " disabled" : ""}>${UPD.busy === "code" ? "Обновляю…" : "Обновить"}</button>`);
  else code = updRow("Код и интерфейс", `<span class="badge ok">${esc(u.version)} — последняя</span>`, "");
  out.push(code);

  // 2. Пакеты Python (Windows; Mac — с 1.5, когда Python не внутри приложения)
  if (u.platform === "windows" || !u.python_bundled) {
    const need = u.packages_changed || u.packages_pending;
    out.push(updRow("Пакеты Python", need ? '<span class="badge warn">есть новые</span>' : '<span class="badge ok">актуальны</span>',
      need ? (u.packages_pending ? `Поставятся при следующем запуске приложения (${IS_WIN ? "окно консоли" : "окно «Установка компонентов»"}, несколько минут) — докачаются только изменившиеся.` : "Сначала скачайте новую версию кода — список пакетов придёт вместе с ней.") : "",
      u.packages_pending && !u.pending ? '<button class="ghost small" type="button" data-upd="restart">Перезапустить сейчас</button>' : ""));
  }

  // 3. Модели
  const mrows = Object.entries(models).filter(([, m]) => m.required || m.status === "ready").map(([name, m]) => {
    const state = m.status === "ready" ? '<span class="badge ok">скачана</span>'
      : m.status === "downloading" ? '<span class="badge warn">скачивается</span>' : '<span class="badge warn">не скачана</span>';
    return updRow(esc(m.title), state, "", m.status === "ready" ? "" : `<div class="upd-dl">${downloadWidget(name, m)}</div>`);
  });
  out.push(`<h3>Модели</h3>${mrows.join("") || '<p class="muted small">Нет моделей.</p>'}`);

  // 4. Ускорение NVIDIA (Windows с видеокартой NVIDIA)
  if (IS_WIN && S.gpu && S.gpu.nvidia) {
    const g = S.gpu;
    const action = g.state === "downloading" ? '<span class="spinner"></span>'
      : g.state === "ready" && !g.outdated ? ""
      : `<button class="ghost small" type="button" data-gpu-upd="install">${g.state === "ready" ? "Обновить" : "Скачать"} ≈ ${fmtBytes(g.size)}</button>`;
    const state = g.state === "ready" ? (g.outdated ? `<span class="badge warn">${esc(g.installed_tag || "старая")} → ${esc(g.latest_tag)}</span>` : '<span class="badge ok">установлено</span>')
      : g.state === "downloading" ? `<span class="badge warn">${esc(g.part)}: ${Math.round(100 * g.done / (g.total || 1))}%</span>`
      : '<span class="muted small">не установлено</span>';
    out.push(`<h3>Ускорение NVIDIA</h3>` + updRow(esc(g.nvidia.name), state,
      g.state === "error" ? esc(g.error) : "Библиотеки CUDA для Whisper и Qwen на видеокарте.", action));
  }

  // 5. Оболочка
  const sd = u.shell_download;
  out.push("<h3>Приложение</h3>" + updRow(u.platform === "mac" ? "Приложение (окно, запись звука)" : "Оболочка (Meeting Agent.exe и Python)",
    sd ? `<span class="badge warn">версия ${esc(u.shell)} → ${esc(sd.version)}</span>`
      : known && u.shell_needed ? '<span class="badge warn">нужна новая</span>' : `<span class="badge ok">версия ${esc(u.shell || "?")}</span>`,
    sd ? `Новое приложение${sd.size ? ` (~${Math.round(sd.size / 1048576)} МБ)` : ""}: скачается, проверится подпись, приложение закроется и откроется уже новым — разрешения macOS сохранятся.${u.python_bundled ? " Python и пакеты переедут из приложения в папку данных: дальше они обновляются этой же кнопкой и только частями." : ""} Во время записи не обновляется.`
      : u.platform === "mac" ? "Обновляется этой же кнопкой, когда выходит новая версия." : "Обновляется только новой версией приложения целиком — если она понадобится, здесь появится ссылка.",
    sd ? `<button class="primary small" type="button" data-upd="shell"${UPD.busy ? " disabled" : ""}>${UPD.busy === "shell" ? "Обновляю…" : "Обновить"}</button>` : ""));

  const n = updCount();
  out.push(`<div class="dlg-actions upd-actions">
    ${u.overlay ? '<button class="link small" type="button" data-upd="reset">Вернуться к версии из приложения</button>' : ""}
    <button class="ghost" type="button" data-upd="check"${UPD.checking ? " disabled" : ""}>${UPD.checking ? '<span class="spinner"></span> Проверяю…' : "Проверить обновления"}</button>
    <button class="primary" type="button" data-upd="all"${!n || UPD.busy ? " disabled" : ""}>${UPD.busy === "all" ? "Обновляю…" : n ? `Обновить всё (${n})` : "Всё актуально"}</button>
    <button class="ghost" type="button" data-upd="close">Закрыть</button></div>`);
  return out.join("");
}

// После перезапуска с новой версией — окно «Обновление установлено» (один раз на версию).
async function showUpdateDone() {
  let u;
  try { u = (await api("update")).just_updated; } catch (_) { return; }
  if (!u) return;
  const groups = Object.entries(u.changes || {});
  $("updDoneBody").innerHTML = `<p>Meeting Agent обновлён: <b>${esc(u.from)} → ${esc(u.to)}</b>${u.date ? ` <span class="muted">(${esc(u.date)})</span>` : ""}.</p>
    ${groups.length ? `<div class="upd-done-changes">${groups.map(([g, items]) =>
      `<div class="small"><b>${esc(g)}</b><ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`).join("")}</div>`
      : (u.notes ? `<p class="muted">${esc(u.notes)}</p>` : "")}`;
  const d = $("updDoneDialog");
  d.addEventListener("close", () => api("update/seen", { method: "POST", body: {} }).catch(() => {}), { once: true });
  d.showModal();
}

// «Что нового» версий с GitVerse/GitLab/GitHub (разделы CHANGELOG.md, перенесённые в version.json).
function changesHtml(u) {
  const groups = Object.entries(u.changes || {});
  if (!groups.length) return esc(u.notes || "");
  const label = (u.changes_releases || 1) > 1 && u.version
    ? `Что нового (${esc(u.version)} → ${esc(u.latest)})`
    : `Что нового в ${esc(u.latest)}${u.date ? ` (${esc(u.date)})` : ""}`;
  return `<details class="changes"><summary>${label}</summary>
    ${groups.map(([g, items]) => `<div class="small"><b>${esc(g)}</b><ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`).join("")}</details>`;
}

function paintUpdWindow() {
  paintUpdBadge();
  if (!$("updDialog").open) return;
  $("updBody").innerHTML = updWindowHtml();
  $("updBody").querySelectorAll("[data-upd]").forEach((b) => b.onclick = () => updAction(b.dataset.upd));
  $("updBody").querySelectorAll("[data-gpu-upd]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { await api("gpu/install", { method: "POST", body: {} }); } catch (e) { toast(e.message, 6000); }
    await loadGpu(); watchGpu();
  });
}

async function openUpdates() {
  $("updDialog").showModal();
  paintUpdWindow();
  if (!UPD.info) await autoCheckUpdates();
  paintUpdWindow();
}

let gpuWatch = null;
function watchGpu() {
  clearTimeout(gpuWatch);
  if (S.gpu && S.gpu.state === "downloading") gpuWatch = setTimeout(async () => { await loadGpu(); paintUpdWindow(); watchGpu(); }, 1500);
}

async function updAction(act) {
  UPD.error = "";
  if (act === "close") { $("updDialog").close(); return; }
  if (act === "check") {
    UPD.checking = true; paintUpdWindow();
    try { UPD.info = await api("update/check", { method: "POST", body: {} }); }
    catch (e) { UPD.error = e.message; }
    if (IS_WIN) await loadGpu();
    UPD.checking = false; paintUpdWindow();
  } else if (act === "code" || act === "reset") {
    if (act === "reset" && !confirm("Вернуться к версии, встроенной в приложение? Это применится при следующем перезапуске.")) return;
    await applyCode(act);
  } else if (act === "cancel") {
    await api("update/cancel", { method: "POST", body: {} }).catch((e) => toast(e.message));
    await refreshUpdInfo();
  } else if (act === "restart") {
    await restartNow();
  } else if (act === "all") {
    await updateAll();
  } else if (act === "shell") {
    await installShell();
  }
}

// Новое приложение (Mac): скачать, проверить подпись, закрыться — помощник заменит и откроет новое.
async function installShell() {
  UPD.busy = "shell"; paintUpdWindow();
  try {
    await api("update/shell", { method: "POST", body: {} });
    toast("Новое приложение готово — Meeting Agent закроется и откроется заново через несколько секунд", 15000);
  } catch (e) {
    UPD.error = e.message;
    UPD.busy = "";
    paintUpdWindow();
  }
}

// Только скачать: новая версия применится при следующем запуске — когда удобно пользователю.
async function applyCode(act = "code") {
  UPD.busy = "code"; paintUpdWindow();
  try {
    const r = await api(act === "reset" ? "update/reset" : "update/apply", { method: "POST", body: {} });
    toast(act === "reset" ? "Возврат к встроенной версии — при следующем перезапуске"
      : `Версия ${r.version} скачана. Применится при следующем запуске — или нажмите «Перезапустить сейчас»`, 7000);
  } catch (e) {
    UPD.error = e.message;
  }
  UPD.busy = "";
  await refreshUpdInfo();
}

async function refreshUpdInfo() {
  try { UPD.info = { ...(UPD.info || {}), ...(await api("update")) }; } catch (_) {}
  if (UPD.info && UPD.info.latest && UPD.info.pending === UPD.info.latest) UPD.info.code_available = false;
  paintUpdWindow();
}

// Перезапуск: на Mac — только сервер (оболочка поднимет его с новым кодом), на Windows — приложение целиком
// (при запуске ставятся и новые пакеты Python).
async function restartNow() {
  try {
    await api("update/restart", { method: "POST", body: { dry: IS_WIN } });
  } catch (e) {
    toast(e.message, 6000);
    return;
  }
  UPD.busy = "restart"; paintUpdWindow();
  if (IS_WIN) { toast("Перезапускаю Meeting Agent…", 4000); native("restartApp"); return; }
  toast("Перезапускаю…", 3000);
  setTimeout(waitServerAndReload, 2500);
}

// «Обновить всё»: сначала то, что качается в фоне (модели, NVIDIA), потом код — он перезапускает сервер.
async function updateAll() {
  UPD.busy = "all"; paintUpdWindow();
  try {
    const models = (S.status && S.status.models) || {};
    for (const [name, m] of Object.entries(models)) {
      if (m.required && m.status !== "ready" && m.status !== "downloading") {
        await api("models/download", { method: "POST", body: { name } }).catch((e) => toast(e.message));
      }
    }
    if (IS_WIN && S.gpu && S.gpu.nvidia && S.gpu.outdated) {
      await api("gpu/install", { method: "POST", body: {} }).catch((e) => toast(e.message));
    }
    for (let i = 0; i < 7200; i++) {  // ждём фоновые загрузки (до 2 часов)
      await pollStatus();
      if (IS_WIN) await loadGpu();
      paintUpdWindow();
      const ms = Object.values((S.status && S.status.models) || {});
      if (!ms.some((m) => m.status === "downloading") && !(S.gpu && S.gpu.state === "downloading")) break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    const u = UPD.info || {};
    if (u.code_available && !u.shell_needed) await applyCode();
    if (u.shell_download) { await installShell(); return; }  // последним: приложение перезапустится
    toast(UPD.info && UPD.info.pending ? "Всё скачано. Новая версия применится при перезапуске — когда будет удобно" : "Готово", 6000);
  } finally {
    if (UPD.busy === "all") UPD.busy = "";
    paintUpdWindow();
  }
}

async function waitServerAndReload() {
  for (let i = 0; i < 60; i++) {
    try { await api("status"); location.reload(); return; } catch (_) {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  toast("Сервер не поднялся — перезапустите приложение", 8000);
}

$("updatesBtn").addEventListener("click", openUpdates);

// ---------- свои шаблоны протоколов ----------

let tplEditing = null;  // id своего шаблона, который меняем (null — новый)
function tplMode(mode) {
  document.querySelectorAll("#tplModes .tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
  $("tplPointsBox").hidden = mode !== "points";
  $("tplTextBox").hidden = mode !== "text";
  $("tplPastBox").hidden = mode !== "past";
  if (mode === "past") renderTplPast();
}

// Шаблон по прошлым встречам: выбираем встречи одного типа, модель протоколов предлагает шаблон.
function tplPastMeetings() {
  const n = +$("tplPastN").value, tpl = $("tplPastTpl").value, topic = $("tplPastTopic").value, person = $("tplPastPerson").value;
  return S.meetings.filter((m) => (!tpl || m.template === tpl)
    && (!topic || (m.topics || []).some((t) => t.toLowerCase() === topic))
    && (!person || personsOf(m).includes(person))).slice(0, n);
}
function renderTplPast() {
  const keep = (id) => $(id).value;
  const fill = (id, opts, all) => { const v = keep(id); $(id).innerHTML = `<option value="">${all}</option>` + opts.map(([val, t]) => `<option value="${esc(val)}"${val === v ? " selected" : ""}>${esc(t)}</option>`).join(""); };
  const tpls = (S.settings && S.settings.templates) || [];
  const used = new Set(S.meetings.map((m) => m.template));
  fill("tplPastTpl", tpls.filter((t) => used.has(t.id)).map((t) => [t.id, t.title]), "Любой");
  fill("tplPastTopic", [...new Set(S.meetings.flatMap((m) => (m.topics || []).map((t) => t.toLowerCase())))].sort().map((t) => [t, t]), "Любая");
  fill("tplPastPerson", [...new Set(S.meetings.flatMap(personsOf))].sort().map((p) => [p, p]), "Любой");
  const ms = tplPastMeetings();
  $("tplPastList").innerHTML = ms.length
    ? `<div class="muted small">Встречи (${ms.length}):</div>` + ms.map((m) => `<div class="small">· ${esc(m.title)} <span class="muted">${esc(m.date.slice(0, 10))}</span></div>`).join("")
    : '<div class="muted small">Нет подходящих встреч — ослабьте условия.</div>';
  const b = S.settings && S.settings.template_backend;
  $("tplPastWarn").className = "small warn-text";
  $("tplPastWarn").textContent = !b
    ? "Шаблон составляет облачная модель — локальному Qwen эта задача не по силам. Добавьте ключ Gemini, ChatGPT или Claude в Настройках."
    : `Протоколы и расшифровки этих встреч уйдут в ${(S.settings.backends || {})[b] || "облачный сервис"}. Уточните, разрешено ли это для рабочих встреч.`;
  $("tplPastGo").disabled = !b || ms.length < 2 || TPLGEN.busy;
  $("tplPastGo").innerHTML = TPLGEN.busy ? '<span class="spinner"></span> Изучаю встречи…' : "Составить шаблон";
}
const TPLGEN = { busy: false };
["tplPastN", "tplPastTpl", "tplPastTopic", "tplPastPerson"].forEach((id) => $(id).addEventListener("change", renderTplPast));
$("tplPastGo").addEventListener("click", async () => {
  const ms = tplPastMeetings();
  if (ms.length < 2) return;
  TPLGEN.busy = true; renderTplPast();
  try {
    const r = await api("template/generate", { method: "POST", body: { ids: ms.map((m) => m.id), hint: $("tplPastHint").value } });
    if (!$("tplName").value.trim()) $("tplName").value = r.title;
    $("tplText").value = r.text;
    tplMode("text");
    toast("Шаблон готов — проверьте и поправьте текст, затем «Сохранить»", 6000);
  } catch (e) { toast(e.message, 6000); }
  TPLGEN.busy = false;
  if (!$("tplPastBox").hidden) renderTplPast();
});
document.querySelectorAll("#tplModes .tab").forEach((t) => t.addEventListener("click", () => tplMode(t.dataset.mode)));

async function openTemplate(id, copy = false) {
  tplEditing = copy ? null : id;
  $("tplName").value = ""; $("tplPoints").value = ""; $("tplNotes").value = ""; $("tplText").value = "";
  $("tplTitle").textContent = id && !copy ? "Изменить шаблон" : "Свой шаблон протокола";
  if (id) {
    try {
      const t = await api("template?id=" + encodeURIComponent(id));
      $("tplName").value = copy ? t.title + " — моя версия" : t.title;
      $("tplText").value = t.text;
    } catch (e) { toast(e.message); return; }
    tplMode("text");
  } else {
    tplMode("points");
  }
  $("templateDialog").showModal();
}

$("templateDialog").addEventListener("close", async () => {
  if ($("templateDialog").returnValue !== "ok") return;
  const points = !$("tplPointsBox").hidden;
  const body = { id: tplEditing, title: $("tplName").value };
  if (points) { body.points = $("tplPoints").value; body.notes = $("tplNotes").value; }
  else body.text = $("tplText").value;
  try {
    const t = await api("template", { method: "POST", body });
    S.settings = await api("settings");
    if (S.view === "settings") renderSettings();
    toast(`Шаблон «${t.title}» сохранён — выберите его при пересоздании протокола или как шаблон по умолчанию`, 6000);
  } catch (e) {
    toast(e.message, 5000);
    $("templateDialog").showModal();
  }
});

// ---------- список участников (вручную: до, во время и после встречи) ----------

let peopleTarget = null;
async function openPeople(target) {
  peopleTarget = target;
  let data = { next: [], current: [], known: [] };
  try { data = await api("participants"); } catch (_) {}
  const names = target === "next" ? data.next : target === "current" ? data.current : (S.current.expected || []);
  $("peopleTitle").textContent = target === "next" ? "Участники следующей встречи"
    : target === "current" ? "Участники текущей встречи" : "Участники этой встречи";
  $("peopleHint").textContent = target === "next"
    ? "Список привяжется к ближайшей записи или импортированному файлу. Имена помогают Whisper правильно их расслышать, а Meeting Agent — узнать собеседников по голосу и по обращениям."
    : target === "current" ? "Список учтётся при обработке этой записи."
    : "Чтобы список учёлся, после сохранения пересоздайте протокол с галочкой «Заново расшифровать аудио».";
  $("peopleNames").value = names.join(", ");
  const renderKnown = () => {
    const have = new Set($("peopleNames").value.split(/[,;\n]/).map((x) => x.trim().toLowerCase()));
    $("peopleKnown").innerHTML = data.known.length
      ? '<span class="muted small">Из «Голосов»:</span> ' + data.known.map((n) =>
        `<button type="button" class="tag${have.has(n.toLowerCase()) ? " on" : ""}" data-name="${esc(n)}">${esc(n)}</button>`).join("")
      : "";
  };
  renderKnown();
  $("peopleNames").oninput = renderKnown;
  $("peopleKnown").onclick = (e) => {
    const n = e.target.dataset.name;
    if (!n) return;
    const list = $("peopleNames").value.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
    const i = list.findIndex((x) => x.toLowerCase() === n.toLowerCase());
    i >= 0 ? list.splice(i, 1) : list.push(n);
    $("peopleNames").value = list.join(", ");
    renderKnown();
  };
  $("peopleDialog").showModal();
}

$("peopleDialog").addEventListener("close", async () => {
  const v = $("peopleDialog").returnValue;
  if (v !== "ok" && v !== "clear") return;
  const target = peopleTarget === "meeting" ? S.current.id : peopleTarget;
  try {
    const r = await api("participants", { method: "POST", body: {
      target, names: v === "clear" ? [] : $("peopleNames").value.split(/[,;\n]/),
    } });
    if (peopleTarget === "meeting") {
      S.current.expected = r.names;
      renderMeeting();
      toast("Сохранено. Пересоздайте протокол с галочкой «Заново расшифровать аудио», чтобы список учёлся", 6000);
    } else {
      toast(r.names.length ? "Участники: " + r.names.join(", ") : "Список очищен");
    }
    renderNextPeople();
  } catch (e) {
    toast(e.message);
  }
});

async function renderNextPeople() {
  const rec = S.status && S.status.recording && S.status.recording.state === "recording";
  $("nextPeople").hidden = !!rec;
  if (rec) return;
  try {
    const d = await api("participants");
    $("nextPeople").innerHTML = d.next.length
      ? `<span class="muted">Следующая встреча:</span> ${esc(d.next.join(", "))}`
      : '<span class="muted">＋ Участники следующей встречи</span>';
  } catch (_) {}
}

$("nextPeople").addEventListener("click", () => openPeople("next"));
$("recPeople").addEventListener("click", () => openPeople("current"));

// ---------- ручное разделение собеседников ----------

$("mBody").addEventListener("click", (e) => {
  const ts = e.target.closest(".ts-play");
  if (ts) { playFrom(+ts.closest(".utt").dataset.start); return; }
  const qb = e.target.closest("[data-quick]");
  if (qb) { sendMeetingAsk(QUICK[+qb.dataset.quick][1]); return; }
  const cb = e.target.closest("[data-copy-a]");
  if (cb) {
    const h = (MCHAT.log[S.current.id] || [])[+cb.dataset.copyA];
    if (h && h.a) navigator.clipboard.writeText(h.a).then(() => toast("Скопировано")).catch(() => toast("Не удалось скопировать"));
    return;
  }
  if (e.target.id === "mcClear") { MCHAT.log[S.current.id] = []; renderMeeting(); }
});
$("mBody").addEventListener("submit", (e) => {
  if (e.target.id !== "mcForm") return;
  e.preventDefault();
  sendMeetingAsk($("mcInput").value);
});
$("mBody").addEventListener("input", (e) => {
  if (e.target.id === "mcInput" && S.current) MCHAT.draft[S.current.id] = e.target.value;
});
$("mBody").addEventListener("keydown", (e) => {
  if (e.target.id === "mcInput" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("mcForm").requestSubmit(); }
});

let assignSeg = null;
$("mBody").addEventListener("click", (e) => {
  const b = e.target.closest("[data-seg]");
  if (!b) return;
  assignSeg = S.current.segments.find((s) => s.i === +b.dataset.seg);
  $("assignQuote").textContent = `${fmtTs(assignSeg.start)} — «${assignSeg.text.slice(0, 160)}${assignSeg.text.length > 160 ? "…" : ""}»`;
  const opts = (S.current.speakers || []).map((sp) => `<label class="check"><input type="radio" name="assignTo" value="${esc(sp.label)}"${sp.label === assignSeg.label ? " checked" : ""}>
      ${esc(sp.display)} <span class="muted small">· ${Math.round(sp.duration)} с речи</span></label>`).join("");
  $("assignOptions").innerHTML = opts + `<label class="check"><input type="radio" name="assignTo" value="new"> Новый собеседник</label>`;
  $("assignDialog").showModal();
});

$("assignDialog").addEventListener("close", async () => {
  if ($("assignDialog").returnValue !== "ok") return;
  const to = (document.querySelector('input[name="assignTo"]:checked') || {}).value;
  if (!to || to === assignSeg.label) return;
  try {
    S.current = await api("segment/assign", { method: "POST", body: { id: S.current.id, index: assignSeg.i, to } });
    renderMeeting();
    toast("Готово. Когда поправите все реплики, нажмите «Пересоздать протокол»", 5000);
  } catch (e) {
    toast(e.message);
  }
});

// Облачная модель не составила протокол (перегрузка, лимит, нет связи) — предложить модель полегче или Qwen.
function protocolFallbackHtml(m) {
  if (!/перегруж|лимит|429|50[0234]|Нет связи|timed out|timeout|не ответил/i.test(m.protocol_error || "")) return "";
  const btns = [];
  const backend = m.llm || (S.settings && S.settings.llm_backend) || "";
  if (backend === "gemini") {
    const cur = (S.settings && S.settings.gemini_model) || "";
    const light = GEMINI_LIGHT.find((x) => x !== cur);
    if (light) btns.push(`<button class="ghost" id="staleLight" data-model="${esc(light)}" title="Более лёгкая бесплатная модель Gemini">Составить на ${esc(light)}</button>`);
  }
  const qwen = S.status && S.status.models && S.status.models.llm && S.status.models.llm.status === "ready";
  if (backend && backend !== "local" && qwen) {
    btns.push(`<button class="ghost" id="staleQwen" title="Ничего не уходит в интернет">Составить на Qwen (на ${PC})</button>`);
  }
  return btns.length ? " " + btns.join(" ") : "";
}

async function redoProtocol(llm, geminiModel) {
  try {
    const body = { id: S.current.id, template: S.current.template || undefined,
                   llm: typeof llm === "string" ? llm : undefined };
    if (geminiModel) body.gemini_model = geminiModel;
    await api("reprocess", { method: "POST", body });
    toast("Протокол пересоздаётся — это видно в списке слева");
    pollStatus();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- участники встречи ----------

function contactLinks(p) {
  let html = "";
  if (p.email) html += ` <a class="contact" href="mailto:${esc(p.email)}" title="${esc(p.email)}">✉</a>`;
  if (p.telegram) html += ` <a class="contact" href="https://t.me/${esc(p.telegram)}" title="Telegram @${esc(p.telegram)}">✈</a>`;
  return html;
}

function renderSpeakers() {
  const m = S.current;
  const sps = m.speakers || [];
  const me = (m.participants || "").split(",").map((x) => x.trim()).includes("Я")
    ? '<span class="chip me"><span class="sp-av me">Я</span><span class="chip-label">Я</span></span>' : "";
  if (!sps.length) {
    $("mSpeakers").innerHTML = me;
    return;
  }
  $("mSpeakers").innerHTML = me + sps.map((sp, i) => {
    const title = sp.uncertain
      ? `Похоже на «${sp.name}» (сходство ${sp.score}). Нажмите, чтобы подтвердить или исправить`
      : sp.name ? `${sp.base} · ${Math.round(sp.duration)} с речи`
      : sp.guess ? `Предполагаемое имя по разговору: «${sp.guess_evidence}». Нажмите, чтобы подтвердить или исправить`
      : `${Math.round(sp.duration)} с речи. Нажмите, чтобы указать имя`;
    const cls = sp.uncertain ? " uncertain" : sp.name ? "" : sp.guess ? " guessed" : " unnamed";
    const displayName = sp.name || sp.guess || sp.base;
    const col = speakerColor(displayName);
    const ini = speakerInitials(displayName);
    const label = sp.name ? esc(sp.name) + (sp.uncertain ? ' <b class="q">?</b>' : "")
      : sp.guess ? esc(sp.guess) + '<b class="star">*</b>' : esc(sp.base);
    const key = `speaker/sample?id=${encodeURIComponent(m.id)}&speaker=${encodeURIComponent(sp.label)}`;
    const playing = audioKey === key;
    return `<span class="chip${cls}">
      ${sp.has_sample
        ? `<button class="play${playing ? " playing" : ""}" data-play-speaker="${esc(sp.label)}" data-play-key="${esc(key)}" style="--sp:${col}" title="${playing ? "Остановить" : "Прослушать"}">${playing ? PAUSE_ICO : PLAY_ICO}</button>`
        : `<span class="sp-av" style="--sp:${col}">${esc(ini)}</span>`}
      <button class="chip-name" data-rename="${i}" title="${esc(title)}">${label}</button>${contactLinks(sp)}</span>`;
  }).join("");
}

// Ссылки на почту и Telegram рядом с именами в таблице задач.
function linkContacts(root) {
  const people = (S.current.speakers || []).filter((p) => p.name && (p.email || p.telegram));
  if (!people.length) return;
  root.querySelectorAll("table").forEach((t) => {
    const head = [...t.querySelectorAll("thead th")].map((th) => th.textContent.toLowerCase());
    const col = head.findIndex((h) => h.includes("ответствен") || h.includes("кто") || h.includes("участник"));
    t.querySelectorAll("tbody tr").forEach((tr) => {
      const cells = col >= 0 ? [tr.children[col]] : [...tr.children];
      for (const td of cells) {
        if (!td) continue;
        const text = td.textContent;
        const found = people.filter((p) => text.includes(p.name));
        if (found.length) td.insertAdjacentHTML("beforeend", found.map(contactLinks).join(""));
      }
    });
  });
}

$("mSpeakers").addEventListener("click", (e) => {
  const play = e.target.closest("[data-play-speaker]");
  if (play) {
    playAudio(`speaker/sample?id=${encodeURIComponent(S.current.id)}&speaker=${encodeURIComponent(play.dataset.playSpeaker)}`, play);
    return;
  }
  const b = e.target.closest("[data-rename]");
  if (b) openRename(S.current.speakers[+b.dataset.rename]);
});

// Вторичные кнопки диалогов (type="button" с value="cancel"|"reset"|"clear") не перехватывают Enter,
// но по клику закрывают <dialog> с нужным returnValue.
document.addEventListener("click", (e) => {
  const btn = e.target.closest('dialog form[method="dialog"] button[type="button"][value]');
  if (btn) btn.closest("dialog").close(btn.value);
});

let renameTarget = null;
async function openRename(sp) {
  renameTarget = sp;
  await loadVoices();
  const names = [...new Set([...(S.current.expected || []), ...(S.voices.voices || []).map((v) => v.name)].filter(Boolean))];
  $("voiceNames").innerHTML = names.map((n) => `<option value="${esc(n)}">`).join("");
  $("renameName").value = sp.name || sp.guess || "";
  $("renameHint").textContent = sp.uncertain
    ? `Голос похож на «${sp.name}», но не наверняка (сходство ${sp.score}). Подтвердите имя или исправьте его.`
    : !sp.name && sp.guess ? `Имя «${sp.guess}» угадано по разговору: «${sp.guess_evidence}». Подтвердите или исправьте.`
    : sp.auto && sp.name ? `Имя подставлено автоматически по голосу (сходство ${sp.score}).`
    : `${sp.base} · ${Math.round(sp.duration)} с речи на этой встрече.`;
  const can = sp.has_embedding && S.voices.available;
  $("renameRemember").checked = can;
  $("renameRemember").disabled = !can;
  $("renameRememberHint").textContent = !S.voices.available ? S.voices.reason
    : !sp.has_embedding ? "Для этого собеседника нет слепка голоса — встречу нужно обработать заново с разделением собеседников."
    : sp.duration < (S.settings.voices_thresholds || {}).min_speech_seconds
      ? `Он говорил всего ${Math.round(sp.duration)} с — слепок может получиться неточным.` : "";
  $("renameReset").hidden = !sp.name && !sp.guess;
  $("renameDialog").showModal();
  $("renameName").focus();
}

$("renameDialog").addEventListener("close", async () => {
  const v = $("renameDialog").returnValue;
  if (v !== "ok" && v !== "reset") return;
  const name = v === "reset" ? "" : $("renameName").value.trim();
  if (v === "ok" && !name) return;
  try {
    S.current = await api("speaker/rename", { method: "POST", body: {
      id: S.current.id, speaker: renameTarget.label, name, remember: v === "ok" && $("renameRemember").checked,
    } });
    renderMeeting();
    loadVoices(true);
    toast(name ? `Сохранено: ${name}` : "Имя сброшено");
  } catch (e) {
    toast(e.message);
  }
});

// ---------- прослушивание образцов ----------

let audioEl = null, audioKey = null, audioGen = 0;

function syncPlayButtons() {
  document.querySelectorAll(".play[data-play-key]").forEach((b) => {
    const on = !!audioKey && b.dataset.playKey === audioKey;
    b.classList.toggle("playing", on);
    b.innerHTML = on ? PAUSE_ICO : PLAY_ICO;
    b.title = on ? "Остановить" : "Прослушать";
  });
}

function stopAudio() {
  audioGen++;
  if (audioEl) {
    try { audioEl.pause(); } catch {}
    if (audioEl.src) URL.revokeObjectURL(audioEl.src);
  }
  audioEl = null;
  audioKey = null;
  syncPlayButtons();
}

async function playAudio(path, btn) {
  const same = audioKey === path;
  stopAudio();
  if (same) return;  // повторное нажатие — пауза/стоп
  const gen = ++audioGen;
  audioKey = path;
  if (btn && !btn.dataset.playKey) btn.dataset.playKey = path;
  syncPlayButtons();
  try {
    const r = await fetch("/api/" + path, { headers: { "X-Token": TOKEN } });
    if (gen !== audioGen) return;  // пользователь уже нажал стоп или другой образец
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Образец не найден");
    const blob = await r.blob();
    if (gen !== audioGen) return;
    const el = new Audio(URL.createObjectURL(blob));
    audioEl = el;
    el.onended = () => { if (audioEl === el) stopAudio(); };
    el.onerror = () => { if (audioEl === el) stopAudio(); };
    await el.play();
  } catch (e) {
    if (gen === audioGen) {
      stopAudio();
      toast(e.message);
    }
  }
}

$("sampleDialog").addEventListener("close", stopAudio);

// ---------- раздел «Голоса» ----------

async function loadVoices(force = false) {
  if (S.voices && !force) return S.voices;
  try {
    S.voices = await api("voices");
  } catch (e) {
    S.voices = { available: false, reason: e.message, voices: [] };
  }
  if (S.view === "voices") renderVoices();
  return S.voices;
}

function renderVoices() {
  const v = S.voices;
  const header = `<div class="st-top"><div><h1>Голоса</h1>
    <div class="st-desc" style="margin-top:4px">Meeting Agent запоминает собеседников по голосу и на следующих встречах сам подписывает их по имени. Чтобы добавить человека, откройте встречу и нажмите на «Собеседник N» над протоколом.</div></div></div>`;
  const vdir = (S.settings || {}).notes_dir ? S.settings.notes_dir.replace(/notes$/, "voices") : "~/MeetingAgent/voices";
  const privacy = `<div class="st-legal" style="margin-top:20px">${ST_ICONS.shield}<span><b>Локальное хранение биометрии:</b> слепки голоса хранятся только на ${PC} (${esc(vdir)}) и никуда не отправляются. Предупреждайте коллег о записи встреч.</span></div>`;
  if (!v.available) {
    $("voices").innerHTML = `<div class="st-page">${header}
      ${stSection("Пока недоступно", `<div class="st-block">
        <div class="st-desc">${esc(v.reason)}</div>
        <button class="st-btn" type="button" id="voicesToSettings" style="margin-top:10px">Открыть настройки</button>
      </div>`)}${privacy}</div>`;
    $("voicesToSettings").onclick = () => { settingsTab = "speech"; $("settingsBtn").click(); };
    return;
  }
  const rows = v.voices.map((p) => {
    const col = speakerColor(p.name);
    const ini = speakerInitials(p.name);
    const key = `voices/sample?id=${p.id}`;
    const playing = audioKey === key;
    return `<div class="st-block voice-row voice" data-id="${p.id}">
      <div class="voice-head">
        <div class="voice-av-wrap">
          <div class="sp-av big" style="--sp:${col}">${esc(ini)}</div>
          ${p.has_sample
            ? `<button class="play mini-over${playing ? " playing" : ""}" data-play-voice="${p.id}" data-play-key="${esc(key)}" title="${playing ? "Остановить" : "Прослушать образец"}">${playing ? PAUSE_ICO : PLAY_ICO}</button>`
            : ""}
        </div>
        <div class="voice-fields">
          <div class="voice-top-line">
            <input class="st-input voice-name" type="text" data-f="name" value="${esc(p.name)}" placeholder="Имя" aria-label="Имя">
            <span class="voice-cnt" title="На скольких встречах учтён голос">${p.meetings} встр.</span>
            <button class="st-btn" type="button" data-save="${p.id}">Сохранить</button>
          </div>
          <div class="voice-contacts">
            <input class="st-input" type="text" data-f="email" value="${esc(p.email)}" placeholder="почта@компания.ru" aria-label="Почта">
            <input class="st-input" type="text" data-f="telegram" value="${esc(p.telegram)}" placeholder="@telegram" aria-label="Telegram">
          </div>
          <div class="voice-foot">
            <button class="link small" type="button" data-sample="${p.id}">Заменить образец</button>
            <button class="link small" type="button" data-merge="${p.id}"${v.voices.length < 2 ? " disabled" : ""}>Объединить с…</button>
            <span class="spacer"></span>
            <button class="link small danger" type="button" data-del="${p.id}">Удалить</button>
          </div>
        </div>
      </div>
    </div>`;
  }).join("");
  $("voices").innerHTML = `<div class="st-page">${header}
    ${stSection(`Сохранённые голоса`, rows || '<div class="st-block"><div class="st-desc">Слепков пока нет — переименуйте любого собеседника в карточке встречи.</div></div>',
      v.voices.length ? `<span class="muted small">${v.voices.length}</span>` : "")}
    ${privacy}</div>`;
}

$("voicesBtn").addEventListener("click", async () => {
  show("voices");
  $("voices").innerHTML = '<div class="st-page"><div class="st-top"><h1>Голоса</h1></div><p class="muted"><span class="spinner"></span> Загружаю…</p></div>';
  await loadVoices(true);
  renderVoices();
});

const voiceById = (id) => S.voices.voices.find((x) => x.id === id);

$("voices").addEventListener("click", async (e) => {
  const playBtn = e.target.closest("[data-play-voice]");
  if (playBtn) return playAudio(`voices/sample?id=${playBtn.dataset.playVoice}`, playBtn);
  const saveBtn = e.target.closest("[data-save]");
  if (saveBtn) {
    const card = saveBtn.closest(".voice");
    const body = { id: saveBtn.dataset.save };
    card.querySelectorAll("[data-f]").forEach((i) => (body[i.dataset.f] = i.value));
    try {
      await api("voices/update", { method: "POST", body });
      toast("Сохранено");
      await loadVoices(true);
      if (S.current) openMeeting(S.current.id).then(() => show("voices"));
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  const sampleBtn = e.target.closest("[data-sample]");
  if (sampleBtn) return openSampleDialog(voiceById(sampleBtn.dataset.sample));
  const mergeBtn = e.target.closest("[data-merge]");
  if (mergeBtn) {
    const p = voiceById(mergeBtn.dataset.merge);
    $("mergeText").textContent = `Эмбеддинги и встречи слепка «${p.name}» перейдут в выбранный слепок, а «${p.name}» будет удалён. Используйте, если один человек оказался записан под двумя именами.`;
    $("mergeInto").innerHTML = S.voices.voices.filter((x) => x.id !== p.id)
      .map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
    $("mergeDialog").dataset.id = p.id;
    $("mergeDialog").showModal();
    return;
  }
  const delBtn = e.target.closest("[data-del]");
  if (delBtn) {
    const p = voiceById(delBtn.dataset.del);
    $("voiceDeleteText").textContent = `Слепок «${p.name}» и его образец будут удалены. В уже готовых протоколах имя останется, но на новых встречах этот голос узнаваться не будет.`;
    $("voiceDeleteDialog").dataset.id = p.id;
    $("voiceDeleteDialog").showModal();
  }
});

$("mergeDialog").addEventListener("close", async () => {
  if ($("mergeDialog").returnValue !== "ok") return;
  try {
    const r = await api("voices/merge", { method: "POST", body: { id: $("mergeDialog").dataset.id, into: $("mergeInto").value } });
    toast(`Объединено в «${r.name}»`);
    await loadVoices(true);
  } catch (e) {
    toast(e.message);
  }
});

$("voiceDeleteDialog").addEventListener("close", async () => {
  if ($("voiceDeleteDialog").returnValue !== "ok") return;
  try {
    await api("voices?id=" + $("voiceDeleteDialog").dataset.id, { method: "DELETE" });
    toast("Слепок удалён");
    await loadVoices(true);
  } catch (e) {
    toast(e.message);
  }
});

async function openSampleDialog(p) {
  $("sampleWho").textContent = p.name;
  $("fragmentList").innerHTML = '<p class="muted"><span class="spinner"></span> Загружаю…</p>';
  $("sampleDialog").dataset.id = p.id;
  $("sampleDialog").showModal();
  try {
    const frs = await api("voices/fragments");
    frs.sort((a, b) => (b.voice_id === p.id) - (a.voice_id === p.id));
    const titles = Object.fromEntries(S.meetings.map((m) => [m.id, m]));
    $("fragmentList").innerHTML = frs.map((f, i) => {
      const note = titles[f.note];
      const when = note ? `${dayLabel(parseDate(note.date))}, ${hhmm(parseDate(note.date))} — ${note.title}` : f.meeting;
      const key = `voices/fragment?meeting=${encodeURIComponent(f.meeting)}&speaker=${encodeURIComponent(f.speaker)}`;
      const playing = audioKey === key;
      return `<div class="fragment${f.voice_id === p.id ? " own" : ""}">
        <button class="play${playing ? " playing" : ""}" data-frag="${i}" data-play-key="${esc(key)}" title="${playing ? "Остановить" : "Прослушать"}">${playing ? PAUSE_ICO : PLAY_ICO}</button>
        <div><div>${esc(f.display)}${f.voice_id === p.id ? ' <span class="badge ok">этот голос</span>' : ""}</div>
        <div class="muted small">${esc(when)} · ${Math.round(f.duration)} с речи</div></div>
        <button class="ghost" data-pick="${i}">Выбрать</button></div>`;
    }).join("") || '<p class="muted">Нет встреч с образцами голоса. Они появляются у встреч, обработанных с разделением собеседников.</p>';
    $("fragmentList").onclick = async (e) => {
      const fragBtn = e.target.closest("[data-frag]");
      if (fragBtn) {
        const f = frs[+fragBtn.dataset.frag];
        if (f) return playAudio(`voices/fragment?meeting=${encodeURIComponent(f.meeting)}&speaker=${encodeURIComponent(f.speaker)}`, fragBtn);
        return;
      }
      const pickBtn = e.target.closest("[data-pick]");
      const f = pickBtn ? frs[+pickBtn.dataset.pick] : null;
      if (!f) return;
      try {
        await api("voices/set_sample", { method: "POST", body: { id: p.id, meeting: f.meeting, speaker: f.speaker } });
        $("sampleDialog").close();
        toast("Образец заменён");
        await loadVoices(true);
      } catch (err) {
        toast(err.message);
      }
    };
  } catch (e) {
    $("fragmentList").textContent = e.message;
  }
}
$("sampleDialog").addEventListener("close", stopAudio);

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => { S.tab = t.dataset.tab; renderMeeting(); }));

$("copyBtn").addEventListener("click", async () => {
  const m = S.current;
  const last = (MCHAT.log[m.id] || []).filter((h) => h.a).pop();
  const v = S.tab === "summary" && currentVersion(m);
  const text = S.tab === "chat" ? (last ? last.a : "") : v ? `# ${v.title}\n\n${v.text}`
    : `# ${m.title}\n\n` + (S.tab === "summary" ? stripTimes(m.summary) : m.transcript);
  try {
    await navigator.clipboard.writeText(text);
    toast("Скопировано");
  } catch (_) {
    toast("Не удалось скопировать");
  }
});

$("revealBtn").addEventListener("click", () => api("reveal", { method: "POST", body: { id: S.current.id } }).catch((e) => toast(e.message)));

$("redoBtn").addEventListener("click", () => {
  const s = S.settings;
  $("redoTemplate").innerHTML = s.templates.map((t) =>
    `<option value="${esc(t.id)}"${t.id === (S.current.template || s.template) ? " selected" : ""}>${esc(t.title)}</option>`).join("");
  $("redoLlm").innerHTML = Object.entries(s.backends).map(([k, v]) =>
    `<option value="${k}"${k === s.llm_backend ? " selected" : ""}>${esc(v)}</option>`).join("");
  $("redoRetranscribe").checked = false;
  $("redoAsr").innerHTML = Object.entries(s.asr_engines).map(([k, e]) =>
    `<option value="${k}"${k === s.asr_engine ? " selected" : ""}${e.ready ? "" : " disabled"}>${esc(e.title)}${e.ready ? "" : " — не скачана"}</option>`).join("");
  $("redoAsrRow").hidden = true;
  $("redoRetranscribe").onchange = (e) => ($("redoAsrRow").hidden = !e.target.checked);
  $("redoDialog").showModal();
});

$("redoDialog").addEventListener("close", async () => {
  if ($("redoDialog").returnValue !== "ok") return;
  try {
    await api("reprocess", { method: "POST", body: {
      id: S.current.id, template: $("redoTemplate").value, llm: $("redoLlm").value,
      retranscribe: $("redoRetranscribe").checked,
      asr: $("redoRetranscribe").checked ? $("redoAsr").value : undefined,
    } });
    toast("Протокол пересоздаётся — это видно в списке слева");
    pollStatus();
  } catch (e) {
    toast(e.message);
  }
});

$("deleteBtn").addEventListener("click", () => {
  $("deleteText").textContent = `«${S.current.title}» — протокол и расшифровка будут удалены.`;
  $("deleteDialog").showModal();
});

$("deleteDialog").addEventListener("close", async () => {
  if ($("deleteDialog").returnValue !== "ok") return;
  try {
    await api(`meeting?id=${encodeURIComponent(S.current.id)}&audio=${$("deleteAudio").checked ? 1 : 0}`, { method: "DELETE" });
    S.current = null;
    await loadMeetings();
    show("meeting");
    toast("Встреча удалена");
  } catch (e) {
    toast(e.message);
  }
});

// ---------- запись ----------

$("recordBtn").addEventListener("click", () => {
  const rec = S.status && S.status.recording;
  native(rec && rec.state === "recording" ? "stopRecording" : "startRecording");
  setTimeout(pollStatus, 600);
});
if ($("micOnlyBtn")) $("micOnlyBtn").addEventListener("click", () => {
  native("startMicOnly");
  setTimeout(pollStatus, 600);
});
$("recStop").addEventListener("click", () => { native("stopRecording"); setTimeout(pollStatus, 600); });
if ($("recPause")) $("recPause").addEventListener("click", () => { native("togglePause"); setTimeout(pollStatus, 400); });

function renderRecording() {
  const rec = (S.status && S.status.recording) || {};
  const on = rec.state === "recording";
  const paused = !!(on && rec.paused);
  if ($("recHero")) $("recHero").hidden = on;
  $("recordBtn").classList.toggle("recording", on);
  $("recordBtn").querySelector(".label").textContent = on ? "Стоп" : "Начать запись";
  if ($("micOnlyBtn")) $("micOnlyBtn").hidden = on;
  $("recBanner").hidden = !on;
  $("recBanner").classList.toggle("paused", paused);
  if ($("recPause")) {
    $("recPause").textContent = paused ? "Продолжить" : "Пауза";
  }
  if (rec.skip_meeting && !S.skipToastShown) toast("Запись остановлена. Эту встречу приложение больше не запишет — автозапись включится на следующей.", 7000);
  S.skipToastShown = !!rec.skip_meeting;
  if ($("nextPeople").hidden !== on) renderNextPeople();  // запись началась или закончилась
  if (on) {
    const secs = rec.since ? Math.max(0, Math.floor(Date.now() / 1000 - rec.since - (rec.paused_seconds || 0))) : 0;
    const t = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    const PLAT = { zoom: "Zoom", teams: "Teams", telemost: "Телемоста", meet: "в браузере",
      slack: "Slack", discord: "Discord", webex: "Webex", telegram: "Telegram", whatsapp: "WhatsApp",
      jazz: "SaluteJazz", trueconf: "TrueConf", ktalk: "Контур.Толка", vkcalls: "VK Звонков", dion: "DION",
      mic_only: "только микрофон" };
    const head = paused ? "Пауза" : (rec.source === "mic_only" ? "Запись (только микрофон)"
      : (PLAT[rec.source] ? "Запись встречи " + PLAT[rec.source] : "Идёт запись"));
    $("recText").textContent = `${head} · ${t}`;
  }
  $("recWarn").hidden = !(on && rec.warning);
  $("recWarn").textContent = on && rec.warning ? "⚠ " + rec.warning : "";
  renderPermissions();
}

// Разрешения macOS: чего не хватает — и кнопка в нужный раздел Системных настроек.
function renderPermissions() {
  const p = (S.status && S.status.permissions) || {};
  const miss = [];
  if (p.mic === "denied") miss.push(["Микрофон", "без него не записывается ваш голос", "Privacy_Microphone", "Открыть настройки"]);
  if (p.screen === "granted_needs_restart") {
    miss.push(["Запись экрана выдана", "перезапустите приложение, чтобы разрешение заработало", "__restart__", "Перезапустить"]);
  } else if (p.screen === "denied") {
    const why = p.process_tap
      ? "нужно для определения заголовков окон Zoom (системный звук пишется отдельно)"
      : "без него не слышно собеседников";
    miss.push(["Запись экрана и системного звука", why, "Privacy_ScreenCapture", "Открыть настройки"]);
  }
  if (p.calendar === "denied") miss.push(["Календари", "без него не подтянутся участники встречи", "Privacy_Calendars", "Открыть настройки"]);
  $("permBanner").hidden = !miss.length;
  $("permBanner").innerHTML = miss.length ? `<b>Разрешения macOS</b>${miss.map(([t, why, pane, btn]) =>
    `<div class="perm-row"><span>${esc(t)} <span class="muted">— ${esc(why)}</span></span>
     <button class="link small" type="button" data-pane="${pane}">${esc(btn)}</button></div>`).join("")}` : "";
}
$("permBanner").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pane]");
  if (!b) return;
  if (b.dataset.pane === "__restart__") { native("restartApp"); return; }
  native("openPrivacy", { pane: b.dataset.pane });
  toast("Включите Meeting Agent в открывшемся разделе — приложение само проверит разрешение", 7000);
});

// ---------- очередь обработки ----------

function renderJobs() {
  const jobs = (S.status && S.status.jobs) || [];
  $("jobs").innerHTML = jobs.filter((j) => j.status !== "done").map((j) => {
    const name = j.folder.split("/").pop();
    if (j.status === "error") {
      return `<div class="job error"><div class="j-title">Не удалось обработать</div>
        <div class="j-sub">${esc(j.error)}</div>
        <div class="j-actions"><button class="link" data-retry="${j.id}">Повторить</button>
        <button class="link" data-dismiss="${j.id}">Скрыть</button></div></div>`;
    }
    const stage = j.status === "queued" ? "В очереди" : (j.stage_title || "Подготовка") + "…";
    return `<div class="job"><div class="j-title"><span class="spinner"></span>Обработка записи</div>
      <div class="j-sub">${esc(stage)} · ${esc(name)}</div></div>`;
  }).join("");

  // Готовые задачи: обновить список и открыть новую заметку.
  for (const j of jobs) {
    if (j.status === "done" && !S.doneJobs.has(j.id)) {
      S.doneJobs.add(j.id);
      loadMeetings().then(() => {
        if (j.note && (S.view !== "meeting" || !S.current || S.current.source === j.folder || !document.hasFocus())) {
          openMeeting(j.note);
        }
      });
    }
  }
}

$("jobs").addEventListener("click", async (e) => {
  const id = e.target.dataset.retry || e.target.dataset.dismiss;
  if (!id) return;
  await api(e.target.dataset.retry ? "jobs/retry" : "jobs/dismiss", { method: "POST", body: { id } }).catch((err) => toast(err.message));
  pollStatus();
});

// ---------- импорт ----------

async function importFile(file) {
  if (!file) return;
  toast(`Загружаю «${file.name}»…`, 60000);
  try {
    await api("import", { method: "POST", body: file, headers: {
      "X-Filename": encodeURIComponent(file.name), "X-Mtime": String(file.lastModified || 0),
      "Content-Type": "application/octet-stream",
    } });
    toast("Файл принят — расшифровка началась");
    pollStatus();
  } catch (e) {
    toast(e.message);
  }
}

$("importBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => { importFile(e.target.files[0]); e.target.value = ""; });

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("drop").hidden = false; });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("drop").hidden = true;
  importFile(e.dataTransfer.files[0]);
});

// ---------- модели ----------

function fmtSpeed(bps) {
  return bps >= 1e6 ? (bps / 1e6).toFixed(1).replace(".", ",") + " МБ/с" : Math.round(bps / 1e3) + " КБ/с";
}

function fmtEta(sec) {
  if (!isFinite(sec) || sec <= 0) return "";
  if (sec < 90) return "меньше 2 мин";
  if (sec < 3600) return `≈ ${Math.round(sec / 60)} мин`;
  return `≈ ${Math.floor(sec / 3600)} ч ${Math.round(sec % 3600 / 60)} мин`;
}

// Состояние загрузки модели: полоса, «скачано из», скорость, пауза и продолжение.
function downloadWidget(name, m, sizeHint) {
  const pct = m.total ? Math.min(100, (100 * m.done) / m.total) : 0;
  const bar = `<div class="bar"><div style="width:${pct}%"></div></div>`;
  if (m.status === "downloading") {
    // Первые секунды идут мелкие служебные файлы — скорость по ним бессмысленна.
    const speed = m.speed > 1e5 ? ` · ${fmtSpeed(m.speed)} · ${fmtEta((m.total - m.done) / m.speed)}` : " · начинаю…";
    return `<div class="dl">${bar}<div class="dl-row"><span class="muted small">${fmtBytes(m.done)} из ${fmtBytes(m.total)}${speed}</span>
      ${m.pausable ? `<button class="ghost small" type="button" data-pause="${name}">Пауза</button>` : '<span class="spinner"></span>'}</div></div>`;
  }
  if (m.status === "paused") {
    return `<div class="dl">${bar}<div class="dl-row"><span class="muted small">Пауза: ${fmtBytes(m.done)} из ${fmtBytes(m.total)}</span>
      <button class="ghost small" type="button" data-download="${name}">Продолжить</button></div></div>`;
  }
  if (m.status === "error") {
    return `<div class="dl"><div class="dl-row"><span class="small" style="color:var(--rec)">${esc(m.error)}</span>
      <button class="ghost small" type="button" data-download="${name}">Повторить</button></div></div>`;
  }
  return `<button class="ghost" type="button" data-download="${name}">Скачать ≈ ${fmtBytes(sizeHint || m.total)}</button>`;
}

function modelsHtml(models, { onlyRequired = false, except = [] } = {}) {
  return Object.entries(models).filter(([name, m]) => (!onlyRequired || m.required) && !except.includes(name)).map(([name, m]) => {
    const ready = m.status === "ready";
    return `<div class="model"><div class="m-main"><div class="m-name">${esc(m.title)}${m.required ? "" : ' <span class="muted">— не нужна при текущих настройках</span>'}</div>
      ${ready ? "" : downloadWidget(name, m)}</div><div>${ready ? '<span class="badge ok">Готово</span>' : ""}</div></div>`;
  }).join("");
}

document.addEventListener("click", async (e) => {
  const d = e.target.dataset || {};
  const name = d.download || d.pause;
  if (!name) return;
  e.preventDefault();  // кнопка внутри <label> варианта — не переключать радиокнопку
  e.target.disabled = true;
  try {
    await api(d.pause ? "models/pause" : "models/download", { method: "POST", body: { name } });
    pollStatus();
  } catch (err) {
    toast(err.message);
    e.target.disabled = false;
  }
});

// ---------- первый запуск ----------

function renderSetup() {
  const st = S.status, s = S.settings;
  if (!st || !s) return;
  const ready = !st.setup_needed;
  // Распознавание и pyannote — в своих шагах, здесь — остальные (Qwen).
  const other = modelsHtml(st.models, { onlyRequired: true, except: ["whisper", "diarization"] });
  if (s.hf_token_set && !S.hfCheck) checkHf();  // токен уже есть (например, из прошлой установки)
  // Подбор под компьютер — только на Windows: Mac у нас всегда Apple Silicon, выбор по умолчанию подходит.
  if (IS_WIN && S.hardware === undefined) { S.hardware = null; loadHardware(); }
  if (IS_WIN && S.gpu === undefined) { S.gpu = null; loadGpu(); }
  const nvidiaStep = IS_WIN && S.gpu && S.gpu.nvidia;
  let n = 0;
  const step = () => ++n;
  const html = `<div class="page">
    <h1>Добро пожаловать в Meeting Agent</h1>
    <p class="muted">Для работы нужно один раз скачать модели. Записи и расшифровки остаются на ${PC}.</p>
    ${hardwareHtml(s)}
    <div class="card"><h2>${step()}. Кто пишет протоколы</h2>
      <div class="hint">Звук в любом случае расшифровывается на ${PC}. Выбор можно поменять в настройках.</div>
      ${backendRadios(s)}</div>
    <div class="card"><h2>${step()}. Распознавание речи</h2>
      <div class="hint">Выберите модель и нажмите «Скачать». Загрузку можно прервать: при следующем запуске она продолжится с того же места. Модель можно сменить в настройках.</div>
      ${asrRadios(s)}</div>
    ${nvidiaStep ? gpuCardHtml(`${step()}. Ускорение NVIDIA${S.hardware && S.hardware.recommend.gpu_accel ? "" : " — по желанию"}`) : ""}
    ${other ? `<div class="card"><h2>${step()}. Модель для протоколов</h2>${other}</div>` : ""}
    <div class="card"><h2>${step()}. Разделение собеседников — по желанию</h2>${hfSetupHtml(s, st)}</div>
    <div class="card"><h2>${step()}. Разрешения ${IS_WIN ? "Windows" : "macOS"}</h2>
      <div class="hint">${IS_WIN
        ? "Приложение записывает ваш микрофон и весь звук компьютера (в нём собеседники из Zoom). Если запись не начинается, откройте Параметры Windows → Конфиденциальность и защита → Микрофон и включите «Разрешить классическим приложениям доступ к микрофону». Собеседники пишутся с устройства вывода по умолчанию — в Zoom оставьте динамик «Как в системе»."
        : "При первой записи macOS спросит доступ к <b>микрофону</b> и к <b>записи экрана и системного звука</b>. Разрешите оба: без второго приложение не услышит собеседников в Zoom."}</div></div>
    <p><button class="primary" id="setupDone" ${ready ? "" : "disabled"}>${ready ? "Начать работу" : "Дождитесь загрузки моделей"}</button>
    <button class="link" id="setupLater" style="margin-left:12px">Позже</button></p></div>`;
  if (html === S.setupHtml) return;  // без лишней перерисовки: не сбивать клики и прокрутку
  S.setupHtml = html;
  // Перерисовка идёт и от прогресса загрузки — не теряем начатый ввод ключа.
  const typed = [...$("setup").querySelectorAll("[data-key-input]")].map((i) => [i.dataset.keyInput, i.value, i === document.activeElement]);
  $("setup").innerHTML = html;
  for (const [name, value, focused] of typed) {
    const i = $("setup").querySelector(`[data-key-input="${name}"]`);
    if (i) { i.value = value; if (focused) i.focus(); }
  }
  bindBackendRadios($("setup"));
  bindGpu($("gpuCard"));
  $("setupDone").onclick = () => { S.setupDismissed = true; show("meeting"); };
  $("setupLater").onclick = () => { S.setupDismissed = true; show("meeting"); };
}

// --- Первый запуск: какой это компьютер и что ему подходит (hardware.py) ---
async function loadHardware() {
  try {
    S.hardware = await api("hardware");
  } catch {
    return;
  }
  const s = S.settings, r = S.hardware.recommend;
  // Свежая установка (ни одной модели распознавания) — сразу выбираем рекомендованное, один раз.
  const fresh = !Object.values(s.asr_engines).some((e) => e.ready);
  if (!s.recommended_applied && fresh) {
    try {
      S.settings = await api("settings", { method: "POST", body: { llm_backend: r.llm, asr_engine: r.asr, recommended_applied: true } });
      await pollStatus();
    } catch (e) {
      toast(e.message);
    }
  }
  S.setupHtml = null;
  if (S.view === "setup") renderSetup();
}

function hardwareHtml(s) {
  const h = S.hardware;
  if (!h) return "";
  const i = h.info, r = h.recommend;
  const gpu = i.gpu && i.gpu.kind === "nvidia" ? ` · ${esc(i.gpu.name)} (${Math.round(i.gpu.vram_gb)} ГБ)` : "";
  const asr = (s.asr_engines[r.asr] || {}).title || r.asr;
  const llm = s.backends[r.llm] || r.llm;
  return `<div class="card reco"><h2>Ваш компьютер</h2>
    <div class="hint">${esc(i.cpu)} · ${i.cores} ${IS_WIN ? "потоков" : "ядер"} · ${Math.round(i.ram_gb)} ГБ памяти${gpu} · ${esc(i.os)}</div>
    <ul class="steps">
      <li><b>Распознавание: ${esc(asr)}.</b> ${esc(r.notes.asr)}</li>
      <li><b>Протоколы: ${esc(llm)}.</b> ${esc(r.notes.llm)}</li>
      ${r.notes.gpu ? `<li><b>Ускорение NVIDIA${r.gpu_accel ? "" : " — не обязательно"}.</b> ${esc(r.notes.gpu)}</li>` : ""}
    </ul>
    <div class="hint">Рекомендованные варианты отмечены ниже${s.recommended_applied ? " и уже выбраны" : ""} — их можно поменять.</div></div>`;
}

function recoBadge(kind, key) {
  const r = S.hardware && S.hardware.recommend;
  return r && r[kind] === key ? ' <span class="badge ok">рекомендуется</span>' : "";
}

function hfSetupHtml(s, st) {
  const m = st.models.diarization || {};
  const link = (repo) => `<a href="https://huggingface.co/${repo}" target="_blank">${repo.split("/")[1]}</a>`;
  let tail = "";
  if (m.status === "ready") tail = '<span class="badge ok">Модель скачана — собеседники будут подписаны</span>';
  else if (s.hf_token_set) {
    tail = `<div class="row"><button class="ghost" type="button" data-check-hf>Проверить токен</button>
      <span class="small">${hfCheckHtml()}</span></div>`;
    if (S.hfCheck && S.hfCheck.ok) tail += downloadWidget("diarization", m);
  }
  return `<div class="hint">Подписывает собеседников «Собеседник 1», «Собеседник 2»… и узнаёт их по голосу на следующих встречах.
      Без этого вся речь собеседников будет под одной подписью. Нужен бесплатный аккаунт Hugging Face:</div>
    <ol class="steps">
      <li>Зарегистрируйтесь на <a href="https://huggingface.co/join" target="_blank">huggingface.co</a>.</li>
      <li>Откройте страницы двух моделей и примите условия (кнопка «Agree and access repository»):
        ${link("pyannote/speaker-diarization-3.1")} и ${link("pyannote/segmentation-3.0")}.</li>
      <li>Создайте токен на <a href="https://huggingface.co/settings/tokens" target="_blank">huggingface.co/settings/tokens</a>:
        «Create new token», тип <b>Read</b>, любое имя. Скопируйте его (начинается с hf_).</li>
      <li>Вставьте токен сюда, сохраните и нажмите «Скачать модель» (30 МБ).</li>
    </ol>
    <div class="field"><label>Токен Hugging Face ${s.hf_token_set ? '<span class="badge ok">сохранён</span>' : ""}</label>
      <div class="row"><input type="password" data-key-input="hf_token" placeholder="${s.hf_token_set ? "•••••• — чтобы заменить, вставьте новый" : "hf_…"}" autocomplete="off">
      <button class="ghost" type="button" data-key-save="hf_token">Сохранить</button></div></div>
    ${tail}`;
}

function backendRadios(s) {
  const desc = {
    "local": `Бесплатно, ничего не уходит в интернет. Нужна модель Qwen (~${IS_WIN ? "4,7" : "4,3"} ГБ). Протокол часовой встречи — около 10 минут на Mac M1 Pro, пара минут на мощной видеокарте NVIDIA, 20–50 минут на процессоре.`,
    "claude-code": s.claude_code_path
      ? "Без доплаты, расходует лимиты вашей подписки Claude. Протоколы точнее."
      : "Нужен установленный Claude Code с входом по подписке — сейчас он не найден.",
    "claude": s.anthropic_key_set ? "Оплата за запросы, примерно 0,2–0,4 $ за часовую встречу." : "Нужен платный ключ API — выберите этот вариант, и под ним появится поле для ключа.",
    "gemini": s.gemini_key_set ? `Модель ${s.gemini_model}. Модели Gemini 3.x Flash и Flash-Lite доступны бесплатно — нужен только ключ.`
      : "Нужен ключ Gemini API — выберите этот вариант, и под ним появится поле для ключа.",
    "openai": s.openai_key_set ? `Модель ${s.openai_model}, оплата за запросы по тарифу OpenAI (отдельно от подписки ChatGPT Plus).`
      : "Нужен платный ключ OpenAI API — выберите этот вариант, и под ним появится поле для ключа.",
  };
  const cloud = { "claude-code": "Anthropic", "claude": "Anthropic", "gemini": "Google", "openai": "OpenAI" };
  return Object.entries(s.backends).map(([k, v]) => `<label class="radio${k === s.llm_backend ? " selected" : ""}">
    <input type="radio" name="backend" value="${k}"${k === s.llm_backend ? " checked" : ""}>
    <div><div class="r-title">${esc(v)}${S.view === "setup" ? recoBadge("llm", k) : ""}</div><div class="r-desc">${esc(desc[k] || "")}</div>
    ${cloud[k] ? `<div class="cloud-note">Текст расшифровки отправляется в облако ${cloud[k]}. Уточните, разрешено ли это для рабочих встреч.</div>` : ""}</div></label>
    ${k === s.llm_backend ? backendExtra(s, k) : ""}`).join("");
}

// Ключи облачных вариантов: вводятся прямо под выбранным вариантом (и при первом запуске).
const BACKEND_KEYS = {
  "claude": { field: "anthropic_key", set: "anthropic_key_set", title: "Ключ Claude API", placeholder: "sk-ant-…",
              url: "https://platform.claude.com/settings/keys" },
  "gemini": { field: "gemini_key", set: "gemini_key_set", title: "Ключ Gemini API", placeholder: "AIza…",
              url: "https://aistudio.google.com/apikey" },
  "openai": { field: "openai_key", set: "openai_key_set", title: "Ключ OpenAI API", placeholder: "sk-…",
              url: "https://platform.openai.com/api-keys" },
};
// Модели Gemini: список приходит от Google по ключу (S.geminiModels), пока его нет — основные.
// Бесплатный уровень Gemini API (ноябрь 2026). Старее 3.1 не показываем — сервер их тоже не берёт.
const GEMINI_PRESETS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"];

// --- Проверка ключей API: крошечный пробный запрос к сервису (summarize.check_key) ---
const KEY_SERVICE = { anthropic_key: "claude", gemini_key: "gemini", openai_key: "openai" };
S.keyCheck = {};
async function checkKey(service) {
  S.keyCheck[service] = { loading: true };
  repaintKeys();
  try {
    S.keyCheck[service] = await api("check_key", { method: "POST", body: { service } });
  } catch (e) {
    S.keyCheck[service] = { ok: false, message: e.message };
  }
  repaintKeys();
  // Gemini: ключ и модель на месте — вторым шагом пробная генерация (регион, лимиты); бывает долгой.
  if (service === "gemini" && S.keyCheck.gemini.ok) {
    const quick = S.keyCheck.gemini;
    S.keyCheck.gemini = { ...quick, generating: true };
    repaintKeys();
    let full;
    try {
      full = await api("check_key", { method: "POST", body: { service, full: true } });
    } catch (e) {
      full = { ok: false, message: e.message };
    }
    S.keyCheck.gemini = { ok: full.ok, message: `${quick.message}. ${full.message}` };
    repaintKeys();
  }
}
function repaintKeys() {
  document.querySelectorAll("[data-key-result]").forEach((el) => { el.innerHTML = keyResultHtml(el.dataset.keyResult); });
}
function keyResultHtml(service) {
  const r = S.keyCheck[service];
  if (!r) return "";
  if (r.loading) return '<span class="spinner"></span> проверяю ключ…';
  if (r.generating) return `<span class="badge ok">ключ работает</span> ${esc(r.message)}. <span class="spinner"></span> пробую сгенерировать ответ — бывает до минуты…`;
  return `<span class="badge ${r.ok ? "ok" : "err"}">${r.ok ? "работает" : "не работает"}</span> ${esc(r.message)}`;
}
function keyCheckHtml(service, saved) {
  if (!saved) return "";
  return `<div class="row"><button class="ghost small" type="button" data-check-key="${service}">Проверить ключ</button>
    <span class="small" data-key-result="${service}">${keyResultHtml(service)}</span></div>`;
}

function geminiNote(id) {
  if (id === GEMINI_PRESETS[0]) return " — бесплатно, основная";
  if (id.includes("flash-lite") && GEMINI_PRESETS.includes(id)) return " — бесплатно, выше лимиты, проще";
  if (GEMINI_PRESETS.includes(id)) return " — бесплатно";
  if (id.includes("pro")) return " — точнее, обычно только с оплатой";
  return "";
}

function geminiModelField(s) {
  if (S.geminiModels === undefined) loadGeminiModels();
  const list = Array.isArray(S.geminiModels) && S.geminiModels.length ? S.geminiModels
    : GEMINI_PRESETS.map((id) => ({ id, title: id }));
  const ids = list.map((m) => m.id);
  const all = ids.includes(s.gemini_model) ? list : [{ id: s.gemini_model, title: s.gemini_model }, ...list];
  return `<div class="field"><label>Модель Gemini для протоколов</label>
    <select data-gemini-model>${all.map((m) => `<option value="${esc(m.id)}"${m.id === s.gemini_model ? " selected" : ""}>${m.title === m.id ? esc(m.id) : `${esc(m.title)} (${esc(m.id)})`}${geminiNote(m.id)}</option>`).join("")}</select>
    <div class="hint">Без оплаты у ключа есть бесплатный лимит запросов в минуту и в день — на моделях Flash, у Pro его может не быть.
      Актуальные лимиты — <a href="https://ai.google.dev/gemini-api/docs/rate-limits" target="_blank">ai.google.dev/gemini-api/docs/rate-limits</a>.</div>
    <div class="cloud-note">На бесплатном уровне Google может использовать присланные тексты для улучшения своих продуктов
      (<a href="https://ai.google.dev/gemini-api/terms" target="_blank">условия Gemini API</a>). Для рабочих встреч уточните, допустимо ли это.</div>
    <div class="hint">
      ${S.geminiModels === "loading" ? '<span class="spinner"></span> загружаю список моделей…' : ""}
      ${S.geminiModelsError ? `<span class="badge err">${esc(S.geminiModelsError)}</span>` : ""}</div></div>`;
}

async function loadGeminiModels() {
  S.geminiModels = "loading";
  S.geminiModelsError = null;
  try {
    S.geminiModels = await api("gemini/models");
  } catch (e) {
    S.geminiModels = [];
    S.geminiModelsError = e.message;
  }
  if (S.view === "setup") renderSetup();
  else if (S.view === "settings") renderSettings();
}

const CLAUDE_INSTALL = IS_WIN ? "irm https://claude.ai/install.ps1 | iex" : "curl -fsSL https://claude.ai/install.sh | bash";
const SHELL_APP = IS_WIN ? "PowerShell" : "Терминал";

function backendExtra(s, k) {
  const key = BACKEND_KEYS[k];
  if (key) {
    const saved = s[key.set];
    return `<div class="backend-extra">
      <div class="field"><label>${key.title} ${saved ? '<span class="badge ok">сохранён</span>' : ""}</label>
        <div class="hint">Создайте ключ на <a href="${key.url}" target="_blank">${key.url.replace("https://", "")}</a> и вставьте сюда.</div>
        <div class="row"><input type="password" data-key-input="${key.field}" placeholder="${saved ? "•••••• — чтобы заменить, вставьте новый" : key.placeholder}" autocomplete="off">
        <button class="ghost" type="button" data-key-save="${key.field}">Сохранить</button></div>
        ${keyCheckHtml(KEY_SERVICE[key.field], saved)}</div>
      ${k === "gemini" && saved ? geminiModelField(s) : ""}</div>`;
  }
  if (k !== "claude-code") return "";
  if (s.claude_code_path) {
    return `<div class="backend-extra"><div class="hint"><span class="badge ok">Claude Code найден</span> ${esc(s.claude_code_path)}.
      Если протокол не составится из-за входа — откройте ${SHELL_APP}, выполните <code>claude</code> и войдите в аккаунт Claude.</div></div>`;
  }
  return `<div class="backend-extra">
    <div class="r-title">Как подключить Claude по подписке</div>
    <div class="hint">Нужна подписка Claude Pro или Max. Claude Code ставится один раз через ${SHELL_APP}:</div>
    <ol class="steps">
      <li>${IS_WIN ? "Откройте <b>PowerShell</b>: меню «Пуск» → наберите «PowerShell» → Enter."
        : "Откройте <b>Терминал</b>: Программы → Утилиты → Терминал (или ⌘ Пробел, «Терминал»)."}</li>
      <li>Вставьте команду установки и нажмите Enter:
        <div class="cmd"><code>${esc(CLAUDE_INSTALL)}</code><button class="ghost small" type="button" data-copy="${esc(CLAUDE_INSTALL)}">Скопировать</button></div></li>
      <li>Когда установка закончится, закройте окно ${IS_WIN ? "PowerShell" : "Терминала"}, откройте новое и выполните:
        <div class="cmd"><code>claude</code><button class="ghost small" type="button" data-copy="claude">Скопировать</button></div>
        Выберите вход через аккаунт Claude (подписка), войдите в открывшемся браузере, затем закройте Терминал.</li>
      <li>Вернитесь сюда и нажмите <button class="ghost small" type="button" data-recheck-claude>Проверить снова</button></li>
    </ol>
    <div class="hint">Подробнее: <a href="https://code.claude.com/docs/en/setup" target="_blank">code.claude.com/docs/en/setup</a></div></div>`;
}

function asrRadios(s) {
  const m = S.status && S.status.models && S.status.models.whisper;
  return Object.entries(s.asr_engines).map(([k, e]) => {
    const on = k === s.asr_engine;
    const ready = e.ready || (on && m && m.status === "ready");
    const badge = ready ? '<span class="badge ok">скачана</span>'
      : on ? "" : `<span class="badge warn">не скачана · ${fmtBytes(e.size)}</span>`;
    const dl = on && !ready && m ? downloadWidget("whisper", m, e.size) : "";
    return `<label class="radio${on ? " selected" : ""}">
      <input type="radio" name="asr" value="${k}"${on ? " checked" : ""}>
      <div style="flex:1"><div class="r-title">${esc(e.title)} ${badge}${S.view === "setup" ? recoBadge("asr", k) : ""}</div><div class="r-desc">${esc(e.desc)}</div>${dl}</div></label>`;
  }).join("");
}

function bindBackendRadios(root) {
  root.querySelectorAll('input[name="asr"]').forEach((r) => r.addEventListener("change", async () => {
    try {
      S.settings = await api("settings", { method: "POST", body: { asr_engine: r.value } });
      await pollStatus();
      S.view === "setup" ? renderSetup() : renderSettings();
      const e = S.settings.asr_engines[r.value];
      toast(e.ready ? `Распознавание: ${e.title}` : `${e.title}: модель ещё не скачана — нажмите «Скачать»`, 5000);
    } catch (e) {
      toast(e.message);
    }
  }));
  const rerender = () => (S.view === "setup" ? renderSetup() : renderSettings());
  root.querySelectorAll("[data-key-save]").forEach((b) => b.addEventListener("click", async () => {
    const input = root.querySelector(`[data-key-input="${b.dataset.keySave}"]`);
    const v = input.value.trim();
    if (!v) return input.focus();
    try {
      S.settings = await api("settings", { method: "POST", body: { [b.dataset.keySave]: v } });
      input.value = "";
      toast("Ключ сохранён");
      if (b.dataset.keySave === "hf_token") checkHf();
      if (b.dataset.keySave === "gemini_key") S.geminiModels = undefined;  // новый ключ — новый список
      rerender();
      if (KEY_SERVICE[b.dataset.keySave]) checkKey(KEY_SERVICE[b.dataset.keySave]);
    } catch (e) {
      toast(e.message);
    }
  }));
  root.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
      toast(`Скопировано — вставьте в ${SHELL_APP} (${CMD_KEY}V)`);
    } catch {
      getSelection().selectAllChildren(b.previousElementSibling);  // без доступа к буферу — выделяем, ⌘C
      toast(`Нажмите ${CMD_KEY}C, чтобы скопировать`);
    }
  }));
  root.querySelectorAll("[data-gemini-model]").forEach((sel) => sel.addEventListener("change", async () => {
    try {
      S.settings = await api("settings", { method: "POST", body: { gemini_model: sel.value } });
      delete S.keyCheck.gemini;
      toast("Модель Gemini: " + sel.value);
      rerender();
    } catch (e) {
      toast(e.message);
    }
  }));
  root.querySelectorAll("[data-check-hf]").forEach((b) => b.addEventListener("click", checkHf));
  root.querySelectorAll("[data-check-key]").forEach((b) => b.addEventListener("click", () => checkKey(b.dataset.checkKey)));
  root.querySelectorAll("[data-recheck-claude]").forEach((b) => b.addEventListener("click", async () => {
    S.settings = await api("settings").catch(() => S.settings);
    toast(S.settings.claude_code_path ? "Claude Code найден" : "Claude Code пока не найден — проверьте, что установка закончилась без ошибок", 5000);
    rerender();
  }));
  root.querySelectorAll('input[name="backend"]').forEach((r) => r.addEventListener("change", async () => {
    try {
      S.settings = await api("settings", { method: "POST", body: { llm_backend: r.value } });
      await pollStatus();
      S.view === "setup" ? renderSetup() : renderSettings();
    } catch (e) {
      toast(e.message);
    }
  }));
}

// ---------- настройки ----------

// --- Ускорение NVIDIA (Windows): библиотеки CUDA скачиваются по желанию ---
let gpuTimer = null;
async function loadGpu() {
  try {
    S.gpu = await api("gpu");
  } catch (e) {
    S.gpu = null;
    const card = $("gpuCard");
    if (card) card.querySelector(".hint").textContent = e.message;
    return;
  }
  const card = $("gpuCard");
  if (card) {
    card.innerHTML = card.dataset.title + gpuHtml(S.gpu);
    bindGpu(card);
  }
  clearTimeout(gpuTimer);
  if (S.gpu.state === "downloading") gpuTimer = setTimeout(loadGpu, 1500);
}

function gpuCardHtml(title) {
  const t = `<h2>${title}</h2>`;
  return `<div class="card" id="gpuCard" data-title="${esc(t)}">${t}${S.gpu ? gpuHtml(S.gpu) : '<div class="hint">Проверяю видеокарту…</div>'}</div>`;
}

function bindGpu(card) {
  if (!card) return;
  card.querySelectorAll("[data-gpu]").forEach((b) => b.addEventListener("click", async () => {
    const act = b.dataset.gpu;
    if (act === "remove" && !confirm("Удалить библиотеки CUDA? Распознавание и Qwen снова будут работать на процессоре.")) return;
    b.disabled = true;
    try {
      await api("gpu/" + act, { method: "POST", body: {} });
    } catch (e) {
      toast(e.message, 6000);
    }
    loadGpu();
  }));
}

function gpuHtml(g) {
  if (!g.nvidia) {
    return `<div class="hint">Видеокарта NVIDIA не найдена (или не установлен её драйвер) — всё считается на процессоре.
      Видеокарты Intel и AMD приложение не использует.</div>`;
  }
  const vram = (g.nvidia.vram_mb / 1024).toFixed(0);
  const small = g.nvidia.vram_mb < g.min_llm_vram_mb;
  let html = `<div class="hint">${esc(g.nvidia.name)} · ${vram} ГБ видеопамяти · драйвер ${esc(g.nvidia.driver)}</div>
    <div class="hint">В приложение входят только сборки для процессора. Здесь можно один раз скачать библиотеки CUDA
    (llama.cpp, cuBLAS, cuDNN ≈ ${fmtBytes(g.size)}): тогда Whisper${small ? "" : " и локальный Qwen"} будут считать на видеокарте —
    в несколько раз быстрее. GigaAM и разделение собеседников остаются на процессоре.</div>
    ${small ? `<div class="hint">Для Qwen на видеокарте нужно от ${Math.ceil(g.min_llm_vram_mb / 1024)} ГБ видеопамяти — на этой карте ускорится только Whisper.</div>` : ""}`;
  if (g.state === "downloading") {
    const pct = g.total ? Math.min(100, (100 * g.done) / g.total) : 0;
    html += `<div class="dl"><div class="bar"><div style="width:${pct}%"></div></div>
      <div class="dl-row"><span class="muted small">${esc(g.part)}: ${fmtBytes(g.done)} из ≈ ${fmtBytes(g.total)}</span><span class="spinner"></span></div></div>`;
  } else if (g.state === "ready" && g.outdated) {
    html += `<div class="row"><span class="badge warn">Есть новая сборка (${esc(g.latest_tag)})</span>
      <button class="ghost small" type="button" data-gpu="install">Обновить</button></div>`;
  } else if (g.state === "ready") {
    html += `<div class="row"><span class="badge ok">Включено</span>
      <span class="muted small">Whisper${g.llm_on_gpu ? " и Qwen" : ""} — на видеокарте</span>
      <button class="ghost small" type="button" data-gpu="remove">Удалить</button></div>`;
  } else {
    if (g.state === "error") html += `<div class="small" style="color:var(--rec)">${esc(g.error)}</div>`;
    html += `<button class="ghost" type="button" data-gpu="install">${g.state === "error" ? "Повторить" : "Скачать"} ≈ ${fmtBytes(g.size)}</button>`;
  }
  return html;
}

const ST_ICONS = {
  sliders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/></svg>',
  mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="19" x2="12" y2="22"/></svg>',
  cpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>',
  user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg>',
  calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  record: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/></svg>',
  wave: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="10" x2="4" y2="14"/><line x1="8" y1="6" x2="8" y2="18"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="16" y1="7" x2="16" y2="17"/><line x1="20" y1="10" x2="20" y2="14"/></svg>',
  phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2" width="12" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>',
  window: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/></svg>',
  disk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M11 12l9-9"/><path d="M17 6l3 3"/><path d="M14 9l3 3"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
  users: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  palette: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18.37 2.63L14 7l-1.59-1.59a2 2 0 0 0-2.82 0L8 7l9 9 1.59-1.59a2 2 0 0 0 0-2.82L17 10l4.37-4.37a2.12 2.12 0 1 0-3-3z"/><path d="M9 8c-2 3-4 3.5-7 4l8 10c2-1 6-5 6-7"/></svg>',
};

function getTheme() {
  try { return localStorage.getItem("ma_theme") || "system"; } catch { return "system"; }
}
function applyTheme(mode) {
  const m = (mode === "dark" || mode === "light") ? mode : "system";
  if (m === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", m);
  try { localStorage.setItem("ma_theme", m); } catch {}
}
applyTheme(getTheme());

const stSwitch = (id, on, disabled = false) =>
  `<label class="sw"><input type="checkbox" id="${id}"${on ? " checked" : ""}${disabled ? " disabled" : ""}><span></span></label>`;

const stRow = (icon, title, desc, ctrl, cls = "") =>
  `<div class="st-row${cls ? " " + cls : ""}">
    <div class="st-ico">${ST_ICONS[icon] || ""}</div>
    <div class="st-info"><div class="st-title">${title}</div>${desc ? `<div class="st-desc">${desc}</div>` : ""}</div>
    ${ctrl ? `<div class="st-ctrl">${ctrl}</div>` : ""}
  </div>`;

const stSection = (title, body, right = "") =>
  `<section class="st-section"><div class="st-head"><h2>${title}</h2>${right}</div><div class="st-group">${body}</div></section>`;

let settingsTab = "general";

function renderSettings() {
  const s = S.settings;
  if (!s) return;
  const tabs = [
    ["general", "sliders", "Общие"],
    ["ai", "spark", "Протоколы и ИИ"],
    ["speech", "mic", "Речь и голоса"],
    ["system", "cpu", "Система"],
  ];
  const autoVal = !s.auto_record ? "off" : (!IS_WIN && s.auto_record_mode === "ask" ? "ask" : "auto");
  const storageVal = !s.keep_audio ? "delete" : (s.compress_audio !== false ? "m4a" : "wav");
  const themeVal = getTheme();
  const platList = [
    ["zoom", "Zoom"],
    ["teams", "Teams"],
    ["telemost", "Телемост"],
    ["meet", "Браузер (Meet, Jitsi, РФ)"],
    ["corp", "SaluteJazz, Толк, VK, DION, TrueConf"],
    ["slack", "Slack"],
    ["discord", "Discord"],
    ...(IS_WIN ? [] : [["webex", "Webex"]]),
    ["telegram", "Telegram"],
    ["whatsapp", "WhatsApp"],
  ];

  const askDescs = {
    "claude-code": "По подписке Claude, без доплаты. Фрагменты встреч уходят в облако Anthropic.",
    "claude": "По ключу Claude API (ниже). Фрагменты уходят в облако Anthropic.",
    "gemini": "По ключу Gemini API (ниже). Фрагменты уходят в облако Google.",
    "openai": "По ключу OpenAI API (ниже). Фрагменты уходят в облако OpenAI.",
    "local": `На ${PC}, ничего не уходит в интернет. Ответ 20–60 с.`,
  };

  let body = "";
  if (settingsTab === "general") {
    body = stSection("Общие настройки", [
      stRow("user", "Как вас зовут", "Распознавание точнее пишет обращение к вам, а протокол подписывает ваши задачи по имени",
        `<input class="st-input" type="text" id="sMyName" maxlength="60" placeholder="Например, Ильяс" value="${esc(s.my_name || "")}">`),
      stRow("calendar", "Участники из календаря",
        IS_WIN ? "Брать название и участников из Outlook (если не заданы вручную)"
               : "Брать название и участников из «Календаря» (Exchange, Google, iCloud)",
        stSwitch("sCalendar", s.calendar_participants)),
      stRow("record", "Автозапись встреч", "Что происходит при начале звонка",
        `<select class="st-select" id="sAutoSelect">
          <option value="off"${autoVal === "off" ? " selected" : ""}>Выключена</option>
          <option value="auto"${autoVal === "auto" ? " selected" : ""}>Записывать сразу</option>
          ${IS_WIN ? "" : `<option value="ask"${autoVal === "ask" ? " selected" : ""}>Предлагать запись</option>`}
        </select>`),
      `<div class="st-block" id="sPlatforms"${s.auto_record ? "" : " hidden"}>
        <div class="st-desc">Где замечать начало встречи:</div>
        <div class="plat-grid">${platList.map(([id, label]) => {
          const on = (s.auto_platforms || []).includes(id);
          return `<label class="plat-chip${on ? " on" : ""}"><input type="checkbox" data-platform="${id}"${on ? " checked" : ""}><span class="p-dot"></span>${esc(label)}</label>`;
        }).join("")}</div>
        <div class="st-desc" style="margin-top:9px">${IS_WIN
          ? "Для Teams, Телемоста и браузера записывается звук компьютера и микрофон."
          : "Teams, Телемост, мессенджеры и браузер определяются по микрофону (macOS 14.2+)."}</div>
      </div>`,
    ].join(""))
    + (IS_WIN ? "" : stSection("Звук и интерфейс", [
      stRow("wave", "Подавление эха", "Вычитает звук динамиков (macOS + офлайн AEC); в наушниках отключается автоматически",
        stSwitch("sEcho", s.echo_cancel)),
      stRow("phone", "Игнорировать микрофон iPhone", "Не переключаться на лежащий рядом iPhone (Continuity Camera)",
        stSwitch("sSkipContinuity", s.skip_continuity_mic !== false)),
      stRow("window", "Плашка записи поверх окон", "Волна звука, таймер, пауза, «Стоп» и быстрая смена микрофона",
        stSwitch("sPill", s.recording_pill)),
    ].join("")))
    + stSection("Хранение записей", [
      stRow("disk", "Хранение локальных записей", "Оставлять аудио на диске, чтобы позже пересоздать расшифровку",
        `<select class="st-select" id="sStorageMode">
          <option value="delete"${storageVal === "delete" ? " selected" : ""}>Удалять после обработки</option>
          <option value="m4a"${storageVal === "m4a" ? " selected" : ""}>Сжимать в M4A (~20 МБ/ч)</option>
          <option value="wav"${storageVal === "wav" ? " selected" : ""}>Хранить WAV (~690 МБ/ч)</option>
        </select>`),
      stRow("folder", "Папка с заметками", esc(s.notes_dir),
        `<button class="st-icon-btn" type="button" id="sReveal" title="Открыть папку">${ST_ICONS.folder}</button>`),
      stRow("palette", "Тема оформления", "Цветовая схема интерфейса",
        `<select class="st-select" id="sTheme">
          <option value="system"${themeVal === "system" ? " selected" : ""}>Как в системе</option>
          <option value="dark"${themeVal === "dark" ? " selected" : ""}>Тёмная</option>
          <option value="light"${themeVal === "light" ? " selected" : ""}>Светлая</option>
        </select>`),
      `<details class="st-adv"><summary>▸ Дополнительно: заголовки окна Zoom</summary>
        <div class="st-adv-body">
          <div class="st-desc" style="margin-bottom:6px">По ним приложение понимает, что встреча Zoom началась (через запятую).</div>
          <input class="st-input wide" type="text" id="sTitles" value="${esc(s.window_titles.join(", "))}">
        </div>
      </details>`,
    ].join(""));
  } else if (settingsTab === "ai") {
    body = stSection("Кто пишет протоколы", [
      `<div class="st-block">${backendRadios(s)}</div>`,
      stRow("doc", "Шаблон по умолчанию", "Инструкция для составления протокола новых встреч",
        `<select class="st-select" id="sTemplate">${s.templates.map((t) =>
          `<option value="${esc(t.id)}"${t.id === s.template ? " selected" : ""}>${esc(t.title)}${t.custom ? " (свой)" : ""}</option>`).join("")}</select>`),
    ].join(""))
    + stSection("Шаблоны протоколов",
      s.templates.map((t) => `<div class="st-row">
        <div class="st-ico">${ST_ICONS.doc}</div>
        <div class="st-info"><div class="st-title">${esc(t.title)}
          ${t.custom ? '<span class="badge ok">свой</span>' : '<span class="muted small">встроенный</span>'}</div></div>
        <div class="st-ctrl">${t.custom
          ? `<button class="st-btn" type="button" data-tpl-edit="${esc(t.id)}">Изменить</button>
             <button class="st-btn" type="button" data-tpl-del="${esc(t.id)}">Удалить</button>`
          : `<button class="st-btn" type="button" data-tpl-copy="${esc(t.id)}">Взять за основу</button>`}</div>
      </div>`).join(""),
      `<button class="st-head-act" type="button" id="tplNew">＋ Свой шаблон</button>`)
    + stSection("Умный поиск", [
      stRow("search", "Кто отвечает на вопросы", askDescs[s.ask_backend] || "Вопросы по встречам обычными словами (Enter в поле поиска)",
        `<select class="st-select" id="sAskBackend">${Object.entries(s.ask_backends).map(([k, v]) =>
          `<option value="${k}"${k === s.ask_backend ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`),
    ].join(""))
    + stSection("Ключи облачных API", [
      `<div class="st-block">
        <div class="st-title">Gemini API ${s.gemini_key_set ? '<span class="badge ok">сохранён</span>' : ""}</div>
        <div class="st-desc">Ключ создаётся на <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>.</div>
        <div class="st-key-row"><input class="st-input" type="password" id="sGemini" placeholder="${s.gemini_key_set ? "•••••• — вставьте новый, чтобы заменить" : "AIza…"}" autocomplete="off">
          <button class="st-btn" type="button" id="sGeminiSave">Сохранить</button>
          ${s.gemini_key_set ? '<button class="st-btn" type="button" id="sGeminiDel">Удалить</button>' : ""}</div>
        ${keyCheckHtml("gemini", s.gemini_key_set)}
        ${s.gemini_key_set ? geminiModelField(s) : ""}
      </div>`,
      `<div class="st-block">
        <div class="st-title">OpenAI (ChatGPT) API ${s.openai_key_set ? '<span class="badge ok">сохранён</span>' : ""}</div>
        <div class="st-desc">Ключ создаётся на <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com/api-keys</a>.</div>
        <div class="st-key-row"><input class="st-input" type="password" id="sOpenai" placeholder="${s.openai_key_set ? "•••••• — вставьте новый, чтобы заменить" : "sk-…"}" autocomplete="off">
          <button class="st-btn" type="button" id="sOpenaiSave">Сохранить</button>
          ${s.openai_key_set ? '<button class="st-btn" type="button" id="sOpenaiDel">Удалить</button>' : ""}</div>
        ${keyCheckHtml("openai", s.openai_key_set)}
        <div class="st-key-row" style="margin-top:10px"><span class="muted small">Модель:</span>
          <input class="st-input" type="text" id="sOpenaiModel" value="${esc(s.openai_model)}" placeholder="gpt-5"></div>
      </div>`,
      `<div class="st-block">
        <div class="st-title">Claude API ${s.anthropic_key_set ? '<span class="badge ok">сохранён</span>' : ""}</div>
        <div class="st-desc">Оплата на platform.claude.com, отдельно от подписки. Claude Code:
          ${s.claude_code_path ? `<span class="badge ok">найден</span>` : '<span class="badge warn">не установлен</span>'}</div>
        <div class="st-key-row"><input class="st-input" type="password" id="sKey" placeholder="${s.anthropic_key_set ? "•••••• — вставьте новый, чтобы заменить" : "sk-ant-…"}" autocomplete="off">
          <button class="st-btn" type="button" id="sKeySave">Сохранить</button>
          ${s.anthropic_key_set ? '<button class="st-btn" type="button" id="sKeyDel">Удалить</button>' : ""}</div>
        ${keyCheckHtml("claude", s.anthropic_key_set)}
      </div>`,
    ].join(""));
  } else if (settingsTab === "speech") {
    body = stSection("Распознавание речи", [
      `<div class="st-block"><div class="st-desc">Всё работает локально на ${PC}.</div>${asrRadios(s)}</div>`,
      stRow("bolt", "Распознавать по ходу встречи",
        `С ${IS_WIN ? "GigaAM" : "GigaAM и Whisper turbo"} на пониженном приоритете — протокол готов быстрее (от батареи паузится)`,
        stSwitch("sLive", s.live_enabled)),
      `<div class="st-block">
        <div class="st-title">${ST_ICONS.book.replace("<svg ", '<svg style="width:15px;height:15px;opacity:.7" ')} Глоссарий терминов и имён</div>
        <div class="st-desc">Через запятую. Пополняется сам, когда вы правите имя или термин в расшифровке.${s.asr_engine === "gigaam"
          ? " С GigaAM похожие слова перепроверяются Whisper." : ""}</div>
        <textarea class="st-textarea" id="sGlossary" placeholder="Например: ЦБ, GigaAM, Ильяс, Сковиков">${esc(s.glossary.join(", "))}</textarea>
      </div>`,
    ].join(""))
    + stSection("Разделение собеседников и голоса", [
      stRow("users", "Разделять собеседников по голосам", "Подписывает «Собеседник 1», «Собеседник 2»… Ваша речь всегда «Я»",
        stSwitch("sDiar", s.diarization_enabled)),
      stRow("user", "Узнавать по базе «Голоса»", "Автоматически подставлять сохранённые имена коллег",
        stSwitch("sVoices", s.voices_enabled, !s.diarization_enabled), s.diarization_enabled ? "" : "disabled"),
      stRow("spark", "Угадывать имена из разговора", "Если имя не в базе голосов — отмечается звёздочкой «Олег*»",
        stSwitch("sGuess", s.names_guess, !s.diarization_enabled), s.diarization_enabled ? "" : "disabled"),
      `<div class="st-block">
        <div class="st-title">Токен Hugging Face ${s.hf_token_set ? '<span class="badge ok">сохранён</span>' : ""}</div>
        <div class="st-desc">Нужен бесплатный токен типа Read и согласие («Agree») для
          <a href="https://huggingface.co/pyannote/speaker-diarization-3.1" target="_blank">speaker-diarization-3.1</a> и
          <a href="https://huggingface.co/pyannote/segmentation-3.0" target="_blank">segmentation-3.0</a>.</div>
        <div class="st-key-row"><input class="st-input" type="password" id="sHf" placeholder="${s.hf_token_set ? "•••••• — вставьте новый, чтобы заменить" : "hf_…"}" autocomplete="off">
          <button class="st-btn" type="button" id="sHfSave">Сохранить</button>
          <button class="st-btn" type="button" id="sHfCheck"${s.hf_token_set ? "" : " disabled"}>Проверить</button></div>
        <div id="sHfResult" class="st-desc" style="margin-top:8px"></div>
      </div>`,
    ].join(""));
  } else {
    const curVer = (S.status && S.status.updates && S.status.updates.version) ? `v${esc(S.status.updates.version)}` : "";
    body = stSection("Обновления и диагностика", [
      stRow("refresh", "Обновления и компоненты",
        `Код и интерфейс, ${IS_WIN ? "пакеты Python, " : ""}модели${IS_WIN ? " и ускорение NVIDIA" : ""}`,
        `<button class="st-btn" type="button" id="sUpdates">Открыть${UPD.count ? ` (${UPD.count})` : ""}</button>`),
      stRow("doc", "Логи и диагностика приложения",
        "Обезличенный .zip с логами, версиями и аудиоустройствами — без текстов встреч, имён и ключей",
        `<button class="st-icon-btn" type="button" id="sDiagFolder" title="Открыть папку с логами">${ST_ICONS.folder}</button>
         <button class="st-btn" type="button" id="sDiag">Сохранить</button>`),
    ].join(""), curVer ? `<span class="muted small">${curVer}</span>` : "")
    + `<div class="st-legal">${ST_ICONS.shield}<span><b>Запись встреч и закон:</b> приложение не уведомляет участников о записи. Предупреждайте коллег и сверяйтесь с правилами компании.</span></div>`;
  }

  $("settings").innerHTML = `<div class="st-page">
    <div class="st-top">
      <h1>Настройки</h1>
      <div class="st-tabs" role="tablist">${tabs.map(([id, ico, label]) =>
        `<button class="st-tab${settingsTab === id ? " active" : ""}" type="button" data-st-tab="${id}">
          ${ST_ICONS[ico]}<span>${label}</span>${id === "system" && UPD.count ? `<span class="dot-badge">${UPD.count}</span>` : ""}
        </button>`).join("")}</div>
    </div>
    ${body}
  </div>`;

  $("settings").querySelectorAll("[data-st-tab]").forEach((b) => b.onclick = () => {
    settingsTab = b.dataset.stTab;
    renderSettings();
  });

  bindBackendRadios($("settings"));
  const save = async (body, msg = "Сохранено") => {
    try {
      S.settings = await api("settings", { method: "POST", body });
      toast(msg);
      return true;
    } catch (e) {
      toast(e.message);
      return false;
    }
  };
  const list = (v) => v.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

  if ($("sUpdates")) $("sUpdates").onclick = openUpdates;
  if ($("sDiagFolder")) $("sDiagFolder").onclick = () => api("reveal", { method: "POST", body: { kind: "logs" } });
  if ($("sDiag")) $("sDiag").onclick = async () => {
    try {
      const r = await api("diagnostics", { method: "POST", body: {} });
      toast(`Диагностика сохранена в «Загрузки»: ${r.name}`, 5000);
    } catch (e) { toast(e.message); }
  };
  if ($("sTemplate")) $("sTemplate").onchange = (e) => save({ template: e.target.value });
  if ($("tplNew")) $("tplNew").onclick = () => openTemplate(null);
  $("settings").querySelectorAll("[data-tpl-edit]").forEach((b) => b.onclick = () => openTemplate(b.dataset.tplEdit));
  $("settings").querySelectorAll("[data-tpl-copy]").forEach((b) => b.onclick = () => openTemplate(b.dataset.tplCopy, true));
  $("settings").querySelectorAll("[data-tpl-del]").forEach((b) => b.onclick = async () => {
    const t = s.templates.find((x) => x.id === b.dataset.tplDel);
    if (!confirm(`Удалить шаблон «${t ? t.title : ""}»? Готовые протоколы останутся.`)) return;
    try {
      await api("template?id=" + encodeURIComponent(b.dataset.tplDel), { method: "DELETE" });
      S.settings = await api("settings");
      renderSettings();
      toast("Шаблон удалён");
    } catch (e) { toast(e.message); }
  });
  if ($("sGlossary")) $("sGlossary").onchange = (e) => save({ glossary: list(e.target.value) });
  if ($("sMyName")) $("sMyName").onchange = (e) => save({ my_name: e.target.value.trim() }, e.target.value.trim() ? `Запомнил: вас зовут ${e.target.value.trim()}` : "Имя убрано");
  if ($("sTitles")) $("sTitles").onchange = (e) => save({ window_titles: list(e.target.value) });
  if ($("sDiar")) $("sDiar").onchange = async (e) => { await save({ diarization_enabled: e.target.checked }); renderSettings(); loadVoices(true); };
  if ($("sVoices")) $("sVoices").onchange = (e) => { save({ voices_enabled: e.target.checked }); loadVoices(true); };
  if ($("sGuess")) $("sGuess").onchange = (e) => save({ names_guess: e.target.checked });
  if ($("sLive")) $("sLive").onchange = (e) => save({ live_enabled: e.target.checked });
  if ($("sAskBackend")) $("sAskBackend").onchange = async (e) => {
    if (await save({ ask_backend: e.target.value }, "Умный поиск: " + s.ask_backends[e.target.value])) renderSettings();
  };
  if ($("sGeminiSave")) $("sGeminiSave").onclick = async () => {
    const v = $("sGemini").value.trim();
    if (v && await save({ gemini_key: v }, "Ключ Gemini сохранён")) { S.geminiModels = undefined; renderSettings(); checkKey("gemini"); }
  };
  if ($("sGeminiDel")) $("sGeminiDel").onclick = async () => { if (await save({ gemini_key: "" }, "Ключ Gemini удалён")) renderSettings(); };
  if ($("sOpenaiSave")) $("sOpenaiSave").onclick = async () => {
    const v = $("sOpenai").value.trim();
    if (v && await save({ openai_key: v }, "Ключ OpenAI сохранён")) { renderSettings(); checkKey("openai"); }
  };
  if ($("sOpenaiDel")) $("sOpenaiDel").onclick = async () => { if (await save({ openai_key: "" }, "Ключ OpenAI удалён")) renderSettings(); };
  if ($("sOpenaiModel")) $("sOpenaiModel").onchange = (e) => { if (e.target.value.trim()) { delete S.keyCheck.openai; save({ openai_model: e.target.value }, "Модель ChatGPT: " + e.target.value.trim()); } };
  if ($("sAutoSelect")) $("sAutoSelect").onchange = async (e) => {
    const v = e.target.value;
    const payload = IS_WIN ? { auto_record: v !== "off" } : { auto_record: v !== "off", auto_record_mode: v === "ask" ? "ask" : "auto" };
    await save(payload);
    native("reloadSettings");
    if ($("sPlatforms")) $("sPlatforms").hidden = v === "off";
  };
  $("settings").querySelectorAll("[data-platform]").forEach((c) => c.onchange = async () => {
    c.closest(".plat-chip").classList.toggle("on", c.checked);
    const pl = [...$("settings").querySelectorAll("[data-platform]:checked")].map((x) => x.dataset.platform);
    await save({ auto_platforms: pl });
    native("reloadSettings");
  });
  if ($("sStorageMode")) $("sStorageMode").onchange = (e) => {
    const v = e.target.value;
    save({ keep_audio: v !== "delete", compress_audio: v === "m4a" });
  };
  if ($("sTheme")) $("sTheme").onchange = (e) => {
    applyTheme(e.target.value);
    toast("Тема обновлена");
  };
  if ($("sEcho")) $("sEcho").onchange = async (e) => { await save({ echo_cancel: e.target.checked }); native("reloadSettings"); };
  if ($("sSkipContinuity")) $("sSkipContinuity").onchange = async (e) => { await save({ skip_continuity_mic: e.target.checked }); native("reloadSettings"); };
  if ($("sPill")) $("sPill").onchange = async (e) => { await save({ recording_pill: e.target.checked }); native("reloadSettings"); };
  if ($("sCalendar")) $("sCalendar").onchange = async (e) => {
    await save({ calendar_participants: e.target.checked }, e.target.checked ? "Участники будут браться из календаря" : "Календарь не используется");
    if (e.target.checked) native("calendarAccess");
    native("reloadSettings");
  };
  if ($("sReveal")) $("sReveal").onclick = () => api("reveal", { method: "POST", body: {} });
  if ($("sHfSave")) $("sHfSave").onclick = async () => {
    const v = $("sHf").value.trim();
    if (v && await save({ hf_token: v }, "Токен сохранён")) { renderSettings(); checkHf(); }
  };
  if ($("sHfCheck")) $("sHfCheck").onclick = checkHf;
  if ($("sKeySave")) $("sKeySave").onclick = async () => {
    const v = $("sKey").value.trim();
    if (v && await save({ anthropic_key: v }, "Ключ сохранён")) { renderSettings(); checkKey("claude"); }
  };
  if ($("sKeyDel")) $("sKeyDel").onclick = async () => { if (await save({ anthropic_key: "" }, "Ключ удалён")) renderSettings(); };
}

// Результат проверки хранится в S.hfCheck: его показывают и настройки, и экран первого запуска.
async function checkHf() {
  const paint = () => {
    const out = $("sHfResult");
    if (out) {
      let html = hfCheckHtml();
      if (S.hfCheck.ok && S.status.models.diarization.status !== "ready") {
        html += ' <button class="ghost" data-download="diarization">Скачать модель</button>';
      }
      out.innerHTML = html;
    }
    if (S.view === "setup") renderSetup();
  };
  S.hfCheck = { pending: true };
  paint();
  try {
    S.hfCheck = await api("check/hf", { method: "POST" });
  } catch (e) {
    S.hfCheck = { ok: false, message: e.message };
  }
  paint();
}

function hfCheckHtml() {
  const r = S.hfCheck;
  if (!r) return "";
  if (r.pending) return '<span class="spinner"></span> Проверяю…';
  let html = `<span class="badge ${r.ok ? "ok" : "err"}">${esc(r.message)}</span>`;
  if (r.missing) {
    html += "<br>Откройте и нажмите «Agree»: " + r.missing.map((m) =>
      `<a href="https://huggingface.co/${m}" target="_blank">${esc(m)}</a>`).join(", ") + ", затем «Проверить токен».";
  }
  return html;
}

$("settingsBtn").addEventListener("click", async () => {
  S.settings = await api("settings").catch(() => S.settings);
  show("settings");
  renderSettings();
});

// Внешние ссылки — в браузере, а не внутри окна приложения.
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href^='http']");
  if (!a) return;
  e.preventDefault();
  if (!native("openURL", { url: a.href })) window.open(a.href, "_blank");
});

// ---------- опрос состояния ----------

let pollTimer;
async function pollStatus() {
  clearTimeout(pollTimer);
  try {
    S.status = await api("status");
    if ($("updDialog").open) paintUpdWindow(); else paintUpdBadge();
    renderRecording();
    renderJobs();
    if (S.status.setup_needed && !S.setupDismissed && S.view !== "settings") {
      if (S.view !== "setup") show("setup");
      renderSetup();
    } else if (S.view === "setup") {
      renderSetup();
    } else if (S.view === "settings" && document.activeElement.tagName !== "INPUT"
               && document.activeElement.tagName !== "TEXTAREA") {
      // Прогресс загрузки и смена её состояния (пауза, готово, ошибка) — сразу видны в настройках.
      const sig = Object.values(S.status.models).map((m) => m.status + (m.error || "")).join("|");
      const downloading = Object.values(S.status.models).some((m) => m.status === "downloading");
      if (downloading || sig !== S.modelsSig) {
        if (sig !== S.modelsSig && !downloading) S.settings = await api("settings").catch(() => S.settings);
        S.modelsSig = sig;
        renderSettings();
      }
    }
  } catch (e) {
    console.warn(e);
  }
  const busy = S.status && (S.status.recording.state === "recording"
    || S.status.jobs.some((j) => j.status === "running" || j.status === "queued")
    || Object.values(S.status.models).some((m) => m.status === "downloading"));
  pollTimer = setTimeout(pollStatus, busy ? 1000 : 3000);
}

// Нативная оболочка сообщает о событиях (например, «запись остановлена»).
window.agentEvent = (name) => {
  if (name === "status") pollStatus();
  if (name === "settings") $("settingsBtn").click();
};

// ---------- главы, правка расшифровки, версии протокола, фильтры, плеер ----------

function transcriptToolsHtml(m) {
  return `<div class="tr-tools">
    <label class="check small" title="Убрать «э-э», «как бы», «типа», повторы слов и реплики из одного «угу». Сама расшифровка не меняется">
      <input type="checkbox" id="cleanToggle"${CLEAN ? " checked" : ""}> Без слов-паразитов</label>
    ${EDIT.undo.length ? '<button class="link small" type="button" id="undoEdit">↶ Отменить правку</button>' : ""}
    ${m.source && !(m.chapters || []).length ? `<button class="ghost small" type="button" id="mkChapters"${CH.busy ? " disabled" : ""}>${CH.busy ? '<span class="spinner"></span> Делю на главы…' : "Разделить на главы"}</button>` : ""}
  </div>`;
}

function chaptersTocHtml(m) {
  const ch = m.chapters || [];
  if (!ch.length) return "";
  return `<nav class="toc"><div class="toc-head">Главы</div>${ch.map((c, i) =>
    `<button class="toc-item" type="button" data-ch="${i + 1}"><span class="toc-t">${fmtTs(c.start).replace(/^00:/, "")}</span> ${esc(c.title)}</button>`).join("")}</nav>`;
}

const CH = { busy: false };
async function makeChapters() {
  const m = S.current;
  CH.busy = true; renderMeeting();
  try {
    const r = await api("meeting/chapters", { method: "POST", body: { id: m.id } });
    if (S.current && S.current.id === m.id) S.current = r;
    toast("Готово: встреча разделена на главы");
  } catch (e) { toast(e.message, 6000); }
  CH.busy = false;
  renderMeeting();
}

// Правка реплик и отмена (⌘Z): стек правок текущей встречи.
const EDIT = { undo: [], meeting: null };
function startEdit(i) {
  const m = S.current;
  const seg = m.segments.find((s) => s.i === i);
  const row = document.querySelector(`#mBody .utt[data-i="${i}"]`);
  if (!seg || !row) return;
  const span = row.querySelector(".utt-text");
  span.outerHTML = `<span class="utt-edit"><textarea rows="${Math.min(8, Math.ceil(seg.text.length / 90) + 1)}">${esc(seg.text)}</textarea>
    <span class="utt-edit-btns"><button class="primary small" type="button" data-save="${i}">Сохранить</button>
    <button class="ghost small" type="button" data-cancel-edit>Отмена</button>
    <span class="muted small">Enter — сохранить, Esc — отмена; пустой текст удалит реплику</span></span></span>`;
  const ta = row.querySelector("textarea");
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
}
async function saveEdit(i, text, { recordUndo = true } = {}) {
  const m = S.current;
  try {
    const r = await api("segment/edit", { method: "POST", body: { id: m.id, index: i, text } });
    if (EDIT.meeting !== m.id) { EDIT.undo = []; EDIT.meeting = m.id; }
    if (recordUndo && r.old) EDIT.undo.push(text.trim() ? { kind: "edit", i, text: r.old.text } : { kind: "restore", i, seg: r.old });
    if (r.learned && r.learned.length) toast(`В словарь распознавания: ${r.learned.join(", ")} — в следующих встречах это услышат правильно`, 6000);
    S.current = r.meeting;
    renderMeeting();
  } catch (e) { toast(e.message, 5000); }
}
async function undoEdit() {
  const m = S.current;
  if (!m || EDIT.meeting !== m.id || !EDIT.undo.length) return;
  const op = EDIT.undo.pop();
  try {
    if (op.kind === "edit") {
      const r = await api("segment/edit", { method: "POST", body: { id: m.id, index: op.i, text: op.text } });
      S.current = r.meeting;
    } else {
      S.current = await api("segment/restore", { method: "POST", body: { id: m.id, index: op.i, segment: op.seg } });
    }
    renderMeeting();
    toast("Правка отменена");
  } catch (e) { toast(e.message, 5000); }
}

// Версии протокола под адресата: «письмо коллегам», «для CEO»…
const VER = { sel: {}, pending: {}, draft: {}, error: {} };
function currentVersion(m) {
  const id = m && VER.sel[m.id];
  return id && id !== "new" ? (m.versions || []).find((v) => v.id === id) || null : null;
}
function versionsBarHtml(m) {
  if (!m.source) return "";
  const sel = VER.sel[m.id] || "";
  const vs = m.versions || [];
  return `<div class="ver-bar">
    <button class="ver-tab${!sel ? " active" : ""}" type="button" data-ver="">Протокол</button>
    ${vs.map((v) => `<button class="ver-tab${sel === v.id ? " active" : ""}" type="button" data-ver="${esc(v.id)}" title="${esc(v.title)} — «${esc(v.request)}»">${esc(v.label || v.title)}</button>`).join("")}
    ${VER.pending[m.id] ? '<span class="ver-tab pending"><span class="spinner"></span> Пишу…</span>' : ""}
    <button class="ver-tab add${sel === "new" ? " active" : ""}" type="button" data-ver="new" title="Протокол или письмо под адресата — своими словами">＋ Для кого?</button>
  </div>`;
}
function versionBodyHtml(m) {
  const sel = VER.sel[m.id];
  if (sel === "new") {
    const ex = m.version_examples || [];
    return `<form class="ver-form" id="verForm">
      <p class="muted small">Напишите, какой документ нужен и для кого — модель подстроит язык, длину и подробности под адресата.
        Основной протокол не изменится.</p>
      <div class="quick">${ex.map((x) => `<button class="ghost small" type="button" data-ver-ex="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <textarea id="verInput" rows="3" placeholder="Например: протокол для CEO — на полстраницы, начни с вывода, что нужно от него; без технических деталей">${esc(VER.draft[m.id] || "")}</textarea>
      <button class="primary" type="submit"${VER.pending[m.id] ? " disabled" : ""}>Составить</button>
      ${VER.error[m.id] && fallbackOptions(VER.error[m.id]).length ? `<div class="a err">${esc(VER.error[m.id])}${fallbackHtml(VER.error[m.id], "ver", 0)}</div>` : ""}
    </form>`;
  }
  const v = currentVersion(m);
  if (!v) return "";
  return `<div class="ver-head muted small">Запрос: «${esc(v.request)}»
    <button class="link small" type="button" data-ver-del="${esc(v.id)}">Удалить версию</button></div>
    ${linkTimes(markdown(v.text), m.can_play)}`;
}
async function createVersion(request, override = null) {
  const m = S.current;
  if (!request.trim() || VER.pending[m.id]) return;
  delete VER.error[m.id];
  VER.pending[m.id] = request;
  VER.draft[m.id] = "";
  VER.sel[m.id] = "";
  renderMeeting();
  try {
    const r = await api("meeting/version", { method: "POST", body: { id: m.id, request, ...(override || {}) } });
    if (S.current && S.current.id === m.id) { S.current = r.meeting; VER.sel[m.id] = r.version.id; }
  } catch (e) {
    if (!fallbackOptions(e.message).length) toast(e.message, 6000);
    VER.error[m.id] = e.message;
    VER.draft[m.id] = request;
    VER.sel[m.id] = "new";
  }
  delete VER.pending[m.id];
  if (S.current && S.current.id === m.id) renderMeeting();
}

$("mBody").addEventListener("click", async (e) => {
  const t = e.target;
  const tc = t.closest(".tc");
  if (tc) { playFrom(+tc.dataset.t); return; }
  const toc = t.closest("[data-ch]");
  if (toc) {
    const h = $("ch" + toc.dataset.ch);
    if (h) { h.scrollIntoView({ block: "start", behavior: "smooth" }); h.classList.add("flash"); setTimeout(() => h.classList.remove("flash"), 1800); }
    return;
  }
  if (t.id === "mkChapters") { makeChapters(); return; }
  if (t.id === "undoEdit") { undoEdit(); return; }
  const ed = t.closest("[data-edit]");
  if (ed) { startEdit(+ed.dataset.edit); return; }
  const sv = t.closest("[data-save]");
  if (sv) { saveEdit(+sv.dataset.save, sv.closest(".utt").querySelector("textarea").value); return; }
  if (t.closest("[data-cancel-edit]")) { renderMeeting(); return; }
  const vt = t.closest("[data-ver]");
  if (vt) { VER.sel[S.current.id] = vt.dataset.ver; renderMeeting(); if (vt.dataset.ver === "new") $("verInput")?.focus(); return; }
  const vx = t.closest("[data-ver-ex]");
  if (vx) { $("verInput").value = vx.dataset.verEx; VER.draft[S.current.id] = vx.dataset.verEx; $("verInput").focus(); return; }
  const vd = t.closest("[data-ver-del]");
  if (vd) {
    if (!confirm("Удалить эту версию? Основной протокол останется.")) return;
    try {
      S.current = await api(`meeting/version?id=${encodeURIComponent(S.current.id)}&vid=${encodeURIComponent(vd.dataset.verDel)}`, { method: "DELETE" });
      VER.sel[S.current.id] = "";
      renderMeeting();
    } catch (err) { toast(err.message); }
  }
});
$("mBody").addEventListener("click", (e) => {
  const fb = e.target.closest("[data-fb]");
  if (!fb || !S.current) return;
  const m = S.current;
  if (fb.dataset.fb === "chat") {
    const log = MCHAT.log[m.id] || [];
    const item = log[+fb.dataset.fbI];
    if (!item) return;
    const opt = fallbackOptions(item.error)[+fb.dataset.fbO];
    log.splice(+fb.dataset.fbI, 1);
    sendMeetingAsk(item.q, opt);
  } else {
    const opt = fallbackOptions(VER.error[m.id])[+fb.dataset.fbO];
    createVersion(VER.draft[m.id] || "", opt);
  }
});
$("mBody").addEventListener("change", (e) => {
  if (e.target.id !== "cleanToggle") return;
  CLEAN = e.target.checked;
  try { localStorage.setItem("ma.clean", CLEAN ? "1" : "0"); } catch (_) {}
  renderMeeting();
});
$("mBody").addEventListener("submit", (e) => {
  if (e.target.id !== "verForm") return;
  e.preventDefault();
  createVersion($("verInput").value);
});
$("mBody").addEventListener("input", (e) => {
  if (e.target.id === "verInput" && S.current) VER.draft[S.current.id] = e.target.value;
});
$("mBody").addEventListener("keydown", (e) => {
  const ta = e.target.closest(".utt-edit textarea");
  if (ta) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveEdit(+ta.closest(".utt").dataset.i, ta.value); }
    if (e.key === "Escape") { e.preventDefault(); renderMeeting(); }
  }
});
$("mMeta").addEventListener("click", (e) => {
  const b = e.target.closest("[data-topic]");
  if (!b) return;
  FILTER.topic = b.dataset.topic;
  renderFilters();
  renderList();
  toast(`Показаны встречи на тему «${b.dataset.topic}»`);
});

// Ручная правка протокола целиком, «найти и заменить» по всей встрече, отмена последней правки.
const SUMEDIT = { id: null, busy: false };
function summaryToolsHtml(m) {
  return `<div class="sum-tools">
    <button class="link small" type="button" id="sumEdit" title="Исправить текст протокола вручную">✎ Править протокол</button>
    ${m.can_undo ? '<button class="link small" type="button" id="sumUndo" title="Вернуть встречу как было до последней правки или замены">↶ Отменить последнюю правку</button>' : ""}
  </div>`;
}
function summaryEditorHtml(m) {
  return `<div class="sum-edit">
    <p class="muted small">Markdown: «## Раздел», «- пункт», таблицы «| … |». Время в скобках [00:12:40] станет кнопкой «▶».</p>
    <textarea id="sumText" spellcheck="true">${esc(m.summary)}</textarea>
    <div class="utt-edit-btns"><button class="primary" type="button" id="sumSave"${SUMEDIT.busy ? " disabled" : ""}>Сохранить</button>
      <button class="ghost" type="button" id="sumCancel">Отмена</button>
      <span class="muted small">⌘Enter — сохранить, Esc — отмена</span></div>
  </div>`;
}
async function saveSummary() {
  const m = S.current;
  SUMEDIT.busy = true;
  try {
    S.current = await api("meeting/summary", { method: "POST", body: { id: m.id, text: $("sumText").value } });
    SUMEDIT.id = null;
    toast("Протокол сохранён");
  } catch (e) { toast(e.message, 5000); }
  SUMEDIT.busy = false;
  renderMeeting();
}
async function undoMeetingEdit() {
  try {
    S.current = await api("meeting/undo", { method: "POST", body: { id: S.current.id } });
    renderMeeting();
    loadMeetings();
    toast("Последняя правка отменена");
  } catch (e) { toast(e.message, 5000); }
}
$("mBody").addEventListener("click", (e) => {
  if (!S.current) return;
  if (e.target.id === "sumEdit") { SUMEDIT.id = S.current.id; renderMeeting(); const t = $("sumText"); t.style.height = Math.max(320, t.scrollHeight + 10) + "px"; t.focus(); }
  else if (e.target.id === "sumCancel") { SUMEDIT.id = null; renderMeeting(); }
  else if (e.target.id === "sumSave") saveSummary();
  else if (e.target.id === "sumUndo") undoMeetingEdit();
});
$("mBody").addEventListener("keydown", (e) => {
  if (e.target.id !== "sumText") return;
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveSummary(); }
  if (e.key === "Escape") { e.preventDefault(); SUMEDIT.id = null; renderMeeting(); }
});

function rplRegex() {
  const f = $("rplFind").value.trim();
  if (!f) return null;
  const body = f.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp($("rplWord").checked ? `(?<![\\p{L}\\p{N}_-])${body}(?![\\p{L}\\p{N}_-])` : body, "gu" + ($("rplCase").checked ? "" : "i"));
}
function rplPreview() {
  const m = S.current, re = rplRegex();
  if (!m || !re) { $("rplCount").textContent = ""; return; }
  const n = (t) => ((t || "").match(re) || []).length;
  const p = n(m.title) + n(m.summary);
  const t = (m.segments && m.segments.length) ? m.segments.reduce((a, s) => a + n(s.text), 0) : n(m.transcript);
  const v = (m.versions || []).reduce((a, x) => a + n(x.text) + n(x.title), 0);
  $("rplCount").textContent = p + t + v
    ? `Найдено: ${p + t + v} — в протоколе ${p}, в расшифровке ${t}${v ? `, в версиях ${v}` : ""}.`
    : "Не найдено ни разу.";
  $("rplGo").disabled = !(p + t + v) || !$("rplTo").value.trim();
}
["rplFind", "rplTo"].forEach((id) => $(id).addEventListener("input", rplPreview));
["rplWord", "rplCase"].forEach((id) => $(id).addEventListener("change", rplPreview));
$("replaceBtn").addEventListener("click", () => {
  if (!S.current) return;
  const sel = (window.getSelection && String(window.getSelection()).trim()) || "";
  $("rplFind").value = sel.length && sel.length < 60 ? sel : "";
  $("rplTo").value = "";
  $("rplGloss").checked = true;
  rplPreview();
  $("replaceDialog").showModal();
  ($("rplFind").value ? $("rplTo") : $("rplFind")).focus();
});
$("replaceDialog").addEventListener("close", async () => {
  if ($("replaceDialog").returnValue !== "ok" || !S.current) return;
  try {
    const r = await api("meeting/replace", { method: "POST", body: {
      id: S.current.id, find: $("rplFind").value, replace: $("rplTo").value,
      whole_word: $("rplWord").checked, case: $("rplCase").checked, glossary: $("rplGloss").checked } });
    S.current = r.meeting;
    renderMeeting();
    loadMeetings();
    const c = r.counts;
    toast(c.total ? `Заменено: ${c.total} — протокол ${c.protocol}, расшифровка ${c.transcript}${c.versions ? `, версии ${c.versions}` : ""}. Отменить — «↶ Отменить последнюю правку» в протоколе`
      : "Ничего не найдено", 7000);
    if ($("rplGloss").checked) S.settings = await api("settings");
  } catch (e) { toast(e.message, 6000); }
});

// Плеер: скорость, пробел — пауза, ← → — 10 секунд; ⌘Z — отменить правку расшифровки.
try { $("speed").value = localStorage.getItem("ma.speed") || "1"; } catch (_) {}
$("audio").playbackRate = +$("speed").value;
$("audio").addEventListener("loadedmetadata", () => { $("audio").playbackRate = +$("speed").value; });
$("speed").addEventListener("change", () => {
  $("audio").playbackRate = +$("speed").value;
  try { localStorage.setItem("ma.speed", $("speed").value); } catch (_) {}
});
document.addEventListener("keydown", (e) => {
  if (S.view !== "meeting" || !S.current || document.querySelector("dialog[open]")) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || e.target.isContentEditable) return;
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === "z" && S.tab === "transcript") {
    e.preventDefault(); undoEdit(); return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey || !S.current.can_play) return;
  const a = $("audio");
  const loaded = a.dataset.id === S.current.id;
  if (e.key === " " || e.code === "Space") {
    e.preventDefault();
    if (!loaded) playFrom(0); else if (a.paused) a.play().catch(() => {}); else a.pause();
  } else if (loaded && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    e.preventDefault();
    a.currentTime = Math.max(0, a.currentTime + (e.key === "ArrowLeft" ? -10 : 10));
  }
});

// Фильтры списка: участник и тема.
const FILTER = { person: "", topic: "" };
const GENERIC_SPEAKER = /^(Я|Собеседник(\s*\d+)?|Speaker\s*\d+)$/i;
function personsOf(m) {
  return (m.participants || "").split(",").map((x) => x.trim().replace(/[*?]|\s*\(\?\)$/g, "").trim()).filter((x) => x && !GENERIC_SPEAKER.test(x));
}
function renderFilters() {
  const count = (arr) => { const c = new Map(); arr.forEach((x) => c.set(x, (c.get(x) || 0) + 1)); return [...c.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru")); };
  const persons = count(S.meetings.flatMap(personsOf));
  const topics = count(S.meetings.flatMap((m) => (m.topics || []).map((t) => t.toLowerCase())));
  $("filters").hidden = !persons.length && !topics.length;
  const opts = (list, all, cur) => `<option value="">${all}</option>` + list.map(([x, n]) =>
    `<option value="${esc(x)}"${x === cur ? " selected" : ""}>${esc(x)} (${n})</option>`).join("");
  $("fPerson").innerHTML = opts(persons, "Все участники", FILTER.person);
  $("fTopic").innerHTML = opts(topics, "Все темы", FILTER.topic.toLowerCase());
  $("fPerson").hidden = !persons.length;
  $("fTopic").hidden = !topics.length;
  $("fPerson").classList.toggle("on", !!FILTER.person);
  $("fTopic").classList.toggle("on", !!FILTER.topic);
}
function passesFilter(m) {
  if (FILTER.person && !personsOf(m).includes(FILTER.person)) return false;
  if (FILTER.topic && !(m.topics || []).some((t) => t.toLowerCase() === FILTER.topic.toLowerCase())) return false;
  return true;
}
$("fPerson").addEventListener("change", () => { FILTER.person = $("fPerson").value; renderFilters(); renderList(); });
$("fTopic").addEventListener("change", () => { FILTER.topic = $("fTopic").value; renderFilters(); renderList(); });

// ---------- старт ----------

(async function init() {
  if (!TOKEN) {
    document.body.innerHTML = '<p style="padding:40px">Откройте Meeting Agent через приложение.</p>';
    return;
  }
  try {
    S.settings = await api("settings");
  } catch (e) {
    toast(e.message);
  }
  await loadMeetings();
  loadVoices();
  renderNextPeople();
  show("meeting");
  if (S.meetings.length) openMeeting(S.meetings[0].id);
  pollStatus();
  showUpdateDone();
  setTimeout(autoCheckUpdates, 4000);  // тихая проверка обновлений (GitVerse/GitLab — не чаще раза в сутки)
})();

