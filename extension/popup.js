/* YouTube -> FLAC: interface do popup. */

const API = "http://127.0.0.1:8765";
const MEDIA_RE = /^https?:\/\/((www\.|m\.|music\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)|(open|play)\.spotify\.com)\//i;

function isSpotify(url) {
  return /^https?:\/\/(open|play)\.spotify\.com\//i.test(url);
}

function cleanTitle(title) {
  return String(title || "")
    .replace(/ - YouTube( Music)?$/, "")
    .replace(/\s*[|·]\s*Spotify\s*$/i, "")
    .replace(/ - song and lyrics by .*$/i, "")
    .replace(/ - Single by .*$/i, "")
    .replace(/ - Album by .*$/i, "")
    .replace(/ - Playlist.*$/i, "");
}

const $ = (id) => document.getElementById(id);

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    const msg = chrome.i18n.getMessage(key);
    if (msg) el.textContent = msg;
  });
}

let currentUrl = "";
let pollTimer = null;
let serviceOnline = false;

function send(msg) {
  return new Promise((resolve) => {
    const attempt = (n) => {
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) {
          if (n < 2) setTimeout(() => attempt(n + 1), 300);
          else resolve({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          resolve(res || { ok: false, error: "sem resposta" });
        }
      });
    };
    attempt(0);
  });
}

function fmtTime(s) {
  if (!s || !isFinite(s)) return "";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ estado */

async function refreshHealth() {
  const res = await send({ type: "health" });
  serviceOnline = !!(res.ok && res.data && res.data.ok);
  const pill = $("service");
  if (serviceOnline) {
    pill.textContent = "serviço online";
    pill.className = "pill pill-on";
    $("offline").classList.add("hidden");
  } else {
    pill.textContent = "serviço offline";
    pill.className = "pill pill-off";
    $("offline").classList.remove("hidden");
    $("download").disabled = true;
  }
  return serviceOnline;
}

async function loadCurrentTab() {
  // ?url=... serve para testar o popup fora do YouTube (depuração).
  const forced = new URLSearchParams(location.search).get("url");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = forced || (tab && tab.url) || "";
  const isMedia = MEDIA_RE.test(currentUrl);

  $("current").classList.toggle("hidden", !isMedia);
  $("notVideo").classList.toggle("hidden", isMedia);

  if (isMedia) {
    const spotify = isSpotify(currentUrl);
    const raw = forced ? (new URL(currentUrl).searchParams.get("v") || currentUrl) : (tab.title || (spotify ? "Spotify" : "Vídeo do YouTube"));
    $("videoTitle").textContent = cleanTitle(raw);
    $("videoUrl").textContent = currentUrl;
    $("download").textContent = spotify ? "Baixar do Spotify em MP3" : "Baixar áudio em MP3";
    // status icon (green/orange/red)
    const icon = $("statusIcon");
    if (icon) {
      icon.className = "status-icon";
      icon.textContent = "…";
      if (spotify) {
        const res = await send({ type: "resolve", url: currentUrl });
        if (!res.ok) {
          icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Não foi possível verificar (serviço offline?)";
        } else if (res.data && res.data.error) {
          icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Playlist personalizada ou requer login — status não verificável.";
        } else if (res.data && Array.isArray(res.data.tracks) && res.data.tracks.length) {
          const total = res.data.tracks.length;
          const existing = res.data.existing != null ? res.data.existing : res.data.tracks.filter((t) => t.exists).length;
          if (existing === 0) { icon.classList.add("red"); icon.textContent = "●"; icon.title = `Nunca baixada (0/${total})`; }
          else if (existing === total) { icon.classList.add("green"); icon.textContent = "●"; icon.title = `Toda baixada (${existing}/${total})`; }
          else { icon.classList.add("orange"); icon.textContent = "●"; icon.title = `Parcialmente baixada (${existing}/${total})`; }
        } else {
          icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Não foi possível ler a playlist.";
        }
      } else {
        const res = await send({ type: "exists", url: currentUrl });
        if (!res.ok) { icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Não foi possível verificar (serviço offline?)"; }
        else if (res.data && res.data.exists) { icon.classList.add("green"); icon.textContent = "●"; icon.title = "Já baixado"; }
        else { icon.classList.add("red"); icon.textContent = "●"; icon.title = "Nunca baixado"; }
      }
    }
  }
}

/* ------------------------------------------------------------------- lista */

function jobLabel(job) {
  if (job.status === "done") return "pronto";
  if (job.status === "error") return "erro";
  if (job.status === "cancelled") return "cancelado";
  if (job.status === "tagging") return "gravando tags…";
  if (job.status === "converting") return "convertendo…";
  if (job.status === "searching") return "procurando áudio…";
  if (job.status === "downloading") return "baixando…";
  return "na fila";
}

function renderJobs(jobs) {
  const ul = $("jobs");
  ul.innerHTML = "";
  const visible = jobs.slice(0, 8);
  $("jobsEmpty").classList.toggle("hidden", visible.length > 0);

  for (const job of visible) {
    const li = document.createElement("li");
    li.className = "job";

    const top = document.createElement("div");
    top.className = "job-top";

    const title = document.createElement("div");
    title.className = "job-title";
    title.textContent = job.title || job.url;
    title.title = [job.group, job.title || job.url].filter(Boolean).join(" — ");

    const state = document.createElement("span");
    state.className = "job-state " + job.status;
    state.textContent = jobLabel(job);

    top.append(title, state);
    li.append(top);

    if (["downloading", "converting", "queued", "searching", "tagging"].includes(job.status)) {
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("i");
      fill.style.width = `${Math.max(2, Math.min(100, job.percent || 2))}%`;
      bar.append(fill);
      li.append(bar);
    }

    const actions = document.createElement("div");
    actions.className = "job-actions";

    if (job.status === "done" && job.file) {
      const a = document.createElement("button");
      a.className = "link";
      a.textContent = "salvar .mp3";
      a.onclick = () => chrome.tabs.create({ url: `${API}/api/file/${job.id}` });
      actions.append(a);
    }
    if (job.status === "error" && job.error) {
      const e = document.createElement("span");
      e.className = "muted";
      e.textContent = job.error.slice(0, 90);
      actions.append(e);
    }
    if (["downloading", "queued", "converting", "searching", "tagging"].includes(job.status)) {
      const c = document.createElement("button");
      c.className = "link";
      c.textContent = "cancelar";
      c.onclick = async () => {
        await send({ type: "cancel", id: job.id });
        poll();
      };
      actions.append(c);
    }
    if (actions.childElementCount) li.append(actions);

    ul.append(li);
  }
}

function renderHistory(entries) {
  const ul = $("history");
  if (!ul) return;
  ul.innerHTML = "";
  const empty = $("historyEmpty");
  if (!entries || !entries.length) {
    if (empty) empty.classList.remove("hidden");
    return;
  }
  if (empty) empty.classList.add("hidden");
  for (const e of entries.slice(0, 20)) {
    const li = document.createElement("li");
    li.className = "job";
    const top = document.createElement("div");
    top.className = "job-top";
    const title = document.createElement("div");
    title.className = "job-title";
    title.textContent = e.name || "Download";
    const state = document.createElement("span");
    state.className = "job-state " + (e.ok ? "done" : "error");
    state.textContent = e.ok ? "✓" : "erro";
    top.append(title, state);
    li.append(top);
    const sub = document.createElement("div");
    sub.className = "muted";
    sub.textContent = `${e.done || 0}/${e.total || 1} · ${new Date((e.finished || 0) * 1000).toLocaleString()}`;
    li.append(sub);
    ul.append(li);
  }
}

async function loadHistory() {
  const res = await send({ type: "history" });
  if (res.ok && res.data && Array.isArray(res.data.history)) {
    lastHistory = res.data.history;
    renderHistory(res.data.history);
    // refresh status line with current jobs
    const r2 = await send({ type: "jobs" });
    if (r2.ok && r2.data && Array.isArray(r2.data.jobs)) updateStatusLine(r2.data.jobs, lastHistory);
  }
}

function updateStatusLine(jobs, history) {
  const el = $("statusLine");
  if (!el) return;
  const active = (jobs || []).filter((j) => ["downloading","converting","queued","searching","tagging"].includes(j.status));
  if (active.length > 0) {
    const done = (jobs || []).filter((j) => j.status === "done").length;
    const total = jobs.length;
    el.textContent = (chrome.i18n.getMessage("statusDownloading") || "Baixando… {0}/{1} concluídas").replace("{0}",done).replace("{1}",total);
    el.className = "status-line active";
    return;
  }
  const last = (history || [])[0];
  if (last) {
    el.textContent = (chrome.i18n.getMessage("statusLast") || "Último: {0} — {1}/{2} concluídas").replace("{0}",last.name).replace("{1}",last.done||0).replace("{2}",last.total||1);
    el.className = "status-line done";
    return;
  }
  el.textContent = chrome.i18n.getMessage("statusIdle") || "Nenhum download em andamento.";
  el.className = "status-line idle";
}

let lastHistory = [];
let lastBatchInfo = null;
async function poll() {
  const res = await send({ type: "jobs" });
  if (res.ok && res.data && Array.isArray(res.data.jobs)) {
    renderJobs(res.data.jobs);
    updateStatusLine(res.data.jobs, lastHistory);
  }
}

/* ---------------------------------------------------------------- acoes */

async function startDownload() {
  if (!currentUrl) return;
  const btn = $("download");
  btn.disabled = true;
  btn.textContent = "Enviando…";

  const res = await send({ type: "download", url: currentUrl });
  if (!res.ok) {
    btn.textContent = "Falhou — tentar de novo";
    btn.disabled = false;
    return;
  }
  const jobs = (res.data && res.data.jobs) || [];
  const skipped = (res.data && res.data.skipped) || 0;
  if (jobs.length) {
    const name = (res.data && res.data.name) || jobs[0].title || "Download";
    lastBatchInfo = { name, total: jobs.length, skipped, started: Date.now() };
    chrome.storage.local.set({ lastBatch: lastBatchInfo });
  }
  if (skipped > 0) {
    const msg = $("saveMsg");
    if (msg) {
      msg.textContent = `${skipped} já existiam — baixando só as novas.`;
      setTimeout(() => (msg.textContent = ""), 4000);
    }
  }
  if (jobs.length > 1) {
    btn.textContent = `${jobs.length} faixas na fila`;
    setTimeout(() => (btn.textContent = isSpotify(currentUrl) ? "Baixar do Spotify em MP3" : "Baixar áudio em MP3"), 2500);
  } else {
    btn.textContent = "Baixar de novo";
    setTimeout(() => (btn.textContent = isSpotify(currentUrl) ? "Baixar do Spotify em MP3" : "Baixar áudio em MP3"), 2500);
  }
  btn.disabled = false;
  poll();
}

async function loadSettings() {
  const res = await send({ type: "settings" });
  if (res.ok && res.data) {
    $("outdir").value = res.data.outdir || "";
    $("cookies").value = res.data.cookies || "";
    $("format").value = res.data.format || "mp3";
    $("normalize").checked = !!res.data.normalize;
    $("minBitrate").value = res.data.min_bitrate || 0;
  }
}

async function saveSettings() {
  const payload = {
    outdir: $("outdir").value.trim(),
    cookies: $("cookies").value.trim(),
    format: $("format").value,
    normalize: $("normalize").checked,
    min_bitrate: parseInt($("minBitrate").value || "0", 10) || 0,
  };
  const res = await send({ type: "save-settings", payload });
  const msg = $("saveMsg");
  if (res.ok) {
    msg.textContent = "salvo ✓";
    setTimeout(() => (msg.textContent = ""), 1800);
  } else {
    msg.textContent = res.error || "erro";
  }
}

/* ---------------------------------------------------------------- bootstrap */

async function boot() {
  await loadCurrentTab();
  const online = await refreshHealth();
  applyI18n();
  if (online) {
    chrome.storage.local.get(["lastBatch"], (r) => {
      const lb = r && r.lastBatch;
      if (lb && lb.name) {
        lastBatchInfo = lb;
        const el = $("statusLine");
        if (el) { el.textContent = `Último enviado: ${lb.name} (${lb.total} faixas)`; el.className = "status-line"; }
      }
    });
    await loadSettings();
    await poll();
    await loadHistory();
    pollTimer = setInterval(poll, 900);
  }
}

$("download").addEventListener("click", startDownload);
$("save").addEventListener("click", saveSettings);
$("openFolder").addEventListener("click", () => send({ type: "open-folder" }));
$("clearHistory").addEventListener("click", async () => {
  const res = await send({ type: "clear-history" });
  if (res.ok) { lastHistory = []; renderHistory([]); $("historyEmpty").classList.remove("hidden"); updateStatusLine([], []); }
});
$("clearList").addEventListener("click", async () => {
  const res = await send({ type: "clear-list" });
  if (res.ok) { renderJobs([]); $("jobsEmpty").classList.remove("hidden"); updateStatusLine([], lastHistory); }
});
$("settingsBox").addEventListener("toggle", (e) => {
  if (e.target.open) loadSettings();
});

window.addEventListener("unload", () => clearInterval(pollTimer));
boot();
