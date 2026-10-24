"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmtTime = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const fmtTs = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtNum = (n) => (n ?? 0).toLocaleString();
const LANGUAGES = { en: "English", hi: "Hindi / Hinglish" };
const fmtBytes = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round((n || 0) / 1e3)} KB`);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Foresight"] = "1";
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* not json */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r.text();
}

async function stream(path, body, onEvent) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Foresight": "1" }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      const ev = JSON.parse(line);
      if (ev.type === "error") throw new Error(ev.error);
      onEvent(ev);
    }
  }
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), isErr ? 7000 : 3500);
}

// Minimal, escape-first Markdown renderer (no external libraries; the app works offline).
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\[((?:\d{1,2}|T)(?:\s*,\s*(?:\d{1,2}|T))*)\]/g, (_, list) =>
      list.split(/\s*,\s*/).map((n) => `<sup class="cite" data-n="${n}">${n}</sup>`).join(""));
}
// Small models sometimes typeset money as LaTeX ($\$52,000$). On such lines, drop the math delimiters.
const delatex = (s) => s.split("\n").map((line) => line.includes("\\$") ? line.replace(/(?<!\\)\$/g, "").replace(/\\\$/g, "$") : line).join("\n");
function md(src) {
  const lines = esc(delatex(src || "")).split(/\r?\n/);
  let html = "", list = null, para = [], inCode = false, code = [];
  const flush = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  const open = (kind) => { if (list !== kind) { close(); html += `<${kind}>`; list = kind; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (line.trimStart().startsWith("```")) {
      if (inCode) { html += `<pre><code>${code.join("\n")}</code></pre>`; code = []; inCode = false; }
      else { flush(); close(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(raw); continue; }
    let m;
    if (!line.trim()) { flush(); close(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); close(); const lv = Math.min(m[1].length + 1, 4); html += `<h${lv}>${inline(m[2])}</h${lv}>`; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) { flush(); close(); html += "<hr>"; continue; }
    if ((m = line.match(/^&gt;\s?(.*)$/))) { flush(); close(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }
    if ((m = line.match(/^\s*[-*•]\s+\[([ xX])\]\s+(.*)$/))) { flush(); open("ul"); html += `<li class="task">${m[1].trim() ? "☑" : "☐"} ${inline(m[2])}</li>`; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { flush(); open("ul"); html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flush(); open("ol"); html += `<li>${inline(m[1])}</li>`; continue; }
    para.push(line.trim());
  }
  if (inCode) html += `<pre><code>${code.join("\n")}</code></pre>`;
  flush(); close();
  return html;
}

// A playable moment: ▶ chip that plays [t_start, t_end] of a stored recording (see the moment player below).
const playAttrs = (m) => (m?.url ? `data-src="${esc(m.url)}" data-t="${m.t_start}" data-end="${m.t_end}" title="Play this moment"` : "");
const momentLabel = (m) => `<span class="pi">${m.url ? "▶" : "♪"}</span> ${fmtTs(m.t_start)}`;
// "Ask about this moment": Gemma hears the clip along with the question.
const askChip = (m, title) => (m?.url ? `<span class="chip ask" title="Ask Gemma about this moment (it listens to the audio)"
  data-ask="${esc(JSON.stringify({ media_id: m.media_id, t_start: m.t_start, t_end: m.t_end, title }))}">Ask</span>` : "");
// A cited picture: its thumbnail, opening the full image.
const isPicture = (m) => m?.kind === "image";
const pictureChip = (m, label) => `<a class="chip picture" href="${esc(m.url)}" target="_blank" rel="noopener" title="Open the picture">`
  + `<img src="${esc(m.thumb)}" alt="" loading="lazy">${label}</a>`;
const mediaChips = (m, title, label) => (isPicture(m) ? pictureChip(m, label)
  : `<span class="chip moment" ${playAttrs(m)}>${momentLabel(m)}${label ? ` · ${label}` : ""}</span>${askChip(m, title)}`);
// What Gemma heard in the cited recordings, word for word (it can differ from their transcripts).
const heardNote = (ev) => `<div class="listened small muted">${Object.entries(ev.texts).map(([n, t]) =>
  `Heard in [${n}]: “${esc(t.length > 240 ? t.slice(0, 240) + "…" : t)}”`).join("<br>")}</div>`;
const gemmaNote = (ev) => [ev.listened?.length && `Gemma listened to ${ev.listened.map((n) => `[${n}]`).join(" ")}`,
  ev.looked?.length && `Gemma looked at ${ev.looked.map((n) => `[${n}]`).join(" ")}`].filter(Boolean).join(" · ");

function renderFocus(el, m, clear) {
  el.hidden = !m;
  el.innerHTML = m ? `<span>Gemma will listen to <b>${esc(m.title)}</b> · ${fmtTs(m.t_start)}–${fmtTs(m.t_end)}</span>
    <button type="button" class="btn icon" title="Stop using this moment">✕</button>` : "";
  if (m) $("button", el).onclick = clear;
}

function sourcesHtml(citations) {
  if (!citations?.length) return "";
  const groups = new Map();  // several chunks of one file share a chip; each audio moment gets its own
  citations.forEach((c) => {
    const key = c.media && !isPicture(c.media) ? `m${c.media.media_id}@${c.media.t_start}` : c.source_id;
    const g = groups.get(key) || []; g.push(c); groups.set(key, g);
  });
  return `<div class="sources">${[...groups.values()].map((g) => {
    const label = `[${g.map((c) => c.n).join(",")}] ${esc(g[0].title)}`;
    const m = g.find((c) => c.media)?.media;
    if (isPicture(m)) return pictureChip(m, label).replace("<a ", `<a data-n="${g[0].n}" `);
    return m ? `<span class="chip moment" data-n="${g[0].n}" ${playAttrs(m)}>${momentLabel(m)} · ${label}</span>${askChip(m, g[0].title)}`
      : `<span class="chip" data-n="${g[0].n}">${label}</span>`;
  }).join("")}</div>`;
}

// "Ask" on a moment: the page that can take it (Chat, a meeting's Ask box) registers state.focusHandler;
// anywhere else the moment is carried over to Chat.
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip.ask[data-ask]");
  if (!chip) return;
  e.preventDefault();
  e.stopPropagation();
  const m = JSON.parse(chip.dataset.ask);
  if (state.focusHandler) state.focusHandler(m);
  else { sessionStorage.setItem("focus", chip.dataset.ask); location.hash = "#/chat"; }
}, true);

// One shared player for moments cited anywhere (chat, live cards, briefings, search results).
const moment = { audio: new Audio(), chip: null, end: 0 };
function stopMoment() {
  moment.audio.pause();
  if (moment.chip) { moment.chip.classList.remove("playing"); const pi = $(".pi", moment.chip); if (pi) pi.textContent = "▶"; }
  moment.chip = null;
}
moment.audio.addEventListener("timeupdate", () => { if (moment.chip && moment.audio.currentTime >= moment.end) stopMoment(); });
moment.audio.addEventListener("ended", stopMoment);
document.addEventListener("click", async (e) => {
  const chip = e.target.closest(".chip[data-src]");
  if (!chip) return;
  e.preventDefault();
  e.stopPropagation();
  const again = moment.chip === chip;
  stopMoment();
  if (again) return;  // a second click stops it
  moment.chip = chip;
  moment.end = Number(chip.dataset.end) + 0.3;
  chip.classList.add("playing");
  $(".pi", chip).textContent = "■";
  const src = new URL(chip.dataset.src, location.href).href;
  if (moment.audio.src !== src) {
    moment.audio.src = src;
    await new Promise((ok, fail) => { moment.audio.onloadedmetadata = ok; moment.audio.onerror = () => fail(new Error("this audio is no longer stored")); })
      .catch((err) => { toast(`Could not play the moment: ${err.message}`, true); stopMoment(); });
    if (moment.chip !== chip) return;
  }
  moment.audio.currentTime = Math.max(0, Number(chip.dataset.t) - 0.3);
  moment.audio.play().catch((err) => { toast(`Could not play the moment: ${err.message}`, true); stopMoment(); });
}, true);

// Citation hover cards: any element carrying ._cites resolves [n] markers inside it.
const tip = $("#tooltip");
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("sup.cite, .sources .chip[data-n]");
  if (!el) { tip.hidden = true; return; }
  let host = el;
  while (host && !host._cites) host = host.parentElement;
  const n = el.dataset.n;
  if (n === "T") { tip.innerHTML = "<b>Live transcript</b>The current meeting's transcript."; }
  else {
    const c = host?._cites?.find((x) => String(x.n) === n);
    if (!c) { tip.hidden = true; return; }
    tip.innerHTML = `<b>[${c.n}] ${esc(c.title)}</b>${esc(c.snippet || "")}${c.similarity ? `\n\nsimilarity ${c.similarity}` : ""}`;
  }
  const r = el.getBoundingClientRect();
  tip.hidden = false;
  tip.style.left = Math.min(r.left, innerWidth - 400) + "px";
  tip.style.top = (r.bottom + 6 + tip.offsetHeight > innerHeight ? r.top - tip.offsetHeight - 6 : r.bottom + 6) + "px";
});

// ------------------------------------------------------------------ live events
const listeners = new Set();
const state = { activeMeeting: null, focusHandler: null };

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "meeting") {
      if (ev.status === "recording") setActive(ev.id);
      else if (state.activeMeeting === ev.id && ["finishing", "done", "error"].includes(ev.status)) setActive(null);
    }
    if (ev.type === "source" && ev.status === "error") toast(`Could not index ${ev.title || "a source"}: ${ev.error}`, true);
    listeners.forEach((fn) => fn(ev));
  };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

function setActive(id) {
  state.activeMeeting = id;
  $("#rec-indicator").hidden = !id;
}

// ------------------------------------------------------------------ first-run check (doctor.py)
function setupList(r, onlyProblems = false) {
  const rows = r.checks.filter((c) => !onlyProblems || !c.ok);
  return `<div class="setup-list">${rows.map((c) => `<div class="setup-row ${c.ok ? "ok" : c.required ? "bad" : "warn"}">
    <span class="mark" aria-hidden="true">${c.ok ? "✓" : c.required ? "✕" : "!"}</span>
    <div class="grow"><div>${esc(c.label)}${!c.ok && !c.required ? ' <span class="small muted">(optional)</span>' : ""}</div>
      <div class="small muted">${esc(c.detail)}</div>
      ${c.fix ? `<div class="small fix">${c.fix.startsWith("ollama pull") ? `Run <code>${esc(c.fix)}</code>` : esc(c.fix)}</div>` : ""}</div></div>`).join("")}</div>`;
}

async function showSetup(el, { onlyProblems = false } = {}) {
  el.innerHTML = `<div class="progress"><span class="spinner"></span>Checking this PC…</div>`;
  try {
    const [r, job] = await Promise.all([api("/api/setup"), api("/api/setup/progress")]);
    if (onlyProblems && !job.running && r.checks.every((c) => c.ok || !c.required)) { el.hidden = true; return r; }
    el.hidden = false;
    const intro = r.installed
      ? "Foresight Wing runs Gemma on this PC through Ollama. Set up now installs Ollama and downloads the two models, once; everything after that works offline."
      : "Foresight Wing needs these before it can work fully. Everything runs on this PC; run <code>setup.bat</code> in the Foresight Wing folder to fix most of them in one step.";
    const button = r.plan.length && (r.installed || r.plan.every((s) => !s.startsWith("install")))
      ? `<button class="btn sm primary" data-setup-run>Set up now${r.download_gb ? ` · downloads about ${r.download_gb} GB` : ""}</button>` : "";
    el.innerHTML = (onlyProblems ? `<h2>Finish setting up</h2><p class="small muted">${intro}</p>` : "")
      + setupList(r, onlyProblems) + `<div class="setup-job" hidden></div>`
      + `<div class="row" style="margin-top:10px;gap:8px">${button}<button class="btn sm" data-recheck>Check again</button></div>`;
    $("[data-recheck]", el).onclick = () => showSetup(el, { onlyProblems });
    const jobEl = $(".setup-job", el);
    const follow = () => {  // the downloads run on the server; show their progress until they finish
      const timer = setInterval(async () => {
        if (!document.body.contains(el)) { clearInterval(timer); return; }
        const s = await api("/api/setup/progress").catch(() => null);
        if (!s) return;
        jobEl.hidden = false;
        const pct = s.total ? Math.round((100 * s.done) / s.total) : null;
        jobEl.innerHTML = s.error ? `<p class="status-error">Setup stopped: ${esc(s.error)}</p>`
          : `<div class="progress"><span class="spinner"></span>${esc(s.step)}${pct !== null ? ` · ${pct}% of ${(s.total / 1e9).toFixed(1)} GB` : ""}</div>`
            + (pct !== null ? `<div class="setup-bar"><i style="width:${pct}%"></i></div>` : "");
        if (!s.running) { clearInterval(timer); if (!s.error) showSetup(el, { onlyProblems }); }
      }, 1000);
    };
    const run = $("[data-setup-run]", el);
    if (run) run.onclick = async () => { run.disabled = true; await api("/api/setup/run", { method: "POST" }); follow(); };
    if (job.running) { if (run) run.disabled = true; follow(); }
    return r;
  } catch (err) { el.innerHTML = `<p class="status-error">${esc(err.message)}</p>`; }
}

// ------------------------------------------------------------------ router
let cleanups = [];
const onCleanup = (fn) => cleanups.push(fn);
const listen = (fn) => { listeners.add(fn); onCleanup(() => listeners.delete(fn)); };

const routes = { home: renderHome, meeting: renderMeeting, knowledge: renderKnowledge, chat: renderChat, settings: renderSettings };

function route() {
  cleanups.forEach((fn) => fn());
  cleanups = [];
  tip.hidden = true;
  const [name, arg] = (location.hash.replace(/^#\/?/, "") || "home").split("/");
  $$(".rail a[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  (routes[name] || renderHome)(arg).catch((err) => {
    view.innerHTML = `<div class="page"><div class="card"><h2>Something went wrong</h2><p class="muted">${esc(err.message)}</p></div></div>`;
  });
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ shared pieces
function startControls(prefs) {
  return `
    <div class="row" style="flex-wrap:wrap;gap:14px">
      <label class="toggle"><input type="checkbox" id="opt-mic" ${prefs.capture_mic ? "checked" : ""}> Microphone (you)</label>
      <label class="toggle"><input type="checkbox" id="opt-sys" ${prefs.capture_system ? "checked" : ""}> System audio (others on the call)</label>
      <label class="toggle" title="Hindi / Hinglish meetings are transcribed live by Whisper, then Gemma re-transcribes every line afterwards">Language
        <select id="opt-lang" style="width:auto">${Object.entries(LANGUAGES).map(([v, name]) => `<option value="${v}" ${prefs.meeting_language === v ? "selected" : ""}>${name}</option>`).join("")}</select></label>
    </div>
    <div class="row" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn rec" id="btn-start">● Start meeting</button>
      <label class="btn" for="import-file">⇪ Import recording</label>
      <input type="file" id="import-file" accept=".wav,.mp3,.m4a,.ogg,.flac,.webm,.mp4,.aac,.opus" hidden>
    </div>`;
}

function wireStartControls() {
  $("#opt-lang").onchange = (e) => api("/api/settings", { method: "PUT", body: { meeting_language: e.target.value } }).catch((err) => toast(err.message, true));
  $("#btn-start").onclick = async () => {
    const btn = $("#btn-start");
    btn.disabled = true; btn.textContent = "Loading speech model…";
    try {
      const { id } = await api("/api/meetings/start", { method: "POST", body: { mic: $("#opt-mic").checked, system: $("#opt-sys").checked, language: $("#opt-lang").value } });
      setActive(id);
      location.hash = `#/meeting/${id}`;
    } catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = "● Start meeting"; }
  };
  $("#import-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("title", file.name.replace(/\.[^.]+$/, ""));
    form.append("language", $("#opt-lang").value);
    try {
      const { id } = await api("/api/meetings/import", { method: "POST", form });
      toast("Transcribing recording on this PC…");
      location.hash = `#/meeting/${id}`;
    } catch (err) { toast(err.message, true); }
  };
}

function meetingRows(ms) {
  if (!ms.length) return `<div class="empty">No meetings yet. Start one, or import a recording.</div>`;
  return ms.map((m) => `
    <div class="list-item" data-id="${m.id}">
      <div class="grow"><div class="title">${esc(m.title)}</div>
        <div class="muted small">${fmtTime(m.started_at)} · ${m.n_segments} transcript lines</div></div>
      ${m.status === "recording" ? '<span class="chip err">recording</span>' : m.status === "importing" ? '<span class="chip warn">transcribing</span>' : ""}
      ${m.has_notes ? '<span class="chip ok">enhanced</span>' : ""}
    </div>`).join("");
}
function wireMeetingRows(root) {
  $$(".list-item[data-id]", root).forEach((el) => (el.onclick = () => (location.hash = `#/meeting/${el.dataset.id}`)));
}

// ------------------------------------------------------------------ daily briefings
const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function startsIn(t) {
  const mins = Math.round((t * 1000 - Date.now()) / 6e4);
  if (mins <= 0) return "now";
  if (mins < 60) return `in ${mins} min`;
  const day = new Date(t * 1000).toDateString();
  if (day === new Date().toDateString()) return "today";
  return day === new Date(Date.now() + 864e5).toDateString() ? "tomorrow" : new Date(t * 1000).toLocaleDateString([], { weekday: "short" });
}

function briefingCard(ev, canStart) {
  const b = ev.briefing;
  const people = ev.attendees.slice(0, 5).join(", ") + (ev.attendees.length > 5 ? ` +${ev.attendees.length - 5}` : "");
  const body = ev.status === "ready"
    ? (b.summary ? `<div class="md">${md(b.summary)}</div>` : `<p class="small muted">Nothing in your knowledge sources or past meetings matches this event yet.</p>`)
    : ev.status === "error" ? `<p class="small status-error">Could not prepare this briefing: ${esc(ev.error)}</p>`
    : `<div class="progress"><span class="spinner"></span> Gemma is reading your sources for this meeting…</div>`;
  return `
    <div class="brief" data-event="${ev.id}">
      <div class="brief-head">
        <div class="brief-when"><b>${fmtClock(ev.starts_at)}</b><span>${startsIn(ev.starts_at)}</span></div>
        <div class="grow"><div class="title">${esc(ev.title)}</div>
          <div class="small muted">${fmtClock(ev.starts_at)}–${fmtClock(ev.ends_at)}${ev.location ? ` · ${esc(ev.location)}` : ""}${people ? ` · ${esc(people)}` : ""}</div></div>
        ${ev.status === "pending" ? "" : `<button class="btn icon" data-brief-refresh="${ev.id}" title="Write this briefing again (e.g. after adding sources)">↻</button>`}
        ${canStart ? `<button class="btn sm rec" data-brief-start="${ev.id}">● Start this meeting</button>` : ""}
      </div>
      ${body}${b ? sourcesHtml(b.citations) : ""}
    </div>`;
}

async function loadUpcoming(meetingActive) {
  const box = $("#upcoming-body");
  if (!box) return;
  let data;
  try { data = await api("/api/briefings"); }
  catch (err) { box.innerHTML = `<p class="small status-error">${esc(err.message)}</p>`; return; }
  if (!box.isConnected) return;
  if (!data.calendars) {
    box.innerHTML = `<p class="small muted">Add a calendar (an .ics file or a subscription URL) in <a href="#/settings">Settings</a> to get a briefing before each meeting, built from your own files and notes. No Google sign-in needed.</p>`;
    return;
  }
  if (!data.events.length) { box.innerHTML = `<p class="small muted">Nothing on your calendars in the next 24 hours.</p>`; return; }
  box.innerHTML = data.events.map((ev) => briefingCard(ev, !meetingActive)).join("");
  data.events.forEach((ev) => {
    const el = $(`.brief[data-event="${ev.id}"]`, box);
    const cites = ev.briefing?.citations || [];
    el._cites = cites;  // hover cards for [n] markers and source chips
    $$(".sources .chip", el).forEach((chip) => {
      const c = cites.find((x) => String(x.n) === chip.dataset.n);
      if (c?.meeting_id) { chip.classList.add("link"); chip.onclick = () => (location.hash = `#/meeting/${c.meeting_id}`); }
    });
    const start = $("[data-brief-start]", el);
    if (start) start.onclick = async () => {
      start.disabled = true; start.textContent = "Loading speech model…";
      try {
        const { id } = await api("/api/meetings/start", { method: "POST", body: { title: ev.title, mic: $("#opt-mic")?.checked ?? true, system: $("#opt-sys")?.checked ?? true, language: $("#opt-lang")?.value || "en" } });
        setActive(id);
        location.hash = `#/meeting/${id}`;
      } catch (err) { toast(err.message, true); start.disabled = false; start.textContent = "● Start this meeting"; }
    };
    const again = $("[data-brief-refresh]", el);
    if (again) again.onclick = async () => {
      try { await api(`/api/briefings/${ev.id}/refresh`, { method: "POST" }); } catch (err) { toast(err.message, true); }
      loadUpcoming(meetingActive);
    };
  });
}

// ------------------------------------------------------------------ home
async function renderHome() {
  const [st, ms, stats] = await Promise.all([api("/api/status"), api("/api/meetings"), api("/api/stats")]);
  setActive(st.meeting.active ? st.meeting.meeting_id : null);
  const llm = st.llm;
  const health = llm.ok
    ? `<span class="chip ok">Ollama ${esc(llm.version)}</span>
       <span class="chip ${llm.gen_ready ? "ok" : "err"}">${esc(llm.gen_model)} ${llm.gen_ready ? "ready" : "missing"}</span>
       <span class="chip ${llm.embed_ready ? "ok" : "err"}">${esc(llm.embed_model)} ${llm.embed_ready ? "ready" : "missing"}</span>
       <span class="chip ok">${esc(st.models.asr)}</span>`
    : `<span class="chip err">${esc(llm.error)}</span>`;
  const tokens7 = stats.days.reduce((a, d) => a + d.prompt + d.completion, 0);
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.now() - i * 864e5);
    const key = d.toLocaleDateString("en-CA");
    const row = stats.days.find((x) => x.day === key);
    days.push({ label: d.toLocaleDateString([], { weekday: "short" }), v: row ? row.prompt + row.completion : 0 });
  }
  const max = Math.max(1, ...days.map((d) => d.v));

  view.innerHTML = `
    <div class="page">
      <div class="card hero">
        <span class="orb"></span>
        <div class="grow">
          <h1>Be completely present.</h1>
          <p>Foresight Wing listens on this PC, transcribes locally, and turns your shorthand into polished notes, grounded in your own files.</p>
          ${st.meeting.active ? `<a class="btn rec" href="#/meeting/${st.meeting.meeting_id}">● Return to the meeting in progress</a>` : startControls(st.prefs)}
        </div>
      </div>
      <div class="card setup-card" id="h-setup" hidden></div>
      <div class="row" style="flex-wrap:wrap;margin:14px 0">${health}</div>
      <div class="card" id="upcoming" style="margin-bottom:14px">
        <div class="row" style="flex-wrap:wrap;margin-bottom:10px"><h2 class="grow" style="margin:0">Upcoming</h2><span class="small muted">Daily Briefings · next 24 hours</span></div>
        <div id="upcoming-body"><div class="progress"><span class="spinner"></span> Loading your calendar…</div></div>
      </div>
      <div class="grid cols-4">
        <div class="card stat"><span class="muted">Knowledge sources</span><b>${fmtNum(st.counts.ready)}</b><span class="small muted">${st.queue ? `${st.queue} indexing…` : `${fmtNum(st.counts.sources)} total`}${st.counts.errors ? ` · ${st.counts.errors} errors` : ""}</span></div>
        <div class="card stat"><span class="muted">Indexed chunks</span><b>${fmtNum(st.counts.chunks)}</b><span class="small muted">EmbeddingGemma 2 vectors</span></div>
        <div class="card stat"><span class="muted">Meetings</span><b>${fmtNum(st.counts.meetings)}</b><span class="small muted">transcribed on-device</span></div>
        <div class="card stat"><span class="muted">Gemma tokens (7 days)</span><b>${fmtNum(tokens7)}</b><span class="small muted">no cloud calls</span></div>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <div class="card"><h2>Recent meetings</h2><div class="list" id="recent">${meetingRows(ms.slice(0, 8))}</div></div>
        <div class="card"><h2>On-device usage</h2>
          <div class="bars">${days.map((d) => `<div class="bar" title="${fmtNum(d.v)} tokens"><i style="height:${(d.v / max) * 100}%"></i><span>${d.label}</span></div>`).join("")}</div>
          <div class="small muted" style="margin-top:10px">${stats.by_task.map((t) => `${esc(t.task)}: ${fmtNum(t.tokens)}`).join(" · ") || "No model calls yet."}</div>
        </div>
      </div>
    </div>`;
  if (!st.meeting.active) wireStartControls();
  wireMeetingRows($("#recent"));
  const reload = debounce(route, 400);
  listen((ev) => { if (ev.type === "meeting") reload(); });
  loadUpcoming(st.meeting.active);
  showSetup($("#h-setup"), { onlyProblems: true });  // first run, or something went missing
  const reloadUpcoming = debounce(() => loadUpcoming(st.meeting.active), 500);
  listen((ev) => { if (ev.type === "briefing" || ev.type === "calendar") reloadUpcoming(); });
  const tick = setInterval(reloadUpcoming, 60000);  // keeps "in 5 min" labels and the 24-hour window current
  onCleanup(() => clearInterval(tick));
}

// ------------------------------------------------------------------ meeting
async function renderMeeting(arg) {
  if (!arg) {
    const [st, ms] = await Promise.all([api("/api/status"), api("/api/meetings")]);
    if (st.meeting.active) { location.hash = `#/meeting/${st.meeting.meeting_id}`; return; }
    view.innerHTML = `<div class="page"><div class="page-head"><h1>Meetings</h1></div>
      <div class="card">${startControls(st.prefs)}</div>
      <div class="card"><div class="list" id="all">${meetingRows(ms)}</div></div></div>`;
    wireStartControls();
    wireMeetingRows($("#all"));
    return;
  }
  const id = Number(arg);
  const [m, st] = await Promise.all([api(`/api/meetings/${id}`), api("/api/status")]);
  const recording = m.status === "recording";
  const prefs = st.prefs;
  let enhanced = m.enhanced || "";
  let tab = enhanced ? "enhanced" : "shorthand";
  const convId = `meeting-${id}-${Date.now()}`;
  const tracks = (m.media || []).filter((a) => a.url);
  const audioNote = recording ? "" : tracks.length ? "Click a line to play it"
    : (m.media || []).some((a) => a.status === "expired") ? "Audio expired" : `<a href="#/settings">Audio not kept</a>`;

  view.innerHTML = `
    <div class="page">
      <div class="page-head">
        <input class="title-input" id="m-title" value="${esc(m.title)}" aria-label="Meeting title">
        <span class="chip ${recording ? "err" : m.status === "importing" ? "warn" : "ok"}" id="m-status">${recording ? "Recording" : m.status === "importing" ? "Transcribing…" : "Done"}</span>
        ${m.language === "hi" ? `<span class="chip">${LANGUAGES.hi}</span>` : ""}
        ${recording && st.meeting.saving_audio ? `<span class="chip" title="This meeting's audio is being saved on this PC (Settings → Audio memory)">Saving audio</span>` : ""}
        <span class="timer" id="m-timer"></span>
        <div class="meters" id="m-meters" ${recording ? "" : "hidden"}>
          <span class="meter">You <span><i data-src="You"></i></span></span>
          <span class="meter">Others <span><i data-src="Others"></i></span></span>
        </div>
        <span class="grow"></span>
        <button class="btn rec" id="m-stop" ${recording ? "" : "hidden"}>■ Stop</button>
        <button class="btn primary" id="m-enhance" ${recording ? "hidden" : ""}>✦ Auto-enhance</button>
        <a class="btn" href="/api/meetings/${id}/export">Export .md</a>
        <button class="btn icon danger" id="m-delete" title="Delete meeting">🗑</button>
      </div>
      ${tracks.length ? `<div class="card player" id="m-player">${tracks.map((a) => `
        <div class="track"><span class="who ${esc(a.channel.toLowerCase())}">${esc(a.channel)}</span>
          <audio controls preload="metadata" src="${a.url}" data-channel="${esc(a.channel)}" data-id="${a.id}"></audio></div>`).join("")}
        ${tracks[0].expires_at ? `<span class="small muted">Audio kept until ${new Date(tracks[0].expires_at * 1000).toLocaleDateString()}</span>` : ""}</div>` : ""}
      <div class="meeting-grid">
        <section class="card pane">
          <div class="pane-head"><h2 style="margin:0">Notes</h2><span class="grow"></span>
            <div class="tabs"><button data-tab="shorthand">Shorthand</button><button data-tab="enhanced">Enhanced</button></div></div>
          <textarea id="m-shorthand" placeholder="Scribble shorthand here…  e.g.\n- budget?\n- launch moved → Q3\n- Priya owns vendor list">${esc(m.shorthand)}</textarea>
          <div class="pane-body md" id="m-enhanced"></div>
          <div class="progress" id="m-progress" hidden><span class="spinner"></span><span></span></div>
        </section>
        <section class="card pane">
          <div class="pane-head" style="flex-wrap:wrap"><h2 style="margin:0">Transcript</h2><span class="muted small" id="m-count"></span>
            <span class="chip" id="m-pass" hidden></span><span class="grow"></span><span class="muted small">${audioNote}</span></div>
          <div class="pane-body" id="m-transcript"></div>
        </section>
        <aside class="card pane sidecar">
          <div class="pane-head"><h2 style="margin:0">Live assistance</h2><span class="grow"></span>
            <button class="btn sm" id="m-float" ${recording ? "" : "hidden"} title="Keep live answers in a small window on top of your call">⧉ Float</button>
            <label class="toggle small"><input type="checkbox" id="m-live" ${prefs.live_assist ? "checked" : ""}> On</label></div>
          <div class="pane-body" id="m-cards"></div>
          <div class="focus-pill" id="m-focus" hidden style="margin-top:10px"></div>
          <form class="row" id="m-ask" style="margin-top:10px"><input type="text" placeholder="Ask about this meeting or your files…" id="m-ask-input"><button class="btn sm primary">Ask</button></form>
        </aside>
      </div>
    </div>`;

  // --- notes pane
  const shorthandEl = $("#m-shorthand"), enhancedEl = $("#m-enhanced");
  const showTab = (t) => {
    tab = t;
    $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    shorthandEl.hidden = t !== "shorthand";
    enhancedEl.hidden = t !== "enhanced";
    if (t === "enhanced" && !enhanced) enhancedEl.innerHTML = `<div class="empty">Press <b>Auto-enhance</b> after the meeting to turn your shorthand + transcript into polished notes.</div>`;
  };
  const renderEnhanced = () => { enhancedEl.innerHTML = md(enhanced); };
  if (enhanced) renderEnhanced();
  $$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  showTab(tab);
  const saveShorthand = debounce(() => api(`/api/meetings/${id}`, { method: "PUT", body: { shorthand: shorthandEl.value } }).catch((e) => toast(e.message, true)), 700);
  shorthandEl.addEventListener("input", saveShorthand);
  $("#m-title").addEventListener("change", (e) => api(`/api/meetings/${id}`, { method: "PUT", body: { title: e.target.value } }));

  // --- Gemma's second pass over a Hindi / Hinglish meeting (repass.py)
  let pass = m.transcript_pass;
  const showPass = () => {
    const el = $("#m-pass");
    el.hidden = !pass;
    if (!pass) return;
    const label = { waiting: "Gemma re-transcribes after the meeting", queued: "Waiting for Gemma to re-transcribe",
      running: `Gemma is re-transcribing · ${pass.done || 0}/${pass.total || "…"}`,
      done: `Re-transcribed by Gemma · ${pass.changed || 0} line${pass.changed === 1 ? "" : "s"} improved`,
      error: "Gemma's pass failed" }[pass.status] || pass.status;
    el.className = "chip" + (pass.status === "error" ? " err" : pass.status === "done" ? " ok" : " warn");
    el.innerHTML = esc(label) + (pass.status === "error" ? ` <button title="${esc(pass.error || "")}">Try again</button>` : "");
    $("button", el)?.addEventListener("click", () => api(`/api/meetings/${id}/transcript-pass`, { method: "POST" }).catch((err) => toast(err.message, true)));
  };
  showPass();

  // --- transcript (+ playback of stored audio: each line plays from its own channel's file)
  const tEl = $("#m-transcript");
  const players = $$("#m-player audio");
  const trackFor = (speaker) => players.find((a) => a.dataset.channel === speaker) || null;
  const redo = async (seg, btn) => {
    btn.disabled = true;
    seg.nextElementSibling?.classList.contains("redo") && seg.nextElementSibling.remove();
    const box = document.createElement("div");
    box.className = "redo";
    box.innerHTML = `<div class="progress"><span class="spinner"></span>Gemma is listening to this line…</div>`;
    seg.after(box);
    try {
      const r = await api(`/api/segments/${seg.dataset.id}/retranscribe`, { method: "POST" });
      const same = r.text.trim().toLowerCase() === r.original.trim().toLowerCase();
      box.innerHTML = `<div class="small muted">Gemma heard${same ? " the same words" : ""}</div><div>${esc(r.text)}</div>`
        + (same ? "" : `<div class="row" style="margin-top:6px"><button class="btn sm primary" data-use>Use this</button><button class="btn sm" data-keep>Keep original</button></div>`);
      if (same) setTimeout(() => box.remove(), 2500);
      $("[data-keep]", box)?.addEventListener("click", () => box.remove());
      $("[data-use]", box)?.addEventListener("click", async () => {
        try { await api(`/api/segments/${seg.dataset.id}`, { method: "PUT", body: { text: r.text } }); $(".txt", seg).textContent = r.text; box.remove(); toast("Transcript line updated. Search and notes will use the new wording."); }
        catch (err) { toast(err.message, true); }
      });
    } catch (err) { box.remove(); toast(err.message, true); }
    btn.disabled = false;
  };
  tEl.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (act) {
      const seg = act.closest(".seg");
      if (act.dataset.act === "redo") redo(seg, act);
      else setFocus({ media_id: Number(trackFor(seg.dataset.speaker).dataset.id), t_start: Math.max(0, Number(seg.dataset.t) - 1),
        t_end: Number(seg.dataset.end) + 1, title: $("#m-title").value || "This meeting" });
      return;
    }
    const seg = e.target.closest(".seg.playable");
    if (!seg) return;
    const audio = trackFor(seg.dataset.speaker);
    players.forEach((a) => { if (a !== audio) a.pause(); });
    $$(".seg.playing", tEl).forEach((s) => s.classList.remove("playing"));
    audio.currentTime = Math.max(0, Number(seg.dataset.t) - 0.3);
    audio.play().catch((err) => toast(`Could not play the audio: ${err.message}`, true));
  });
  players.forEach((audio) => audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    let current = null;
    $$(`.seg[data-speaker="${audio.dataset.channel}"]`, tEl).forEach((s) => {
      const on = t >= Number(s.dataset.t) - 0.3 && t < Number(s.dataset.end) + 0.3;
      s.classList.toggle("playing", on);
      if (on) current = s;
    });
    if (current && !audio.paused) current.scrollIntoView({ block: "nearest" });
  }));
  let nSegs = 0;
  const addSeg = (s) => {
    const near = tEl.scrollHeight - tEl.scrollTop - tEl.clientHeight < 80;
    const div = document.createElement("div");
    const playable = !!trackFor(s.speaker);
    div.className = "seg" + (playable ? " playable" : "");
    Object.assign(div.dataset, { id: s.id, t: s.t_start, end: s.t_end, speaker: s.speaker });
    div.innerHTML = `<span class="ts">${fmtTs(s.t_start)}</span><div><div class="who ${s.speaker.toLowerCase()}">${esc(s.speaker)}</div><span class="txt">${esc(s.text)}</span></div>`
      + (playable ? `<span class="seg-actions"><button class="btn icon" data-act="ask" title="Ask about this moment (Gemma listens)">✦</button>`
        + `<button class="btn icon" data-act="redo" title="Re-transcribe this line with Gemma">↻</button></span>` : "");
    tEl.append(div);
    nSegs++;
    $("#m-count").textContent = `${nSegs} lines`;
    if (near) tEl.scrollTop = tEl.scrollHeight;
  };
  if (m.segments.length) m.segments.forEach(addSeg);
  else tEl.innerHTML = `<div class="empty" id="t-empty">${recording ? "Listening… speech will appear here as it is transcribed on this PC." : "No transcript."}</div>`;

  // --- live cards
  const cardsEl = $("#m-cards");
  const cardEmpty = () => { if (!cardsEl.children.length) cardsEl.innerHTML = `<div class="empty" id="c-empty">When someone asks a question your files can answer, the answer appears here, with sources.</div>`; };
  const addCard = (c, pendingEl) => {
    $("#c-empty")?.remove();
    const div = pendingEl || document.createElement("div");
    div.className = "insight";
    div.innerHTML = `<div class="q">❝ ${esc(c.question)}</div><div class="md">${md(c.answer)}</div>${sourcesHtml(c.citations)}`;
    div._cites = c.citations;
    if (!pendingEl) cardsEl.prepend(div);
  };
  m.insights.forEach((c) => addCard(c));  // oldest first; each goes on top, like live ones
  cardEmpty();
  const pending = new Map();

  $("#m-live").onchange = (e) => api("/api/settings", { method: "PUT", body: { live_assist: e.target.checked } });
  $("#m-float").onclick = () => openHud(id).catch((err) => toast(`Could not open the floating window: ${err.message}`, true));

  // A moment picked with ✦ on a transcript line (or "Ask" on a cited moment): Gemma hears it with the question.
  let focus = null;
  const setFocus = (m) => { focus = m; renderFocus($("#m-focus"), focus, () => setFocus(null)); if (m) $("#m-ask-input").focus(); };
  state.focusHandler = setFocus;
  onCleanup(() => (state.focusHandler = null));

  $("#m-ask").onsubmit = async (e) => {
    e.preventDefault();
    const input = $("#m-ask-input");
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    $("#c-empty")?.remove();
    const div = document.createElement("div");
    div.className = "insight";
    div.innerHTML = `<div class="q">You asked${focus ? ` about ${fmtTs(focus.t_start)}` : ""}: ${esc(q)}</div><div class="md"><span class="progress"><span class="spinner"></span>Thinking on-device…</span></div>`;
    cardsEl.prepend(div);
    let text = "";
    const picked = focus && { media_id: focus.media_id, t_start: focus.t_start, t_end: focus.t_end };
    try {
      await stream("/api/chat", { question: q, conv_id: convId, meeting_id: id, focus: picked }, (ev) => {
        if (ev.type === "start") {
          div._cites = ev.citations;
          if (gemmaNote(ev)) $(".q", div).insertAdjacentHTML("beforeend", ` <span class="muted">· ${gemmaNote(ev)}</span>`);
          if (ev.listened?.length) $(".md", div).innerHTML = `<span class="progress"><span class="spinner"></span>Listening to the recording…</span>`;
        }
        if (ev.type === "heard") $(".q", div).insertAdjacentHTML("afterend", heardNote(ev));
        if (ev.type === "delta") { text += ev.text; $(".md", div).innerHTML = md(text); }
        if (ev.type === "done") div.insertAdjacentHTML("beforeend", sourcesHtml(div._cites));
      });
    } catch (err) { $(".md", div).textContent = err.message; }
  };

  // --- recording controls
  let timer = null;
  const startTimer = (t0) => {
    const tick = () => { $("#m-timer").textContent = fmtTs(Date.now() / 1000 - t0); };
    tick();
    timer = setInterval(tick, 1000);
  };
  if (recording) startTimer(m.started_at);
  onCleanup(() => clearInterval(timer));

  $("#m-stop").onclick = async () => {
    const btn = $("#m-stop");
    btn.disabled = true; btn.textContent = "Finishing transcription…";
    try { await api("/api/meetings/stop", { method: "POST" }); } catch (err) { toast(err.message, true); }
  };
  $("#m-delete").onclick = async () => {
    if (!confirm("Delete this meeting, its transcript and notes?")) return;
    await api(`/api/meetings/${id}`, { method: "DELETE" });
    location.hash = "#/meeting";
  };

  $("#m-enhance").onclick = async () => {
    const btn = $("#m-enhance");
    const prog = $("#m-progress");
    btn.disabled = true;
    prog.hidden = false;
    enhanced = "";
    showTab("enhanced");
    enhancedEl.innerHTML = "";
    try {
      await stream(`/api/meetings/${id}/enhance`, {}, (ev) => {
        if (ev.type === "progress") $("span:last-child", prog).textContent = ev.step;
        if (ev.type === "delta") { enhanced += ev.text; renderEnhanced(); }
        if (ev.type === "done") toast(`Notes enhanced${ev.action_items.length ? ` · ${ev.action_items.length} action items` : ""}. Saved to your knowledge hub.`);
      });
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
    prog.hidden = true;
  };

  listen((ev) => {
    if (ev.meeting_id !== id && ev.id !== id) return;
    if (ev.type === "media" && ev.meeting_id === id && ev.status === "ready") { route(); return; }  // saved audio is ready to play
    if (ev.type === "segment_update" && ev.meeting_id === id) { const t = $(`.seg[data-id="${ev.id}"] .txt`, tEl); if (t) t.textContent = ev.text; }
    if (ev.type === "transcript_pass" && ev.meeting_id === id) {
      const filledNow = ev.filled && !(pass && pass.filled);
      pass = { ...(pass || {}), ...ev };
      showPass();
      // New lines for speech live transcription missed (show them in time order), or the audio kept only for the pass is gone.
      if (filledNow || (ev.status === "done" && (pass.temp_audio || pass.filled || ev.removed))) route();
    }
    if (ev.type === "segment" && ev.meeting_id === id) { $("#t-empty")?.remove(); addSeg(ev); }
    if (ev.type === "levels" && ev.meeting_id === id) {
      $$("#m-meters i").forEach((i) => (i.style.width = `${(ev[i.dataset.src] || 0) * 100}%`));
      const errs = Object.entries(ev.errors || {});
      if (errs.length) $("#m-status").textContent = `Recording (${errs.map(([k]) => k).join(", ")} failed)`;
    }
    if (ev.type === "question" && ev.meeting_id === id) {
      $("#c-empty")?.remove();
      const div = document.createElement("div");
      div.className = "insight pending";
      div.innerHTML = `<div class="q">❝ ${esc(ev.question)}</div><div class="progress"><span class="spinner"></span>Searching your files…</div>`;
      cardsEl.prepend(div);
      pending.set(ev.question, div);
    }
    if (ev.type === "card" && ev.meeting_id === id) { addCard(ev, pending.get(ev.question)); pending.delete(ev.question); }
    if (ev.type === "card_skip" && ev.meeting_id === id) { pending.get(ev.question)?.remove(); pending.delete(ev.question); cardEmpty(); }
    if (ev.type === "meeting" && ev.id === id) {
      if (ev.title) $("#m-title").value = ev.title;
      if (ev.status === "finishing") $("#m-status").textContent = "Finishing…";
      if (ev.status === "done" || ev.status === "error") {
        clearInterval(timer);
        $("#m-status").className = "chip ok"; $("#m-status").textContent = ev.status === "error" ? "Stopped with errors" : "Done";
        $("#m-stop").hidden = true; $("#m-meters").hidden = true; $("#m-enhance").hidden = false; $("#m-float").hidden = true;
      }
    }
  });
}

// ------------------------------------------------------------------ heads-up window
// Live answers in a small window that stays on top of the call (Document Picture-in-Picture, Chrome and Edge; a plain
// pop-up elsewhere). It listens to the main window's WebSocket itself, so it keeps going wherever the main window is.
const hud = { win: null, meetingId: null };
const HUD_CARDS = 6;

async function openHud(meetingId) {
  if (hud.win && !hud.win.closed) {
    if (hud.meetingId === meetingId) { hud.win.focus(); return; }
    hud.win.close();
  }
  let win = null;
  if (window.documentPictureInPicture) {
    try { win = await documentPictureInPicture.requestWindow({ width: 380, height: 540 }); }
    catch { win = null; }  // some embedded browsers have the API but cannot make the window
  }
  if (!win) {
    win = window.open("", "foresight-hud", "popup,width=400,height=580");
    if (!win) { toast("Allow pop-ups for Foresight Wing to float live answers", true); return; }
    toast("This browser cannot keep it on top, so it opened as a small window. Chrome or Edge can float it over your call.");
  }
  const m = await api(`/api/meetings/${meetingId}`);
  hud.win = win; hud.meetingId = meetingId;
  const doc = win.document;
  doc.title = `Live answers · ${m.title}`;
  doc.head.innerHTML = `<meta charset="utf-8"><link rel="stylesheet" href="${location.origin}/static/style.css">`;
  doc.body.className = "hud";
  doc.body.innerHTML = `
    <header class="hud-head"><span class="rec-dot" id="h-dot"></span><b class="grow" id="h-title"></b><span class="timer" id="h-timer"></span></header>
    <div class="hud-cards" id="h-cards"></div>
    <div class="hud-said" id="h-said" hidden></div>
    <form class="row hud-ask" id="h-ask"><input type="text" id="h-q" placeholder="Ask your files…" aria-label="Ask a question"><button class="btn sm primary">Ask</button></form>`;
  const $h = (sel) => doc.querySelector(sel);
  $h("#h-title").textContent = m.title;
  const cards = $h("#h-cards"), said = $h("#h-said");

  const empty = () => {
    if (!cards.children.length) cards.innerHTML = `<div class="empty" id="h-empty">When someone asks a question your files can answer, the answer appears here.</div>`;
  };
  const sources = (cites) => (cites?.length
    ? `<div class="hud-src">${cites.map((c) => `<span title="${esc(c.snippet || "")}">[${c.n}] ${esc(c.title)}</span>`).join(" · ")}</div>` : "");
  const card = (question, html, pending = false) => {
    $h("#h-empty")?.remove();
    const div = doc.createElement("div");
    div.className = "insight" + (pending ? " pending" : "");
    div.innerHTML = `<div class="q">${esc(question)}</div><div class="md">${html}</div>`;
    cards.prepend(div);
    while (cards.children.length > HUD_CARDS) cards.lastElementChild.remove();
    return div;
  };
  const thinking = (what) => `<span class="progress"><span class="spinner"></span>${what}</span>`;
  // oldest first; each goes on top
  m.insights.slice(-HUD_CARDS).forEach((c) => { card(`❝ ${c.question}`, md(c.answer)).insertAdjacentHTML("beforeend", sources(c.citations)); });
  empty();

  const lines = m.segments.slice(-2);
  const showSaid = () => {
    said.hidden = !lines.length;
    said.innerHTML = lines.map((s) => `<div><b>${esc(s.speaker)}</b> ${esc(s.text)}</div>`).join("");
  };
  showSaid();

  const ended = () => { $h("#h-dot").hidden = true; $h("#h-timer").textContent = "Meeting ended"; clearInterval(timer); };
  let timer = null;
  if (m.status === "recording") {
    const tick = () => { $h("#h-timer").textContent = fmtTs(Date.now() / 1000 - m.started_at); };
    tick();
    timer = setInterval(tick, 1000);
  } else ended();

  const pending = new Map();
  const onEvent = (ev) => {
    if (ev.meeting_id !== meetingId && !(ev.type === "meeting" && ev.id === meetingId)) return;
    if (ev.type === "question") pending.set(ev.question, card(`❝ ${ev.question}`, thinking("Searching your files…"), true));
    if (ev.type === "card") {
      const div = pending.get(ev.question) || card(`❝ ${ev.question}`, "");
      pending.delete(ev.question);
      div.className = "insight";
      div.querySelector(".md").innerHTML = md(ev.answer);
      div.insertAdjacentHTML("beforeend", sources(ev.citations));
    }
    if (ev.type === "card_skip") { pending.get(ev.question)?.remove(); pending.delete(ev.question); empty(); }
    if (ev.type === "segment") { lines.push(ev); if (lines.length > 2) lines.shift(); showSaid(); }
    if (ev.type === "segment_update") { const s = lines.find((l) => l.id === ev.id); if (s) { s.text = ev.text; showSaid(); } }
    if (ev.type === "meeting" && ["done", "error"].includes(ev.status)) ended();
  };
  listeners.add(onEvent);
  win.addEventListener("pagehide", () => {
    listeners.delete(onEvent);
    clearInterval(timer);
    if (hud.win === win) { hud.win = null; hud.meetingId = null; }
  });

  const convId = `hud-${meetingId}-${Date.now()}`;
  $h("#h-ask").onsubmit = async (e) => {
    e.preventDefault();
    const q = $h("#h-q").value.trim();
    if (!q) return;
    $h("#h-q").value = "";
    const div = card(`You asked: ${q}`, thinking("Thinking on-device…"));
    let text = "", cites = [];
    try {
      await stream("/api/chat", { question: q, conv_id: convId, meeting_id: meetingId }, (ev) => {
        if (ev.type === "start") cites = ev.citations;
        if (ev.type === "delta") { text += ev.text; div.querySelector(".md").innerHTML = md(text); }
        if (ev.type === "done") div.insertAdjacentHTML("beforeend", sources(cites));
      });
    } catch (err) { div.querySelector(".md").textContent = err.message; }
  };
}

// ------------------------------------------------------------------ knowledge graph
// A small force layout on a canvas (no libraries, works offline). Dots are sources, sized by how much they hold;
// lines join sources whose content is similar (EmbeddingGemma 2); grey squares are labels.
const GRAPH_KINDS = [["file", "Files"], ["url", "Web pages"], ["transcript", "Meetings"], ["notes", "Notes"], ["label", "Labels"]];
const graphKind = (k) => (k === "upload" ? "file" : k);

function graphView(root, data, { memory, onOpen, onLabel } = {}) {
  const positions = memory?.positions || new Map();  // where dots were before a refresh, so the map does not jump
  const canvas = $("canvas", root), side = $(".graph-side", root), find = $(".graph-find", root);
  const ctx = canvas.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const ink = { line: color("--outline"), text: color("--text"), panel: color("--panel"), accent: color("--accent"), soft: color("--accent-soft") };
  const kindColor = Object.fromEntries(GRAPH_KINDS.map(([k]) => [k, color(`--g-${k}`)]));

  const nodes = data.nodes.map((n, i) => {
    const p = positions.get(n.id), r = 12 * Math.sqrt(i + .5), a = i * Math.PI * (3 - Math.sqrt(5));  // sunflower start: same map every time
    return { ...n, x: p ? p.x : r * Math.cos(a), y: p ? p.y : r * Math.sin(a), vx: 0, vy: 0,
             r: n.kind === "label" ? 6 : 4 + Math.min(7, Math.sqrt(n.chunks || 1)) };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = data.edges.map((e) => ({ ...e, s: byId.get(e.a), t: byId.get(e.b) })).filter((e) => e.s && e.t);
  const links = new Map(nodes.map((n) => [n.id, []]));
  edges.forEach((e) => { links.get(e.s.id).push({ node: e.t, edge: e }); links.get(e.t.id).push({ node: e.s, edge: e }); });
  const degree = (n) => links.get(n.id).length || 1;

  let cam = memory ? { ...memory.cam } : { x: 0, y: 0, k: 1 }, touched = !!memory?.touched, fitted = !!memory?.fitted;
  let alpha = positions.size ? .25 : 1, alphaTarget = 0, raf = 0, selected = null, hover = null, drag = null;
  let width = 0, height = 0;

  const step = () => {
    for (let i = 0; i < nodes.length; i++) {  // every pair pushes apart (fine for a few hundred dots)
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
        if (d2 > 250000) continue;
        if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
        const f = -110 * alpha / d2;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const e of edges) {  // links pull together; closer for more similar sources
      const { s, t } = e;
      const dx = t.x + t.vx - s.x - s.vx, dy = t.y + t.vy - s.y - s.vy, d = Math.hypot(dx, dy) || 1;
      const rest = e.kind === "label" ? 90 : 45 + (1 - e.weight) * 220;
      const f = (d - rest) / d * alpha * (e.kind === "label" ? .25 : .6) / Math.min(degree(s), degree(t));
      const bias = degree(s) / (degree(s) + degree(t));
      t.vx -= dx * f * bias; t.vy -= dy * f * bias; s.vx += dx * f * (1 - bias); s.vy += dy * f * (1 - bias);
    }
    for (const n of nodes) {  // a gentle pull to the middle keeps separate clusters on screen
      n.vx -= n.x * .02 * alpha; n.vy -= n.y * .02 * alpha;
      if (n === drag?.node) { n.vx = n.vy = 0; continue; }
      n.vx *= .6; n.vy *= .6; n.x += n.vx; n.y += n.vy;
    }
    alpha += (alphaTarget - alpha) * .0228;
  };

  const fit = () => {
    if (!nodes.length || !width) return false;
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const top = 64, pad = 36;  // keep clear of the search box and zoom buttons
    cam.k = Math.max(.2, Math.min(2.5, Math.min((width - 2 * pad) / (x1 - x0 || 1), (height - top - pad) / (y1 - y0 || 1))));
    cam.x = -(x0 + x1) / 2 * cam.k; cam.y = (top - pad) / 2 - (y0 + y1) / 2 * cam.k;
    return true;
  };
  const toWorld = (px, py) => [(px - width / 2 - cam.x) / cam.k, (py - height / 2 - cam.y) / cam.k];
  const nodeAt = (px, py) => {
    const [x, y] = toWorld(px, py);
    let best = null, bestD = Infinity;
    for (const n of nodes) {
      const d = Math.hypot(n.x - x, n.y - y);
      if (d < Math.max(n.r + 3, 8 / cam.k) && d < bestD) { best = n; bestD = d; }
    }
    return best;
  };
  const near = (n) => new Set([n.id, ...links.get(n.id).map((l) => l.node.id)]);

  const draw = () => {
    const dpr = devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.setTransform(dpr * cam.k, 0, 0, dpr * cam.k, dpr * (width / 2 + cam.x), dpr * (height / 2 + cam.y));
    const focus = selected || hover;
    const lit = focus ? near(focus) : null;
    ctx.lineWidth = 1 / cam.k;
    for (const e of edges) {
      const on = focus && (e.s === focus || e.t === focus);
      ctx.strokeStyle = on ? ink.accent : ink.line;
      ctx.globalAlpha = on ? .9 : focus ? .12 : e.kind === "label" ? .35 : .25 + (e.weight - .5) * 1.2;
      ctx.setLineDash(e.kind === "label" ? [3 / cam.k, 3 / cam.k] : []);
      ctx.beginPath(); ctx.moveTo(e.s.x, e.s.y); ctx.lineTo(e.t.x, e.t.y); ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const n of nodes) {
      ctx.globalAlpha = lit && !lit.has(n.id) ? .2 : 1;
      ctx.fillStyle = kindColor[graphKind(n.kind)] || ink.line;
      ctx.beginPath();
      if (n.kind === "label") ctx.rect(n.x - n.r, n.y - n.r, n.r * 2, n.r * 2);
      else ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fill();
      if (n === selected) {  // an outer ring, apart from the dot, so it shows on any colour
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 4 / cam.k, 0, Math.PI * 2);
        ctx.lineWidth = 2 / cam.k; ctx.strokeStyle = ink.accent; ctx.stroke(); ctx.lineWidth = 1 / cam.k;
      }
    }
    ctx.font = `${12 / cam.k}px ${css.getPropertyValue("--font")}`;
    ctx.textBaseline = "middle";
    for (const n of nodes) {  // names: labels and the focused neighbourhood always; everything once zoomed in
      const show = n.kind === "label" || (lit ? lit.has(n.id) : cam.k > 1.6);
      if (!show || (lit && !lit.has(n.id))) continue;
      const name = n.title.length > 42 ? n.title.slice(0, 40) + "…" : n.title;
      ctx.globalAlpha = 1;
      ctx.lineWidth = 3 / cam.k; ctx.strokeStyle = ink.panel; ctx.strokeText(name, n.x + n.r + 4 / cam.k, n.y);
      ctx.fillStyle = ink.text; ctx.fillText(name, n.x + n.r + 4 / cam.k, n.y);
    }
    ctx.globalAlpha = 1;
  };

  const frame = () => {
    raf = 0;
    if (alpha > .004 || alphaTarget) { step(); if (!fitted && alpha < .08 && !touched) fitted = fit(); }
    draw();
    if (alpha > .004 || alphaTarget) raf = requestAnimationFrame(frame);
  };
  const kick = (a = .3) => { alpha = Math.max(alpha, a); if (!raf) raf = requestAnimationFrame(frame); };
  const redraw = () => { if (!raf) raf = requestAnimationFrame(frame); };

  const showSide = () => {
    if (!selected) {
      const n = nodes.filter((x) => x.kind !== "label").length, m = edges.filter((e) => e.kind === "similar").length;
      side.innerHTML = `<h3>${n} source${n === 1 ? "" : "s"} · ${m} link${m === 1 ? "" : "s"}</h3>
        <p class="small muted">Lines join sources that are more alike than usual for this collection (similarity ${data.threshold.toFixed(2)} or more, by EmbeddingGemma 2 on this PC). Closer means more alike. Click a dot to see what it connects to; drag to move, scroll to zoom.</p>
        <div class="graph-legend">${GRAPH_KINDS.map(([k, name]) => `<span><i class="${k}"></i>${name}</span>`).join("")}</div>`;
      return;
    }
    const related = links.get(selected.id).filter((l) => l.edge.kind === "similar").sort((a, b) => b.edge.weight - a.edge.weight);
    const members = links.get(selected.id).filter((l) => l.edge.kind === "label");
    const item = (l) => `<button class="graph-link" data-node="${esc(l.node.id)}"><i class="${graphKind(l.node.kind)}"></i><span class="grow">${esc(l.node.title)}</span>${l.edge.kind === "similar" ? `<span class="small muted">${l.edge.weight.toFixed(2)}</span>` : ""}</button>`;
    if (selected.kind === "label") {
      side.innerHTML = `<div class="small muted">Label</div><h3>${esc(selected.title)}</h3>
        <button class="btn sm" data-act="label">Show only this label</button>
        <div class="small muted" style="margin-top:12px">${members.length} source${members.length === 1 ? "" : "s"}</div>${members.map(item).join("")}`;
    } else {
      const kind = Object.fromEntries(GRAPH_KINDS)[graphKind(selected.kind)] || selected.kind;
      side.innerHTML = `<div class="small muted">${esc(kind)} · ${selected.chunks} chunk${selected.chunks === 1 ? "" : "s"}</div><h3>${esc(selected.title)}</h3>
        <div class="row" style="flex-wrap:wrap;gap:6px"><button class="btn sm" data-act="open">Read text</button>
          ${selected.meeting_id ? `<a class="btn sm" href="#/meeting/${selected.meeting_id}">Open meeting</a>` : ""}</div>
        ${selected.labels.length ? `<div class="row" style="flex-wrap:wrap;gap:4px;margin-top:10px">${selected.labels.map((l) => `<span class="chip">${esc(l)}</span>`).join("")}</div>` : ""}
        <div class="small muted" style="margin-top:12px">${related.length ? "Most similar" : "No close matches among your sources"}</div>${related.map(item).join("")}`;
    }
    $$("[data-node]", side).forEach((b) => (b.onclick = () => select(byId.get(isNaN(+b.dataset.node) ? b.dataset.node : +b.dataset.node), true)));
    const act = (name, fn) => { const b = $(`[data-act="${name}"]`, side); if (b) b.onclick = fn; };
    act("open", () => onOpen?.(selected));
    act("label", () => onLabel?.(selected.title));
  };
  const select = (n, center = false) => {
    selected = n || null;
    if (n && center) { cam.x = -n.x * cam.k; cam.y = -n.y * cam.k; touched = true; }
    showSide(); redraw();
  };

  // pointer: drag a dot to move it, drag the background to pan, click to select, double-click to read
  const at = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.onpointerdown = (e) => {
    const [x, y] = at(e);
    drag = { node: nodeAt(x, y), x, y, cx: cam.x, cy: cam.y, moved: false };
    canvas.setPointerCapture(e.pointerId);
  };
  canvas.onpointermove = (e) => {
    if (!drag) {
      const n = nodeAt(...at(e));
      if (n !== hover) { hover = n; canvas.style.cursor = n ? "pointer" : "grab"; canvas.title = n ? n.title : ""; redraw(); }
      return;
    }
    const [x, y] = at(e);
    if (Math.hypot(x - drag.x, y - drag.y) > 3) drag.moved = true;
    if (!drag.moved) return;
    touched = true;
    if (drag.node) { [drag.node.x, drag.node.y] = toWorld(x, y); alphaTarget = .3; kick(); }
    else { cam.x = drag.cx + x - drag.x; cam.y = drag.cy + y - drag.y; canvas.style.cursor = "grabbing"; redraw(); }
  };
  canvas.onpointerup = () => {
    if (drag && !drag.moved) select(drag.node);
    alphaTarget = 0; drag = null; canvas.style.cursor = hover ? "pointer" : "grab";
  };
  canvas.ondblclick = (e) => { const n = nodeAt(...at(e)); if (n && n.kind !== "label") onOpen?.(n); };
  canvas.onwheel = (e) => {
    e.preventDefault();
    const k = Math.max(.2, Math.min(5, cam.k * Math.exp(-e.deltaY * .0015)));
    const [px, py] = at(e), [x, y] = toWorld(px, py);
    cam.x = px - width / 2 - x * k; cam.y = py - height / 2 - y * k; cam.k = k;
    touched = true; redraw();
  };
  $("[data-zoom='fit']", root).onclick = () => { fit(); redraw(); };
  $$("[data-zoom='in'], [data-zoom='out']", root).forEach((b) => (b.onclick = () => {
    const k = Math.max(.2, Math.min(5, cam.k * (b.dataset.zoom === "in" ? 1.3 : 1 / 1.3)));
    cam.x *= k / cam.k; cam.y *= k / cam.k; cam.k = k; touched = true; redraw();
  }));
  $("datalist", root).innerHTML = nodes.map((n) => `<option value="${esc(n.title)}">`).join("");
  find.onkeydown = (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const q = find.value.trim().toLowerCase();
    const n = q && (nodes.find((x) => x.title.toLowerCase() === q) || nodes.find((x) => x.title.toLowerCase().includes(q)));
    if (n) select(n, true); else toast("No source with that name in the graph");
  };

  const measure = () => {
    width = canvas.clientWidth; height = canvas.clientHeight;
    const dpr = devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    if (!fitted && !touched) fitted = fit();
    redraw();
  };
  const resize = new ResizeObserver(measure);
  resize.observe(canvas);
  while (alpha > .01) step();  // settle before the first paint (animation frames pause while the window is hidden)
  measure();
  showSide();

  return {
    memory: () => ({ positions: new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }])), cam: { ...cam }, touched, fitted }),
    destroy: () => { cancelAnimationFrame(raf); raf = -1; resize.disconnect(); },
  };
}

// ------------------------------------------------------------------ knowledge
async function renderKnowledge() {
  let activeLabel = sessionStorage.getItem("label") || "";
  view.innerHTML = `
    <div class="page">
      <div class="page-head"><h1>Knowledge</h1><span class="grow"></span>
        <form id="k-search" class="row" style="width:min(420px,100%)"><input type="search" placeholder="Semantic search across everything…" id="k-q"><button class="btn sm">Search</button></form></div>
      <div class="grid cols-3">
        <form class="card" id="k-folder"><h3>Connect a local folder</h3>
          <p class="small muted">Foresight Wing indexes supported files and re-scans every 10 minutes. Files stay on this PC.</p>
          <div class="row"><input type="text" id="k-folder-path" placeholder="C:\\Users\\you\\Documents\\Project X"><button class="btn sm primary">Add</button></div></form>
        <form class="card" id="k-url"><h3>Save a web page</h3>
          <p class="small muted">The only time Foresight Wing goes online: it fetches the page once, then indexes it locally.</p>
          <div class="row"><input type="url" id="k-url-input" placeholder="https://…"><button class="btn sm primary">Add</button></div></form>
        <div class="card"><h3>Upload files</h3>
          <label class="drop" id="k-drop" for="k-files">Drop PDF, DOCX, PPTX, XLSX, MD, TXT, HTML, EPUB, images, audio… or click</label>
          <input type="file" id="k-files" multiple hidden></div>
      </div>
      <div class="card" id="k-folders-card" hidden><h2>Folders</h2><div class="list" id="k-folders"></div></div>
      <div class="card" id="k-results-card" hidden><div class="row"><h2 class="grow" style="margin:0">Search results</h2><button class="btn sm" id="k-results-close">Close</button></div><div id="k-results"></div></div>
      <div class="card">
        <div class="row" style="flex-wrap:wrap"><h2 style="margin:0">Labels</h2><span class="grow"></span>
          <button class="btn sm primary" id="k-wiki" hidden>✦ Build wiki page</button></div>
        <div class="row" id="k-labels" style="flex-wrap:wrap;margin-top:10px"></div>
        <div id="k-wiki-out" class="md" style="margin-top:12px" hidden></div>
      </div>
      <div class="card"><div class="row" style="flex-wrap:wrap"><h2 class="grow" style="margin:0">Sources</h2><span class="muted small" id="k-count"></span>
          <div class="tabs" id="k-mode"><button data-mode="list">List</button><button data-mode="graph">Graph</button></div></div>
        <div id="k-list" style="overflow:auto"><table><thead><tr><th>Title</th><th>Type</th><th>Status</th><th>Chunks</th><th>Labels</th><th></th></tr></thead><tbody id="k-rows"></tbody></table></div>
        <div id="k-graph" class="graph-wrap" hidden>
          <div class="graph-stage"><canvas aria-label="Map of your sources: similar sources are joined by lines"></canvas>
            <div class="graph-tools"><input type="search" class="graph-find" list="graph-names" placeholder="Find a source…" aria-label="Find a source in the graph"><datalist id="graph-names"></datalist>
              <button class="btn icon" data-zoom="in" title="Zoom in">+</button><button class="btn icon" data-zoom="out" title="Zoom out">−</button><button class="btn sm" data-zoom="fit">Fit</button></div>
            <div class="graph-empty empty" hidden>Nothing to map yet. Sources appear here once they are indexed.</div></div>
          <aside class="graph-side"></aside>
        </div></div>
      <datalist id="label-list"></datalist>
      <dialog id="k-dialog" class="card" style="max-width:820px;width:92vw;color:var(--text)"><div class="row"><h2 class="grow" id="k-dialog-title"></h2><button class="btn sm" onclick="this.closest('dialog').close()">Close</button></div><div id="k-dialog-body" style="max-height:65vh;overflow:auto;white-space:pre-wrap"></div></dialog>
    </div>`;

  const refresh = async () => {
    const [sources, labels, folders] = await Promise.all([
      api("/api/sources" + (activeLabel ? `?label=${encodeURIComponent(activeLabel)}` : "")), api("/api/labels"), api("/api/folders")]);
    $("#label-list").innerHTML = labels.map((l) => `<option value="${esc(l.name)}">`).join("");
    $("#k-labels").innerHTML = `<span class="chip label ${activeLabel ? "" : "active"}" data-label="">All sources</span>` +
      labels.map((l) => `<span class="chip label ${l.name === activeLabel ? "active" : ""}" data-label="${esc(l.name)}">${esc(l.name)} · ${l.n}</span>`).join("") +
      (labels.length ? "" : `<span class="small muted">Tag related sources with a label (e.g. <i>Client-Project-X</i>) to build a wiki page and catch conflicting details.</span>`);
    $$("#k-labels .chip").forEach((c) => (c.onclick = () => { activeLabel = c.dataset.label; sessionStorage.setItem("label", activeLabel); $("#k-wiki-out").hidden = true; refresh(); }));
    $("#k-wiki").hidden = !activeLabel;
    $("#k-wiki").textContent = `✦ Build wiki page for “${activeLabel}”`;

    $("#k-folders-card").hidden = !folders.length;
    $("#k-folders").innerHTML = folders.map((f) => `<div class="list-item" style="cursor:default"><div class="grow"><div class="title">${esc(f.path)}</div>
      <div class="small muted">${f.n_sources} files${f.scanned_at ? ` · scanned ${fmtTime(f.scanned_at)}` : ""}</div></div>
      <button class="btn sm" data-rescan="${f.id}">Rescan</button><button class="btn sm danger" data-rmfolder="${f.id}">Remove</button></div>`).join("");
    $$("[data-rescan]").forEach((b) => (b.onclick = async () => { const r = await api(`/api/folders/${b.dataset.rescan}/rescan`, { method: "POST" }); toast(`${r.queued} files queued`); }));
    $$("[data-rmfolder]").forEach((b) => (b.onclick = async () => { if (confirm("Remove this folder and its indexed files from Foresight Wing? (Your files are not deleted.)")) { await api(`/api/folders/${b.dataset.rmfolder}`, { method: "DELETE" }); refresh(); } }));

    $("#k-count").textContent = `${sources.length} ${activeLabel ? `labelled “${activeLabel}”` : "total"}`;
    $("#k-rows").innerHTML = sources.length ? sources.map((s) => `
      <tr data-id="${s.id}">
        <td class="title-cell"><a href="#" data-view="${s.id}" title="${esc(s.uri)}">${esc(s.title)}</a></td>
        <td><span class="chip">${esc(s.kind === "file" || s.kind === "upload" ? (s.mime || s.kind) : s.kind)}</span></td>
        <td class="status-${s.status}" title="${esc(s.error || "")}">${s.status}${s.status === "error" ? " ⓘ" : ""}</td>
        <td>${s.n_chunks}</td>
        <td><div class="row" style="flex-wrap:wrap;gap:4px">${s.labels.map((l) => `<span class="chip">${esc(l)}<button data-unlabel="${esc(l)}" title="Remove label">×</button></span>`).join("")}
          <input type="text" class="label-add" list="label-list" placeholder="+ label" data-label-for="${s.id}"></div></td>
        <td style="white-space:nowrap"><button class="btn icon" data-reindex="${s.id}" title="Re-index">↻</button><button class="btn icon danger" data-rm="${s.id}" title="Remove from Foresight Wing">×</button></td>
      </tr>`).join("") : `<tr><td colspan="6" class="empty">Nothing here yet. Connect a folder, save a page, or upload files.</td></tr>`;

    $$("[data-label-for]").forEach((inp) => inp.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      e.preventDefault();
      await api(`/api/sources/${inp.dataset.labelFor}/labels`, { method: "POST", body: { name: inp.value.trim() } });
      refresh();
    }));
    $$("[data-unlabel]").forEach((b) => (b.onclick = async () => {
      await api(`/api/sources/${b.closest("tr").dataset.id}/labels/${encodeURIComponent(b.dataset.unlabel)}`, { method: "DELETE" });
      refresh();
    }));
    $$("[data-reindex]").forEach((b) => (b.onclick = () => api(`/api/sources/${b.dataset.reindex}/reindex`, { method: "POST" })));
    $$("[data-rm]").forEach((b) => (b.onclick = async () => { await api(`/api/sources/${b.dataset.rm}`, { method: "DELETE" }); refresh(); }));
    $$("[data-view]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openText(a.dataset.view); }));
    if (mode === "graph") await drawGraph();
  };
  const openText = async (id) => {
    const s = await api(`/api/sources/${id}/text`);
    $("#k-dialog-title").textContent = s.title;
    $("#k-dialog-body").textContent = s.chunks.join("\n\n— — —\n\n") || "(not indexed yet)";
    if (s.picture) $("#k-dialog-body").insertAdjacentHTML("afterbegin", `<a href="${esc(s.picture.url)}" target="_blank" rel="noopener"><img class="dialog-picture" src="${esc(s.picture.url)}" alt="${esc(s.title)}"></a>`);
    if (s.audio) $("#k-dialog-body").insertAdjacentHTML("afterbegin", `<audio class="dialog-audio" controls preload="metadata" src="${esc(s.audio.url)}"></audio>`);
    $("#k-dialog").showModal();
  };

  // List or Graph: the graph is built only while it is on screen
  let mode = sessionStorage.getItem("k-mode") === "graph" ? "graph" : "list", graph = null, drawn = "";
  const drawGraph = async () => {
    const data = await api("/api/graph" + (activeLabel ? `?label=${encodeURIComponent(activeLabel)}` : ""));
    const key = JSON.stringify(data);
    if (mode !== "graph" || (graph && key === drawn)) return;  // indexing sends many events; most change nothing here
    drawn = key;
    const memory = graph?.memory();
    graph?.destroy();
    graph = null;
    $("#k-graph .graph-empty").hidden = data.nodes.length > 0;
    if (!data.nodes.length) { $("#k-graph .graph-side").innerHTML = ""; return; }
    graph = graphView($("#k-graph"), data, {
      memory, onOpen: (n) => openText(n.id),
      onLabel: (name) => { activeLabel = name; sessionStorage.setItem("label", name); refresh(); },
    });
  };
  const setMode = (m, draw = true) => {
    mode = m;
    sessionStorage.setItem("k-mode", m);
    $$("#k-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    $("#k-list").hidden = m !== "list";
    $("#k-graph").hidden = m !== "graph";
    if (m === "graph") { if (draw) drawGraph().catch((err) => toast(err.message, true)); }
    else { graph?.destroy(); graph = null; }
  };
  $$("#k-mode button").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
  onCleanup(() => graph?.destroy());
  setMode(mode, false);  // refresh() draws it
  await refresh();
  const refreshSoon = debounce(refresh, 600);
  listen((ev) => { if (ev.type === "source") refreshSoon(); });

  $("#k-folder").onsubmit = async (e) => {
    e.preventDefault();
    try { const r = await api("/api/sources/folder", { method: "POST", body: { path: $("#k-folder-path").value } }); toast(`Watching ${r.path} · ${r.queued} files queued`); $("#k-folder-path").value = ""; refresh(); }
    catch (err) { toast(err.message, true); }
  };
  $("#k-url").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/sources/url", { method: "POST", body: { url: $("#k-url-input").value } }); $("#k-url-input").value = ""; refresh(); }
    catch (err) { toast(err.message, true); }
  };
  const uploadFiles = async (files) => {
    if (!files.length) return;
    const form = new FormData();
    [...files].forEach((f) => form.append("files", f));
    try {
      const r = await api("/api/sources/upload", { method: "POST", form });
      toast(`${r.added.length} file(s) added${r.rejected.length ? `, ${r.rejected.length} skipped (unsupported)` : ""}`);
      refresh();
    } catch (err) { toast(err.message, true); }
  };
  $("#k-files").onchange = (e) => uploadFiles(e.target.files);
  const drop = $("#k-drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles(e.dataTransfer.files); };

  $("#k-search").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#k-q").value.trim();
    if (!q) return;
    $("#k-results-card").hidden = false;
    $("#k-results").innerHTML = `<div class="progress"><span class="spinner"></span>Embedding your query with EmbeddingGemma 2…</div>`;
    try {
      const hits = await api(`/api/search?q=${encodeURIComponent(q)}${activeLabel ? `&label=${encodeURIComponent(activeLabel)}` : ""}`);
      $("#k-results").innerHTML = hits.length ? hits.map((h) => `<div class="list-item" style="cursor:default;align-items:flex-start">
        <span class="chip">${h.similarity.toFixed(2)}</span><div class="grow"><div class="title">${esc(h.title)}</div><div class="small muted">${esc(h.snippet)}</div></div>
        ${h.media ? mediaChips(h.media, h.title, isPicture(h.media) ? "Open" : "") : ""}</div>`).join("")
        : `<div class="empty">No matches.</div>`;
    } catch (err) { $("#k-results").textContent = err.message; }
  };
  $("#k-results-close").onclick = () => ($("#k-results-card").hidden = true);

  $("#k-wiki").onclick = async () => {
    const out = $("#k-wiki-out");
    const btn = $("#k-wiki");
    btn.disabled = true;
    out.hidden = false;
    out.innerHTML = `<div class="progress"><span class="spinner"></span><span>Starting…</span></div>`;
    let text = "";
    try {
      await stream("/api/wiki", { label: activeLabel }, (ev) => {
        if (ev.type === "progress") { const p = $(".progress span:last-child", out); if (p) p.textContent = ev.step; }
        if (ev.type === "start") out._cites = ev.citations;
        if (ev.type === "delta") { text += ev.text; out.innerHTML = md(text); }
        if (ev.type === "done") out.insertAdjacentHTML("beforeend", sourcesHtml(ev.citations));
      });
    } catch (err) { out.textContent = err.message; }
    btn.disabled = false;
  };
}

// ------------------------------------------------------------------ chat
async function renderChat(convArg) {
  let convId = convArg || null;
  const labels = await api("/api/labels");
  view.innerHTML = `
    <div class="page">
      <div class="chat-layout">
        <aside class="card chat-list"><button class="btn primary" id="c-new" style="width:100%;justify-content:center">+ New chat</button>
          <div class="list" id="c-list" style="margin-top:10px"></div></aside>
        <section class="card chat-main">
          <div class="messages" id="c-msgs"></div>
          <form class="composer" id="c-form">
            <select id="c-label" style="width:170px" title="Limit answers to one label"><option value="">All knowledge</option>${labels.map((l) => `<option>${esc(l.name)}</option>`).join("")}</select>
            <textarea id="c-input" rows="1" placeholder="Ask anything about your files, notes and meetings…"></textarea>
            <button class="btn primary" id="c-send">Send</button>
          </form>
          <div class="composer-opts"><div class="focus-pill" id="c-focus" hidden></div><span class="grow"></span>
            <label class="toggle small" title="Gemma also hears the audio of up to two cited moments: slower, but it catches transcription mistakes">
              <input type="checkbox" id="c-listen"> Listen to cited audio</label></div>
        </section>
      </div>
    </div>`;
  const msgs = $("#c-msgs");
  const addMsg = (role, content, citations) => {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (role === "user") div.textContent = content;
    else { div.innerHTML = `<div class="md">${md(content)}</div>${sourcesHtml(citations)}`; div._cites = citations; }
    msgs.append(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  };
  const loadList = async () => {
    const convs = await api("/api/chats");
    $("#c-list").innerHTML = convs.length ? convs.map((c) => `<div class="list-item ${c.conv_id === convId ? "active" : ""}" data-conv="${esc(c.conv_id)}"><div class="grow"><div class="small">${esc((c.first || "Chat").slice(0, 60))}</div><div class="small muted">${fmtTime(c.started_at)}</div></div></div>`).join("")
      : `<div class="empty small">No chats yet.</div>`;
    $$("#c-list [data-conv]").forEach((el) => (el.onclick = () => (location.hash = `#/chat/${el.dataset.conv}`)));
  };
  await loadList();
  if (convId) (await api(`/api/chats/${encodeURIComponent(convId)}`)).forEach((m) => addMsg(m.role, m.content, m.citations));
  else msgs.innerHTML = `<div class="empty" id="c-empty"><span class="orb" style="display:inline-block;width:44px;height:44px"></span><h2 style="margin-top:12px">Ask your second brain</h2>
    <p>Answers come from your indexed files, saved pages and meeting notes, with citations. Gemma 4 runs on this PC.</p></div>`;
  $("#c-new").onclick = () => (location.hash = "#/chat");

  // A moment picked with "Ask" (here, or on another page and carried over) stays until cleared.
  let focus = null;
  try { focus = JSON.parse(sessionStorage.getItem("focus") || "null"); } catch { /* ignore */ }
  sessionStorage.removeItem("focus");
  const showFocus = () => renderFocus($("#c-focus"), focus, () => { focus = null; showFocus(); });
  showFocus();
  state.focusHandler = (m) => { focus = m; showFocus(); $("#c-input").focus(); };
  onCleanup(() => (state.focusHandler = null));
  const listenBox = $("#c-listen");
  listenBox.checked = sessionStorage.getItem("listen") === "1";
  listenBox.onchange = () => sessionStorage.setItem("listen", listenBox.checked ? "1" : "0");

  const input = $("#c-input");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#c-form").requestSubmit(); } });
  $("#c-form").onsubmit = async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    $("#c-empty")?.remove();
    addMsg("user", q);
    const div = addMsg("assistant", "");
    $(".md", div).innerHTML = `<span class="progress"><span class="spinner"></span>Searching your knowledge…</span>`;
    $("#c-send").disabled = true;
    let text = "";
    try {
      const picked = focus && { media_id: focus.media_id, t_start: focus.t_start, t_end: focus.t_end };
      await stream("/api/chat", { question: q, conv_id: convId, label: $("#c-label").value || null, listen: listenBox.checked, focus: picked }, (ev) => {
        if (ev.type === "start") {
          convId = ev.conv_id; div._cites = ev.citations;
          if (gemmaNote(ev)) div.insertAdjacentHTML("afterbegin", `<div class="listened small muted">${gemmaNote(ev)}</div>`);
          if (ev.listened?.length) $(".md", div).innerHTML = `<span class="progress"><span class="spinner"></span>Listening to the recordings…</span>`;
          if (ev.window) div.insertAdjacentHTML("afterbegin", `<div class="listened small muted">${ev.window.meetings
            ? `Looked at meetings from ${esc(ev.window.label)} (${ev.window.meetings})` : `No meetings were recorded ${esc(ev.window.label)}; searched everything`}</div>`);
        }
        if (ev.type === "heard") $(".md", div).insertAdjacentHTML("beforebegin", heardNote(ev));
        if (ev.type === "delta") { text += ev.text; $(".md", div).innerHTML = md(text); msgs.scrollTop = msgs.scrollHeight; }
        if (ev.type === "done") div.insertAdjacentHTML("beforeend", sourcesHtml(div._cites));
      });
      history.replaceState(null, "", `#/chat/${convId}`);
      loadList();
    } catch (err) { $(".md", div).textContent = err.message; }
    $("#c-send").disabled = false;
    input.focus();
  };
  input.focus();
}

// ------------------------------------------------------------------ settings
async function renderSettings() {
  const [st, dev] = await Promise.all([api("/api/status"), api("/api/audio-devices")]);
  const p = st.prefs, l = st.llm;
  const pullHint = (name, ready) => ready ? '<span class="chip ok">ready</span>' : `<span class="chip err">missing</span> <code>ollama pull ${esc(name)}</code>`;
  view.innerHTML = `
    <div class="page"><div class="page-head"><h1>Settings</h1></div>
      <div class="card"><h2>Setup check</h2><div id="s-setup"></div></div>
      <div class="grid cols-2">
        <div class="card"><h2>On-device models</h2>
          ${l.ok ? `<p class="muted small">Ollama ${esc(l.version)} · running locally</p>
          <p><b>Generation</b> · ${esc(l.gen_model)} ${pullHint(l.gen_model, l.gen_ready)}</p>
          <p><b>Embeddings</b> · ${esc(l.embed_model)} ${pullHint(l.embed_model, l.embed_ready)}</p>` : `<p class="status-error">${esc(l.error)}</p>`}
          <p><b>Speech-to-text</b> · ${esc(st.models.asr)} (local weights only)</p>
          <p class="small muted">Change models with FORESIGHT_GEN_MODEL, FORESIGHT_EMBED_MODEL and FORESIGHT_ASR_ENGINE (parakeet or whisper) before starting.</p>
          <p><label class="toggle"><input type="checkbox" data-pref="rerank" ${p.rerank ? "checked" : ""}> Let Gemma re-rank sources before answering</label><br>
            <span class="small muted">More accurate chat answers and briefings, a few seconds slower. Live answers skip it.</span></p>
          <p><label class="toggle"><input type="checkbox" data-pref="index_pictures" ${p.index_pictures ? "checked" : ""}> Index pictures in watched folders</label><br>
            <span class="small muted">Gemma describes each photo, screenshot or scan (about 2 seconds each) so you can search and ask about it. Pictures under 12 KB are skipped; uploaded pictures are always indexed. Pictures wait while a meeting records.</span></p>
          <p><label class="toggle"><input type="checkbox" data-pref="index_audio_files" ${p.index_audio_files ? "checked" : ""}> Transcribe audio files in watched folders</label><br>
            <span class="small muted">Voice memos and recordings (mp3, m4a, wav, ogg, flac…) become searchable, with a ▶ that plays the cited moment from your file. Uses your default meeting language. Uploaded audio is always transcribed; files wait while a meeting records.</span></p></div>
        <div class="card"><h2>Live assistance</h2>
          <p><label class="toggle"><input type="checkbox" data-pref="live_assist" ${p.live_assist ? "checked" : ""}> Answer questions heard during meetings</label></p>
          <p><label class="toggle"><input type="checkbox" data-pref="answer_own_questions" ${p.answer_own_questions ? "checked" : ""}> Also answer questions <i>I</i> ask (microphone)</label></p>
          <p>Relevance threshold <b id="score-v">${Number(p.live_min_score).toFixed(2)}</b><br>
            <input type="range" min="0.1" max="0.7" step="0.01" value="${p.live_min_score}" data-pref="live_min_score" style="width:100%"></p>
          <p class="small muted">Higher means fewer but more confident cards.</p></div>
        <div class="card"><h2>Audio</h2>
          ${dev.error ? `<p class="status-error">${esc(dev.error)}</p>` : `<p><b>Microphone</b> · ${esc(dev.microphone)}</p><p><b>System audio</b> · loopback of ${esc(dev.speaker)}</p>`}
          <p><label class="toggle"><input type="checkbox" data-pref="capture_mic" ${p.capture_mic ? "checked" : ""}> Capture microphone by default</label></p>
          <p><label class="toggle"><input type="checkbox" data-pref="capture_system" ${p.capture_system ? "checked" : ""}> Capture system audio by default</label></p>
          <p class="small muted">Change devices in Windows Sound settings. Tell other participants you are transcribing.</p></div>
        <div class="card"><h2>Audio memory</h2>
          <p><label class="toggle"><input type="checkbox" id="keep-audio" ${p.keep_meeting_audio ? "checked" : ""}> Keep meeting audio on this PC</label></p>
          <p class="small muted">Lets you replay any transcript line. About 14 MB per hour for each audio source. Imported recordings are kept only while this is on. Off by default.
            Hindi / Hinglish meetings keep their audio until Gemma has re-transcribed them, then delete it unless this is on.</p>
          <p><label>Keep audio for <select id="audio-retention" style="width:auto">${[7, 30, 90, 0].map((d) =>
            `<option value="${d}" ${Number(p.audio_retention_days) === d ? "selected" : ""}>${d ? `${d} days` : "until I delete it"}</option>`).join("")}</select></label></p>
          <div class="row" style="flex-wrap:wrap"><span class="small muted grow" id="audio-usage"></span><button class="btn sm danger" id="audio-delete">Delete all stored audio</button></div>
          <p class="small muted" id="media-caps" style="margin-bottom:0"></p></div>
        <div class="card"><h2>Privacy</h2>
          <p>Everything (transcription, embeddings, generation and storage) runs on this PC. The server only listens on 127.0.0.1.</p>
          <p class="small muted">Data folder: <code>${esc(st.data_dir)}</code></p>
          <p class="small muted">The only network requests are web pages and calendar subscriptions you add yourself.</p></div>
        <div class="card" style="grid-column:1/-1"><h2>Calendars</h2>
          <p class="small muted">Daily Briefings read events from local .ics files or calendar subscription URLs (the private iCal address from your calendar's settings, or webcal://). They re-sync every 15 minutes. No Google sign-in.</p>
          <div class="list" id="cal-list"></div>
          <form class="row" id="cal-form" style="margin-top:10px"><input type="text" id="cal-uri" placeholder="C:\\Users\\you\\calendar.ics  or  https://…/basic.ics"><button class="btn sm primary">Add calendar</button></form></div>
      </div></div>`;
  // Audio memory: opt-in storage, retention, usage, and what the installed models can do with audio.
  const showMedia = (m) => {
    $("#audio-usage").textContent = (m.files ? `${fmtBytes(m.bytes)} in ${m.files} recording${m.files === 1 ? "" : "s"}` : "No stored audio.")
      + (m.moments ? ` · ${fmtNum(m.moments)} searchable audio moment${m.moments === 1 ? "" : "s"}` : "");
    $("#audio-delete").disabled = !m.files;
    const c = m.caps || {};
    const mark = (ok) => (ok ? "✓" : "✗");
    $("#media-caps").innerHTML = !c.checked ? "Checking what your models can do with audio…"
      : `Model support: audio search ${mark(c.audio_embed)} · Gemma listening ${mark(c.gen_audio)} · images ${mark(c.image_embed && c.gen_vision)}`
        + (c.error ? ` <span class="status-error">(${esc(c.error)})</span>` : "")
        + ` <button class="btn sm" id="media-recheck" style="margin-left:6px">Check again</button>`;
    $("#media-recheck")?.addEventListener("click", async () => {
      $("#media-caps").textContent = "Checking…";
      try { await api("/api/media/check", { method: "POST" }); } catch (err) { toast(err.message, true); }
      showMedia((await api("/api/status")).media);
    });
  };
  showMedia(st.media);
  showSetup($("#s-setup"));
  if (!st.media.caps.checked) setTimeout(async () => showMedia((await api("/api/status")).media), 9000);
  $("#keep-audio").onchange = async (e) => {
    try {
      await api("/api/settings", { method: "PUT", body: { keep_meeting_audio: e.target.checked } });
      toast(e.target.checked ? "New meetings will keep their audio on this PC. Tell participants you are recording." : "New meetings will not keep audio. Stored audio stays until it expires or you delete it.");
    } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  };
  const retention = $("#audio-retention");
  let keptDays = Number(retention.value);
  retention.onchange = async () => {
    const days = Number(retention.value);
    const shorter = days !== 0 && (keptDays === 0 || days < keptDays);
    if (shorter && !confirm(`Audio older than ${days} days will be deleted now. Transcripts stay. Continue?`)) { retention.value = String(keptDays); return; }
    try {
      await api("/api/settings", { method: "PUT", body: { audio_retention_days: days } });
      keptDays = days;
      showMedia((await api("/api/status")).media);
    } catch (err) { toast(err.message, true); retention.value = String(keptDays); }
  };
  $("#audio-delete").onclick = async () => {
    if (!confirm("Delete all stored meeting audio? Transcripts and notes stay.")) return;
    try { const r = await api("/api/media?kind=recording", { method: "DELETE" }); toast(`Deleted ${r.deleted} recording${r.deleted === 1 ? "" : "s"}.`); }
    catch (err) { toast(err.message, true); }
    showMedia((await api("/api/status")).media);
  };

  // Subscription URLs often carry a private token, so only the host is shown.
  const maskUri = (u) => (/^https?:\/\//i.test(u) ? u.replace(/^(https?:\/\/[^/]+).*$/i, "$1/…") : u);
  const refreshCals = async () => {
    const cals = await api("/api/calendars");
    $("#cal-list").innerHTML = cals.length ? cals.map((c) => `<div class="list-item" style="cursor:default"><div class="grow"><div class="title">${esc(c.name)}</div>
      <div class="small muted">${esc(maskUri(c.uri))} · ${c.n_events} events in the next 7 days${c.synced_at ? ` · synced ${fmtTime(c.synced_at)}` : ""}</div>
      ${c.error ? `<div class="small status-error">${esc(c.error)}</div>` : ""}</div>
      <button class="btn sm" data-cal-sync="${c.id}">Sync now</button><button class="btn sm danger" data-cal-rm="${c.id}">Remove</button></div>`).join("")
      : `<div class="small muted">No calendars yet.</div>`;
    $$("[data-cal-sync]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { const r = await api(`/api/calendars/${b.dataset.calSync}/sync`, { method: "POST" }); r.error ? toast(r.error, true) : toast(`${r.n_events} events synced`); }
      catch (err) { toast(err.message, true); }
      refreshCals();
    }));
    $$("[data-cal-rm]").forEach((b) => (b.onclick = async () => {
      if (confirm("Remove this calendar from Foresight Wing? (The .ics file or subscription itself is not changed.)")) { await api(`/api/calendars/${b.dataset.calRm}`, { method: "DELETE" }); refreshCals(); }
    }));
  };
  await refreshCals();
  $("#cal-form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#cal-form button");
    btn.disabled = true;
    try {
      const r = await api("/api/calendars", { method: "POST", body: { uri: $("#cal-uri").value } });
      toast(`Added ${r.name} · ${r.n_events} events in the next 7 days`);
      $("#cal-uri").value = "";
      refreshCals();
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  };
  $$("[data-pref]").forEach((el) => {
    const save = () => api("/api/settings", { method: "PUT", body: { [el.dataset.pref]: el.type === "checkbox" ? el.checked : Number(el.value) } }).catch((e) => toast(e.message, true));
    if (el.type === "range") { el.oninput = () => ($("#score-v").textContent = Number(el.value).toFixed(2)); el.onchange = save; }
    else el.onchange = save;
  });
}

// ------------------------------------------------------------------ boot
connectWS();
route();
