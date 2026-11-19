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
  if (rb) { rb.textContent = "В Проводнике"; rb.title = "Показать файл заметки в Проводнике"; }
}

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

function segmentsHtml(segs) {
  return segs.map((s) => `<div class="utt" data-start="${s.start}"><span class="ts">${fmtTs(s.start)}</span>${s.label
    ? `<button class="who who-btn" data-seg="${s.i}" title="Это сказал другой человек?">${esc(s.speaker)}:</button>`
    : `<span class="who">${esc(s.speaker)}:</span>`} ${esc(s.text)}</div>`).join("");
}

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

function show(view) {
  S.view = view;
  for (const id of ["empty", "meeting", "settings", "setup", "voices", "ask"]) $(id).hidden = true;
  stopAudio();
  if (view === "meeting") (S.current ? $("meeting") : $("empty")).hidden = false;
  else $(view).hidden = false;
  renderList();
}

// ---------- список встреч ----------

async function loadMeetings() {
  try {
    S.meetings = await api("meetings");
  } catch (e) {
    toast(e.message);
  }
  renderList();
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const items = S.meetings.filter((m) => !q || (m.title + " " + m.participants).toLowerCase().includes(q));
  const list = $("list");
  if (!items.length) {
    list.innerHTML = `<div class="list-empty">${q ? "Ничего не нашлось" : "Пока нет ни одной встречи"}</div>`;
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
    + (m.source ? ` <button class="link small" id="mPeople">${m.expected && m.expected.length
      ? "По списку: " + esc(m.expected.join(", ")) + " · изменить" : "＋ Указать участников"}</button>` : "");
  if ($("mPeople")) $("mPeople").onclick = () => openPeople("meeting");
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === S.tab));
  const stale = m.protocol_error
    ? `<div class="stale">Протокол не составлен: ${esc(m.protocol_error.slice(0, 200))}. Расшифровка сохранена.
       <button class="ghost" id="staleRedo">Составить протокол</button></div>`
    : m.protocol_stale
    ? `<div class="stale">Разметка собеседников изменена — протокол составлен по старой.
       <button class="ghost" id="staleRedo">Пересоздать протокол</button></div>` : "";
  if (S.tab === "summary") {
    $("mBody").innerHTML = stale + markdown(m.summary);
  } else {
    $("mBody").innerHTML = (m.segments && m.segments.length)
      ? (stale || '<p class="muted small">Подписи неверны? Нажмите на имя у реплики и укажите, кто на самом деле говорил.</p>')
        + segmentsHtml(m.segments)
      : transcriptHtml(m.transcript);
  }
  if ($("staleRedo")) $("staleRedo").onclick = redoProtocol;
  $("redoBtn").disabled = !m.source;
  renderSpeakers();
  if (S.tab === "summary") linkContacts($("mBody"));
}

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

async function redoProtocol() {
  try {
    await api("reprocess", { method: "POST", body: { id: S.current.id, template: S.current.template || undefined } });
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
  const me = (m.participants || "").split(",").map((x) => x.trim()).includes("Я") ? '<span class="chip me">Я</span>' : "";
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
    const label = sp.name ? esc(sp.name) + (sp.uncertain ? ' <b class="q">?</b>' : "")
      : sp.guess ? esc(sp.guess) + '<b class="star">*</b>' : esc(sp.base);
    return `<span class="chip${cls}">
      ${sp.has_sample ? `<button class="play" data-play-speaker="${esc(sp.label)}" title="Прослушать">▶</button>` : ""}
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

let renameTarget = null;
async function openRename(sp) {
  renameTarget = sp;
  await loadVoices();
  $("voiceNames").innerHTML = (S.voices.voices || []).map((v) => `<option value="${esc(v.name)}">`).join("");
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

let audioEl = null, audioBtn = null;
function stopAudio() {
  if (audioEl) { audioEl.pause(); URL.revokeObjectURL(audioEl.src); }
  if (audioBtn) audioBtn.classList.remove("playing");
  audioEl = audioBtn = null;
}

async function playAudio(path, btn) {
  const same = audioBtn === btn;
  stopAudio();
  if (same) return;  // повторное нажатие — стоп
  try {
    const r = await fetch("/api/" + path, { headers: { "X-Token": TOKEN } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Образец не найден");
    audioEl = new Audio(URL.createObjectURL(await r.blob()));
    audioBtn = btn;
    btn.classList.add("playing");
    audioEl.onended = stopAudio;
    await audioEl.play();
  } catch (e) {
    stopAudio();
    toast(e.message);
  }
}

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
  const intro = `<h1>Голоса</h1>
    <p class="muted">Meeting Agent запоминает собеседников по голосу и на следующих встречах сам подписывает их по имени.
    Чтобы добавить человека, откройте встречу и нажмите на «Собеседник N» над протоколом.</p>`;
  const privacy = `<div class="card"><div class="hint">Слепки голоса — биометрические данные. Они хранятся только на ${PC}
    (${esc((S.settings || {}).notes_dir ? S.settings.notes_dir.replace(/notes$/, "voices") : "~/MeetingAgent/voices")}) и никуда не отправляются.
    Предупреждайте коллег, что вы записываете встречи и запоминаете голоса.</div></div>`;
  if (!v.available) {
    $("voices").innerHTML = `<div class="page">${intro}<div class="card"><h2>Пока недоступно</h2>
      <div class="hint">${esc(v.reason)}</div>
      <button class="ghost" id="voicesToSettings">Открыть настройки</button></div>${privacy}</div>`;
    $("voicesToSettings").onclick = () => $("settingsBtn").click();
    return;
  }
  const cards = v.voices.map((p) => `<div class="card voice" data-id="${p.id}">
      <div class="voice-head">
        ${p.has_sample ? `<button class="play big" data-play-voice="${p.id}" title="Прослушать образец">▶</button>` : '<span class="play big off" title="Нет образца">▶</span>'}
        <div class="voice-fields">
          <input type="text" data-f="name" value="${esc(p.name)}" placeholder="Имя" aria-label="Имя">
          <div class="row">
            <input type="text" data-f="email" value="${esc(p.email)}" placeholder="почта@компания.ru" aria-label="Почта">
            <input type="text" data-f="telegram" value="${esc(p.telegram)}" placeholder="ник в Telegram" aria-label="Telegram">
            <button class="ghost" data-save="${p.id}">Сохранить</button>
          </div>
        </div>
      </div>
      <div class="voice-foot">
        <span class="muted">Учтено встреч: ${p.meetings}</span>
        <span class="spacer"></span>
        <button class="link" data-sample="${p.id}">Заменить образец</button>
        <button class="link" data-merge="${p.id}"${v.voices.length < 2 ? " disabled" : ""}>Объединить с…</button>
        <button class="link danger" data-del="${p.id}">Удалить</button>
      </div>
    </div>`).join("");
  $("voices").innerHTML = `<div class="page">${intro}
    ${cards || '<div class="card"><div class="hint">Слепков пока нет.</div></div>'}${privacy}</div>`;
}

$("voicesBtn").addEventListener("click", async () => {
  show("voices");
  $("voices").innerHTML = '<div class="page"><h1>Голоса</h1><p class="muted"><span class="spinner"></span> Загружаю…</p></div>';
  await loadVoices(true);
  renderVoices();
});

const voiceById = (id) => S.voices.voices.find((x) => x.id === id);

$("voices").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.dataset.playVoice) return playAudio(`voices/sample?id=${t.dataset.playVoice}`, t);
  if (t.dataset.save) {
    const card = t.closest(".voice");
    const body = { id: t.dataset.save };
    card.querySelectorAll("[data-f]").forEach((i) => (body[i.dataset.f] = i.value));
    try {
      await api("voices/update", { method: "POST", body });
      toast("Сохранено");
      await loadVoices(true);
      if (S.current) openMeeting(S.current.id).then(() => show("voices"));
    } catch (err) {
      toast(err.message);
    }
  }
  if (t.dataset.sample) openSampleDialog(voiceById(t.dataset.sample));
  if (t.dataset.merge) {
    const p = voiceById(t.dataset.merge);
    $("mergeText").textContent = `Эмбеддинги и встречи слепка «${p.name}» перейдут в выбранный слепок, а «${p.name}» будет удалён. Используйте, если один человек оказался записан под двумя именами.`;
    $("mergeInto").innerHTML = S.voices.voices.filter((x) => x.id !== p.id)
      .map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
    $("mergeDialog").dataset.id = p.id;
    $("mergeDialog").showModal();
  }
  if (t.dataset.del) {
    const p = voiceById(t.dataset.del);
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
      return `<div class="fragment${f.voice_id === p.id ? " own" : ""}">
        <button class="play" data-frag="${i}" title="Прослушать">▶</button>
        <div><div>${esc(f.display)}${f.voice_id === p.id ? ' <span class="badge ok">этот голос</span>' : ""}</div>
        <div class="muted small">${esc(when)} · ${Math.round(f.duration)} с речи</div></div>
        <button class="ghost" data-pick="${i}">Выбрать</button></div>`;
    }).join("") || '<p class="muted">Нет встреч с образцами голоса. Они появляются у встреч, обработанных с разделением собеседников.</p>';
    $("fragmentList").onclick = async (e) => {
      const f = frs[+(e.target.dataset.frag ?? e.target.dataset.pick)];
      if (!f) return;
      if (e.target.dataset.frag !== undefined) {
        return playAudio(`voices/fragment?meeting=${encodeURIComponent(f.meeting)}&speaker=${encodeURIComponent(f.speaker)}`, e.target);
      }
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
  const text = `# ${m.title}\n\n` + (S.tab === "summary" ? m.summary : m.transcript);
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
$("recStop").addEventListener("click", () => { native("stopRecording"); setTimeout(pollStatus, 600); });

function renderRecording() {
  const rec = (S.status && S.status.recording) || {};
  const on = rec.state === "recording";
  $("recordBtn").classList.toggle("recording", on);
  $("recordBtn").querySelector(".label").textContent = on ? "Стоп" : "Записать";
  $("recBanner").hidden = !on;
  if (rec.skip_meeting && !S.skipToastShown) toast("Запись остановлена. Эту встречу Zoom приложение больше не запишет — автозапись включится на следующей.", 7000);
  S.skipToastShown = !!rec.skip_meeting;
  if ($("nextPeople").hidden !== on) renderNextPeople();  // запись началась или закончилась
  if (on) {
    const secs = rec.since ? Math.max(0, Math.floor(Date.now() / 1000 - rec.since)) : 0;
    const t = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    $("recText").textContent = `${rec.source === "zoom" ? "Запись встречи Zoom" : "Идёт запись"} · ${t}`;
  }
}

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
  const html = `<div class="page">
    <h1>Добро пожаловать в Meeting Agent</h1>
    <p class="muted">Для работы нужно один раз скачать модели. Записи и расшифровки остаются на ${PC}.</p>
    <div class="card"><h2>1. Кто пишет протоколы</h2>
      <div class="hint">Звук в любом случае расшифровывается на ${PC}. Выбор можно поменять в настройках.</div>
      ${backendRadios(s)}</div>
    <div class="card"><h2>2. Распознавание речи</h2>
      <div class="hint">Выберите модель и нажмите «Скачать». Загрузку можно прервать: при следующем запуске она продолжится с того же места. Модель можно сменить в настройках.</div>
      ${asrRadios(s)}</div>
    ${other ? `<div class="card"><h2>3. Модель для протоколов</h2>${other}</div>` : ""}
    <div class="card"><h2>${other ? 4 : 3}. Разделение собеседников — по желанию</h2>${hfSetupHtml(s, st)}</div>
    <div class="card"><h2>${other ? 5 : 4}. Разрешения ${IS_WIN ? "Windows" : "macOS"}</h2>
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
  $("setupDone").onclick = () => { S.setupDismissed = true; show("meeting"); };
  $("setupLater").onclick = () => { S.setupDismissed = true; show("meeting"); };
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
    "local": "Бесплатно, ничего не уходит в интернет. Нужна модель Qwen (~4,3 ГБ), протокол часовой встречи — 2–4 минуты.",
    "claude-code": s.claude_code_path
      ? "Без доплаты, расходует лимиты вашей подписки Claude. Протоколы точнее."
      : "Нужен установленный Claude Code с входом по подписке — сейчас он не найден.",
    "claude": s.anthropic_key_set ? "Оплата за запросы, примерно 0,2–0,4 $ за часовую встречу." : "Нужен платный ключ API — выберите этот вариант, и под ним появится поле для ключа.",
    "gemini": s.gemini_key_set ? `Модель ${s.gemini_model}. У моделей Flash есть бесплатный лимит — можно без оплаты, только с ключом.`
      : "Нужен ключ Gemini API — выберите этот вариант, и под ним появится поле для ключа.",
    "openai": s.openai_key_set ? `Модель ${s.openai_model}, оплата за запросы по тарифу OpenAI (отдельно от подписки ChatGPT Plus).`
      : "Нужен платный ключ OpenAI API — выберите этот вариант, и под ним появится поле для ключа.",
  };
  const cloud = { "claude-code": "Anthropic", "claude": "Anthropic", "gemini": "Google", "openai": "OpenAI" };
  return Object.entries(s.backends).map(([k, v]) => `<label class="radio${k === s.llm_backend ? " selected" : ""}">
    <input type="radio" name="backend" value="${k}"${k === s.llm_backend ? " checked" : ""}>
    <div><div class="r-title">${esc(v)}</div><div class="r-desc">${esc(desc[k] || "")}</div>
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
const GEMINI_PRESETS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"];

function geminiNote(id) {
  if (id.includes("flash")) return " — есть бесплатный лимит";
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
        <button class="ghost" type="button" data-key-save="${key.field}">Сохранить</button></div></div>
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
      <div style="flex:1"><div class="r-title">${esc(e.title)} ${badge}</div><div class="r-desc">${esc(e.desc)}</div>${dl}</div></label>`;
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
      toast("Модель Gemini: " + sel.value);
      rerender();
    } catch (e) {
      toast(e.message);
    }
  }));
  root.querySelectorAll("[data-check-hf]").forEach((b) => b.addEventListener("click", checkHf));
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

function renderSettings() {
  const s = S.settings, st = S.status;
  if (!s) return;
  $("settings").innerHTML = `<div class="page">
    <h1>Настройки</h1>
    <div class="card"><h2>Кто пишет протоколы</h2>${backendRadios(s)}
      <div class="field"><label for="sTemplate">Шаблон по умолчанию</label>
        <select id="sTemplate">${s.templates.map((t) => `<option value="${esc(t.id)}"${t.id === s.template ? " selected" : ""}>${esc(t.title)}</option>`).join("")}</select></div>
    </div>

    <div class="card"><h2>Умный поиск</h2>
      <div class="hint">Вопросы по встречам обычными словами (Enter в поле поиска). Кто формулирует ответ:</div>
      ${Object.entries(s.ask_backends).map(([k, v]) => `<label class="radio${k === s.ask_backend ? " selected" : ""}">
        <input type="radio" name="askBackend" value="${k}"${k === s.ask_backend ? " checked" : ""}>
        <div><div class="r-title">${esc(v)}</div><div class="r-desc">${{
          "claude-code": "Без доплаты, по подписке Claude. Найденные фрагменты встреч уходят в облако Anthropic.",
          "claude": "Нужен ключ Claude API (ниже, в разделе «Claude API»). Фрагменты уходят в облако Anthropic.",
          "gemini": "Нужен ключ Gemini API (ниже, в разделе «Gemini и ChatGPT»). Фрагменты уходят в облако Google.",
          "openai": "Нужен ключ OpenAI API (ниже, в разделе «Gemini и ChatGPT»). Фрагменты уходят в облако OpenAI.",
          "local": "Ничего не уходит в интернет. Ответ — 20–60 секунд, качество скромнее.",
        }[k] || ""}</div></div></label>`).join("")}
    </div>

    <div class="card"><h2>Распознавание речи</h2>
      <div class="hint">Чем превращать звук в текст. Всё работает на ${PC}.</div>
      ${asrRadios(s)}
      <label class="check"><input type="checkbox" id="sLive"${s.live_enabled ? " checked" : ""}> Распознавать во время встречи — после встречи протокол готов быстрее</label>
      <div class="hint">Работает с ${IS_WIN ? "GigaAM" : "GigaAM и Whisper turbo"}, с пониженным приоритетом, чтобы не мешать Zoom. От батареи — не работает: всё досчитается после встречи.</div>
      <div class="field"><label for="sGlossary">Глоссарий</label>
        <div class="hint">Термины, имена и аббревиатуры, которые звучат на ваших встречах, — через запятую. Сильно помогает распознаванию.</div>
        <textarea id="sGlossary">${esc(s.glossary.join(", "))}</textarea></div>
    </div>

    <div class="card"><h2>Разделение собеседников</h2>
      <div class="hint">Подписывает собеседников «Собеседник 1», «Собеседник 2»… Ваша речь всегда подписана «Я». Нужен бесплатный токен Hugging Face и согласие с условиями двух моделей:
        <a href="https://huggingface.co/pyannote/speaker-diarization-3.1" target="_blank">speaker-diarization-3.1</a> и
        <a href="https://huggingface.co/pyannote/segmentation-3.0" target="_blank">segmentation-3.0</a> (кнопка «Agree»).</div>
      <label class="check"><input type="checkbox" id="sDiar"${s.diarization_enabled ? " checked" : ""}> Разделять собеседников по голосам</label>
      <label class="check"><input type="checkbox" id="sVoices"${s.voices_enabled ? " checked" : ""}${s.diarization_enabled ? "" : " disabled"}> Узнавать собеседников по голосу (раздел «Голоса»)</label>
      <label class="check"><input type="checkbox" id="sGuess"${s.names_guess ? " checked" : ""}${s.diarization_enabled ? "" : " disabled"}> Угадывать имена по разговору — отмечаются звёздочкой: «Олег*»</label>
      <div class="field"><label for="sHf">Токен Hugging Face ${s.hf_token_set ? '<span class="badge ok">сохранён</span>' : ""}</label>
        <div class="row"><input type="password" id="sHf" placeholder="${s.hf_token_set ? "•••••• — чтобы заменить, вставьте новый" : "hf_…  — huggingface.co/settings/tokens, тип Read"}" autocomplete="off">
        <button class="ghost" id="sHfSave">Сохранить</button><button class="ghost" id="sHfCheck"${s.hf_token_set ? "" : " disabled"}>Проверить</button></div>
        <div id="sHfResult" class="hint"></div></div>
    </div>

    <div class="card"><h2>Claude API</h2>
      <div class="hint">Нужен только для варианта «Claude API по ключу». Ключ создаётся на platform.claude.com, оплата отдельно от подписки.</div>
      <div class="field"><label for="sKey">Ключ API ${s.anthropic_key_set ? '<span class="badge ok">сохранён</span>' : ""}</label>
        <div class="row"><input type="password" id="sKey" placeholder="${s.anthropic_key_set ? "•••••• — чтобы заменить, вставьте новый" : "sk-ant-…"}" autocomplete="off">
        <button class="ghost" id="sKeySave">Сохранить</button>${s.anthropic_key_set ? '<button class="ghost" id="sKeyDel">Удалить</button>' : ""}</div></div>
      <div class="hint">Claude Code: ${s.claude_code_path ? `<span class="badge ok">найден</span> ${esc(s.claude_code_path)}` : '<span class="badge warn">не установлен</span>'}</div>
    </div>

    <div class="card"><h2>Gemini и ChatGPT</h2>
      <div class="hint">Нужны для вариантов «Gemini API по ключу» и «ChatGPT (OpenAI API) по ключу» — в протоколах и в умном поиске. Оплата за запросы, отдельно от подписок Gemini и ChatGPT Plus.</div>
      <div class="field"><label for="sGemini">Ключ Gemini API ${s.gemini_key_set ? '<span class="badge ok">сохранён</span>' : ""}</label>
        <div class="hint">Создаётся на <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>.</div>
        <div class="row"><input type="password" id="sGemini" placeholder="${s.gemini_key_set ? "•••••• — чтобы заменить, вставьте новый" : "AIza…"}" autocomplete="off">
        <button class="ghost" id="sGeminiSave">Сохранить</button>${s.gemini_key_set ? '<button class="ghost" id="sGeminiDel">Удалить</button>' : ""}</div></div>
      ${s.gemini_key_set ? geminiModelField(s) : ""}
      <div class="field"><label for="sOpenai">Ключ OpenAI API ${s.openai_key_set ? '<span class="badge ok">сохранён</span>' : ""}</label>
        <div class="hint">Создаётся на <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com/api-keys</a>.</div>
        <div class="row"><input type="password" id="sOpenai" placeholder="${s.openai_key_set ? "•••••• — чтобы заменить, вставьте новый" : "sk-…"}" autocomplete="off">
        <button class="ghost" id="sOpenaiSave">Сохранить</button>${s.openai_key_set ? '<button class="ghost" id="sOpenaiDel">Удалить</button>' : ""}</div></div>
      <div class="field"><label for="sOpenaiModel">Модель ChatGPT</label>
        <input type="text" id="sOpenaiModel" value="${esc(s.openai_model)}" placeholder="gpt-5"></div>
      <div class="hint">Если модель устареет, приложение само возьмёт актуальную.</div>
    </div>

    <div class="card"><h2>Запись</h2>
      <label class="check"><input type="checkbox" id="sAuto"${s.auto_record ? " checked" : ""}> Записывать встречи Zoom автоматически</label>
      <div class="field"><label for="sTitles">Заголовки окна встречи Zoom</label>
        <div class="hint">По ним приложение понимает, что встреча началась. Через запятую.</div>
        <input type="text" id="sTitles" value="${esc(s.window_titles.join(", "))}"></div>
      <label class="check"><input type="checkbox" id="sKeepAudio"${s.keep_audio ? " checked" : ""}> Хранить аудио после обработки (нужно, чтобы позже заново расшифровать)</label>
      <div class="hint">Заметки: ${esc(s.notes_dir)} · <button class="link" id="sReveal">Открыть папку</button></div>
    </div>

    <div class="card"><h2>Модели</h2>${modelsHtml(st ? st.models : {})}</div>

    <div class="card"><h2>Запись встреч и закон</h2>
      <div class="hint">Приложение не уведомляет участников о записи. Предупреждайте их и сверяйтесь с правилами компании.</div></div>
  </div>`;

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
  $("sTemplate").onchange = (e) => save({ template: e.target.value });
  $("sGlossary").onchange = (e) => save({ glossary: list(e.target.value) });
  $("sTitles").onchange = (e) => save({ window_titles: list(e.target.value) });
  $("sDiar").onchange = async (e) => { await save({ diarization_enabled: e.target.checked }); renderSettings(); loadVoices(true); };
  $("sVoices").onchange = (e) => { save({ voices_enabled: e.target.checked }); loadVoices(true); };
  $("sGuess").onchange = (e) => save({ names_guess: e.target.checked });
  $("sLive").onchange = (e) => save({ live_enabled: e.target.checked });
  $("settings").querySelectorAll('input[name="askBackend"]').forEach((r) => r.addEventListener("change", async () => {
    if (await save({ ask_backend: r.value }, "Умный поиск: " + s.ask_backends[r.value])) renderSettings();
  }));
  $("sGeminiSave").onclick = async () => {
    const v = $("sGemini").value.trim();
    if (v && await save({ gemini_key: v }, "Ключ Gemini сохранён")) { S.geminiModels = undefined; renderSettings(); }
  };
  if ($("sGeminiDel")) $("sGeminiDel").onclick = async () => { if (await save({ gemini_key: "" }, "Ключ Gemini удалён")) renderSettings(); };
  $("sOpenaiSave").onclick = async () => {
    const v = $("sOpenai").value.trim();
    if (v && await save({ openai_key: v }, "Ключ OpenAI сохранён")) renderSettings();
  };
  if ($("sOpenaiDel")) $("sOpenaiDel").onclick = async () => { if (await save({ openai_key: "" }, "Ключ OpenAI удалён")) renderSettings(); };
  $("sOpenaiModel").onchange = (e) => { if (e.target.value.trim()) save({ openai_model: e.target.value }, "Модель ChatGPT: " + e.target.value.trim()); };
  $("sAuto").onchange = (e) => { save({ auto_record: e.target.checked }); native("reloadSettings"); };
  $("sKeepAudio").onchange = (e) => save({ keep_audio: e.target.checked });
  $("sReveal").onclick = () => api("reveal", { method: "POST", body: {} });
  $("sHfSave").onclick = async () => {
    const v = $("sHf").value.trim();
    if (v && await save({ hf_token: v }, "Токен сохранён")) { renderSettings(); checkHf(); }
  };
  $("sHfCheck").onclick = checkHf;
  $("sKeySave").onclick = async () => {
    const v = $("sKey").value.trim();
    if (v && await save({ anthropic_key: v }, "Ключ сохранён")) renderSettings();
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
})();
