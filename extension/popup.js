/* YouTube -> FLAC: interface do popup. */

const API = "http://127.0.0.1:8765";
const MEDIA_RE = /^https?:\/\/((www\.|m\.|music\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)|(open|play)\.spotify\.com|(www\.)?qobuz\.com|play\.qobuz\.com)\//i;

function isSpotify(url) {
  return /^https?:\/\/(open|play)\.spotify\.com\//i.test(url);
}

function isQobuz(url) {
  return /^https?:\/\/((www\.)?qobuz\.com|play\.qobuz\.com)\//i.test(url);
}

const FORMAT_LABELS = {
  mp3: "MP3", m4a: "M4A", opus: "Opus", vorbis: "OGG",
  flac: "FLAC", alac: "ALAC", wav: "WAV", original: "Original",
};
function formatLabel(fmt) {
  return FORMAT_LABELS[fmt] || String(fmt || "mp3").toUpperCase();
}
function updateDownloadLabel() {
  const btn = $("download");
  if (!btn) return;
  const fmtEl = $("format");
  const fmt = formatLabel(fmtEl ? fmtEl.value : "mp3");
  const isYtPlaylist = /youtube\.com\/.*[?&]list=/.test(currentUrl) || /youtube\.com\/playlist/i.test(currentUrl);
  if (isYtPlaylist) btn.textContent = `Baixar playlist em ${fmt}`;
  else if (resolvedKind === "album") btn.textContent = `Baixar álbum em ${fmt}`;
  else if (resolvedKind === "playlist") btn.textContent = `Baixar playlist em ${fmt}`;
  else if (resolvedKind === "track") btn.textContent = `Baixar faixa em ${fmt}`;
  else if (isSpotify(currentUrl)) btn.textContent = `Baixar do Spotify em ${fmt}`;
  else if (isQobuz(currentUrl)) btn.textContent = `Baixar do Qobuz em ${fmt}`;
  else btn.textContent = `Baixar áudio em ${fmt}`;
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
    if (msg) {
      if (el.tagName === "FOOTER") el.innerHTML = msg;
      else el.textContent = msg;
    }
  });
}

let currentUrl = "";
let pollTimer = null;
let serviceOnline = false;

function send(msg) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (r) => { if (!done) { done = true; resolve(r); } };
    const attempt = (n) => {
      if (done) return;
      chrome.runtime.sendMessage(msg, (res) => {
        if (done) return;
        if (chrome.runtime.lastError) {
          if (n < 2) setTimeout(() => attempt(n + 1), 300);
          else finish({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          finish(res || { ok: false, error: "sem resposta" });
        }
      });
    };
    attempt(0);
    // timeout de seguranca: nao pode ficar "enviando" para sempre
    setTimeout(() => finish({ ok: false, error: "sem resposta (timeout)" }), 25000);
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
  const off = $("offline");
  const dl = $("download");
  if (serviceOnline) {
    if (pill) { pill.textContent = "serviço online"; pill.className = "pill pill-on"; }
    if (off) off.classList.add("hidden");
  } else {
    if (pill) { pill.textContent = "serviço offline"; pill.className = "pill pill-off"; }
    if (off) off.classList.remove("hidden");
    if (dl) dl.disabled = true;
  }
  return serviceOnline;
}

async function loadCurrentTab() {
  // ?url=... serve para testar o popup fora do YouTube (depuração).
  const forced = new URLSearchParams(location.search).get("url");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = forced || (tab && tab.url) || "";
  const isMedia = MEDIA_RE.test(currentUrl);

  const cur = $("current");
  const nv = $("notVideo");
  if (cur) cur.classList.toggle("hidden", !isMedia);
  if (nv) nv.classList.toggle("hidden", isMedia);

  if (isMedia) {
    const spotify = isSpotify(currentUrl);
    const qobuz = isQobuz(currentUrl);
    const metaSource = spotify || qobuz;
    let resolvedName = "";
    if (metaSource) {
      const r = await send({ type: "resolve", url: currentUrl });
      if (r.ok && r.data) {
        resolvedName = r.data.name || (r.data.tracks && r.data.tracks[0] && r.data.tracks[0].artists && r.data.tracks[0].artists.join(" / ")) || "";
        resolvedKind = r.data.kind || null;
      }
    }
    const sourceLabel = spotify ? "Spotify" : (qobuz ? "Qobuz" : "Vídeo do YouTube");
    const raw = forced ? (new URL(currentUrl).searchParams.get("v") || currentUrl) : (resolvedName || tab.title || sourceLabel);
    const vt = $("videoTitle");
    if (vt) vt.textContent = cleanTitle(raw);
    const vu = $("videoUrl");
    if (vu) vu.textContent = currentUrl;
    updateDownloadLabel();
    // remove previous note
    const oldNote = document.getElementById("statusNote");
    if (oldNote) oldNote.remove();
    // status icon (green/orange/red)
    const icon = $("statusIcon");
    if (icon) {
      icon.className = "status-icon";
      icon.textContent = "…";
      if (metaSource) {
        const res = await send({ type: "resolve", url: currentUrl });
        if (!res.ok) {
          icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Não foi possível verificar (serviço offline?)";
        } else if (res.data && res.data.error) {
          icon.classList.add("gray"); icon.textContent = "●"; icon.title = "Playlist personalizada ou requer login — status não verificável.";
          const note = document.createElement("div");
          note.id = "statusNote";
          note.className = "muted";
          note.style.fontSize = "11px";
          note.style.marginTop = "4px";
          note.textContent = "Playlist personalizada: não dá para verificar o status, mas o download funciona normalmente.";
          const vu2 = $("videoUrl");
          if (vu2) vu2.after(note);
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

// Espelho de job_display_percent() do servidor: a barra flui 0→100 por fase,
// em vez de pular pra 100% no download e travar na conversão.
function jobDisplayPercent(job) {
  const st = job.status;
  if (st === "done") return 100;
  if (st === "error" || st === "cancelled") return job.percent || 0;
  if (st === "queued") return 0;
  if (st === "searching") return 4;
  if (st === "downloading") {
    const p = Math.max(0, Math.min(100, job.percent || 0));
    return 8 + p * 0.72; // 8..80
  }
  if (st === "converting") return 86;
  if (st === "tagging") return 94;
  return job.percent || 0;
}

function renderBatches(batches) {
  const ul = $("batches");
  if (!ul) return;
  ul.innerHTML = "";
  for (const b of batches || []) {
    const li = document.createElement("li");
    li.className = "batch";

    const top = document.createElement("div");
    top.className = "job-top";

    const title = document.createElement("div");
    title.className = "job-title";
    const label = b.name || b.group || "Download";
    title.textContent = label;
    title.title = label;

    const state = document.createElement("span");
    state.className = "job-state";
    const errs = b.error ? ` · ${b.error} erro${b.error > 1 ? "s" : ""}` : "";
    state.textContent = `${b.done}/${b.total} · ${Math.round(b.percent)}%${errs}`;

    top.append(title, state);
    li.append(top);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(2, Math.min(100, b.percent || 0))}%`;
    bar.append(fill);
    li.append(bar);

    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (b.status === "running") {
      const stop = document.createElement("button");
      stop.className = "link";
      stop.textContent = "⏹ parar";
      stop.title = "Parar este download";
      stop.onclick = async () => {
        for (const id of b.job_ids || []) {
          try { await send({ type: "cancel", id }); } catch (e) {}
        }
        poll();
      };
      actions.append(stop);
    }
    if (actions.childElementCount) li.append(actions);

    ul.append(li);
  }
}

function renderJobs(jobs) {
  const ul = $("jobs");
  if (!ul) return;
  ul.innerHTML = "";
  const visible = jobs;  // mostra todos; scroll no container
  const empty = $("jobsEmpty");
  if (empty) empty.classList.toggle("hidden", visible.length > 0);

  for (const job of visible) {
    const li = document.createElement("li");
    li.className = "job";

    const top = document.createElement("div");
    top.className = "job-top";

    const title = document.createElement("div");
    title.className = "job-title";
    title.textContent = job.group ? `${job.group} — ${job.title || job.url}` : (job.title || job.url);
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
      fill.style.width = `${Math.max(2, Math.min(100, jobDisplayPercent(job)))}%`;
      bar.append(fill);
      li.append(bar);
    }

    const actions = document.createElement("div");
    actions.className = "job-actions";

    if (job.status === "done" && job.file) {
      const play = document.createElement("button");
      play.className = "link";
      play.textContent = "▶ tocar";
      play.onclick = () => send({ type: "play", id: job.id, file: job.file || "" });
      actions.append(play);
    }
    if (job.status === "error" && job.error) {
      const e = document.createElement("span");
      e.className = "muted";
      e.textContent = job.error.slice(0, 90);
      actions.append(e);
      const retry = document.createElement("button");
      retry.className = "link";
      retry.textContent = "↻ tentar de novo";
      retry.title = "Reenviar este download";
      retry.onclick = async () => {
        retry.disabled = true;
        retry.textContent = "…";
        const res = await send({ type: "download", url: job.url });
        if (res && res.ok) poll();
        else { retry.disabled = false; retry.textContent = "↻ tentar de novo"; }
      };
      actions.append(retry);
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
    if (e.first_file) {
      const loc = document.createElement("div");
      loc.className = "muted";
      loc.style.fontSize = "10px";
      loc.style.wordBreak = "break-all";
      loc.textContent = (e.exists ? "📁 " : "⚠ apagado: ") + e.first_file;
      li.append(loc);
      if (e.exists) {
        const play = document.createElement("button");
        play.className = "link";
        play.textContent = "▶ tocar";
        play.onclick = () => send({ type: "play", file: e.first_file });
        li.append(play);
      }
    }
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
  // (removido: "Último: ..." — o histórico abaixo já mostra)
  el.textContent = chrome.i18n.getMessage("statusIdle") || "Nenhum download em andamento.";
  el.className = "status-line idle";
}

let lastHistory = [];
let lastBatchInfo = null;
let lastJobs = [];
let resolvedKind = null;
let previewData = null;
async function poll() {
  const res = await send({ type: "jobs" });
  if (res.ok && res.data && Array.isArray(res.data.jobs)) {
    renderBatches(res.data.batches || []);
    if (res.data.jobs.length > 0) {
      lastJobs = res.data.jobs;
      chrome.storage.local.set({ lastJobs: res.data.jobs });
      renderJobs(res.data.jobs);
      updateStatusLine(res.data.jobs, lastHistory);
    } else {
      // sem jobs ativos: limpa cache e mostra idle
      lastJobs = [];
      chrome.storage.local.remove(["lastJobs"]);
      renderBatches([]);
      renderJobs([]);
      updateStatusLine([], lastHistory);
    }
  }
}

/* ---------------------------------------------------------------- acoes */






async function downloadOne(url, group) {
  const payload = { url };
  if (group) payload.group = group;
  const res = await send({ type: "download", url, group: group || undefined });
  if (!res.ok) {
    const msg = $("saveMsg");
    if (msg) { msg.textContent = res.error || "falhou"; setTimeout(() => (msg.textContent = ""), 4000); }
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
    if (msg) { msg.textContent = `${skipped} já existiam — baixando só as novas.`; setTimeout(() => (msg.textContent = ""), 4000); }
  }
  poll();
}

async function startDownload() {
  if (!currentUrl) return;
  const btn = $("download");
  if (btn) { btn.disabled = true; btn.textContent = "Enviando…"; }
  await downloadOne(currentUrl);
  if (btn) { btn.disabled = false; setTimeout(() => updateDownloadLabel(), 2500); }
}

async function loadSettings() {
  const res = await send({ type: "settings" });
  if (res.ok && res.data) {
    $("outdir").value = res.data.outdir || "";
    $("cookies").value = res.data.cookies || "";
    $("deezerArl").value = res.data.deezer_arl || "";
    $("tidalCookies").value = res.data.tidal_cookies || "";
    $("format").value = res.data.format || "mp3";
    $("normalize").checked = !!res.data.normalize;
    const avail = res.data.mp3gain_available !== false;
    const g = $("mp3gain");
    g.checked = !!res.data.mp3gain && avail;
    g.disabled = !avail;
    const row = $("mp3gainOpt");
    if (row) row.classList.toggle("disabled", !avail);
    const hint = $("mp3gainHint");
    if (hint) hint.textContent = chrome.i18n.getMessage(avail ? "mp3gainHint" : "mp3gainMissing");
    $("minBitrate").value = res.data.min_bitrate || 0;
    const par = $("parallel");
    if (par) par.value = String(Math.min(14, Math.max(1, parseInt(res.data.parallel || "14", 10) || 14)));
    const ucf = $("useCollectionFolder");
    if (ucf) ucf.checked = res.data.use_collection_folder !== false;
    const nf = (res.data.name_format || "").trim();
    const sel = $("nameFormat");
    const custom = $("nameFormatCustom");
    const presets = Array.from(sel.options).map((o) => o.value).filter((v) => v && v !== "custom");
    if (presets.includes(nf)) {
      sel.value = nf;
      if (custom) custom.style.display = "none";
    } else if (nf) {
      sel.value = "custom";
      if (custom) { custom.style.display = ""; custom.value = nf; }
    } else {
      sel.value = "";
      if (custom) custom.style.display = "none";
    }
    const lt = $("loudnessTarget");
    if (lt) lt.value = String(res.data.loudness_target || -16);
    const nm = $("normMode");
    if (nm) nm.value = res.data.norm_mode || "constant";
    const normCb = $("normalize");
    const syncNormUi = () => {
      const on = !!(normCb && normCb.checked);
      if (nm) nm.disabled = !on;
      const lt = $("loudnessTarget");
      if (lt) lt.disabled = !on;
    };
    if (normCb) { normCb.addEventListener("change", syncNormUi); syncNormUi(); }
  }
}

async function saveSettings() {
  const payload = {
    outdir: $("outdir").value.trim(),
    cookies: $("cookies").value.trim(),
    deezer_arl: $("deezerArl").value.trim(),
    tidal_cookies: $("tidalCookies").value.trim(),
    format: $("format").value,
    normalize: $("normalize").checked,
    mp3gain: $("mp3gain").checked,
    min_bitrate: parseInt($("minBitrate").value || "0", 10) || 0,
    parallel: (() => {
      const p = $("parallel");
      const v = parseInt(p ? p.value : "14", 10) || 14;
      return Math.min(14, Math.max(1, v));
    })(),
    use_collection_folder: (() => {
      const c = $("useCollectionFolder");
      return c ? c.checked : true;
    })(),
    name_format: (() => {
      const sel = $("nameFormat");
      if (!sel) return "";
      if (sel.value === "custom") return ($("nameFormatCustom").value || "").trim();
      return sel.value;
    })(),
    loudness_target: parseInt($("loudnessTarget").value || "-16", 10) || -16,
    norm_mode: $("normMode") ? $("normMode").value : "constant",
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
    chrome.storage.local.get(["lastJobs"], (r) => {
      if (r && r.lastJobs && r.lastJobs.length) {
        lastJobs = r.lastJobs;
        renderJobs(r.lastJobs);
      }
    });
    await loadSettings();
    updateDownloadLabel();
    await poll();
    await loadHistory();
    pollTimer = setInterval(poll, 900);
  }
}

window.addEventListener("unhandledrejection", (e) => {
  try {
    const msg = (e && e.reason && (e.reason.stack || e.reason.message)) || String(e.reason);
    console.error("UNHANDLED REJECTION:", msg);
    const el = $("statusLine");
    if (el) { el.textContent = "Erro: " + String(msg).slice(0, 120); el.className = "status-line active"; }
  } catch (_) {}
});
window.addEventListener("error", (e) => {
  try {
    const msg = (e && (e.error && (e.error.stack || e.error.message)) || e.message) || "";
    console.error("WINDOW ERROR:", msg);
    const el = $("statusLine");
    if (el) { el.textContent = "Erro: " + String(msg).slice(0, 120); el.className = "status-line active"; }
  } catch (_) {}
});
const dlBtn2 = $("download");
if (dlBtn2) dlBtn2.addEventListener("click", startDownload);
const saveBtn2 = $("save");
if (saveBtn2) saveBtn2.addEventListener("click", saveSettings);
const previewBtn = $("preview");
if (previewBtn) previewBtn.addEventListener("click", async () => {
  if (!currentUrl) return;
  const btn = $("preview");
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  const r = await send({ type: "resolve", url: currentUrl });
  if (r.ok && r.data) previewData = r.data;
  if (btn) { btn.disabled = false; btn.textContent = "Buscar info"; }
  const box = $("previewBox");
  if (!box) return;
  if (!r.ok || !r.data) {
    box.open = true;
    const t = $("previewTitle");
    if (t) t.textContent = "Não foi possível resolver";
    const b = $("previewBody");
    if (b) b.textContent = (r.error || "serviço offline?");
    const ul = $("previewTracks");
    if (ul) ul.innerHTML = "";
    return;
  }
  const data = r.data;
  box.open = true;
  const t = $("previewTitle");
  if (t) t.textContent = data.name || (data.tracks && data.tracks[0] && data.tracks[0].title) || "—";
  const sm = $("previewSummary");
  if (sm) sm.textContent = data.name || "Buscar info";
  const body = $("previewBody");
  const tracks = data.tracks || [];
  const existing = data.existing != null ? data.existing : tracks.filter((x) => x.exists).length;
  let html = "";
  if (tracks.length === 1) {
    const tr = tracks[0];
    html = `${(tr.artists || []).join(", ")} · ${tr.album || ""} · ${tr.duration ? Math.round(tr.duration) + "s" : ""}`;
    if (tr.exists) html += " · já baixada";
  } else {
    html = `${tracks.length} faixas · ${existing} já baixadas`;
  }
  if (body) body.textContent = html;
  const ul = $("previewTracks");
  if (ul) {
    ul.innerHTML = "";
    if (tracks.length > 1) {
      tracks.forEach((tr, i) => {
        const li = document.createElement("li");
        li.className = "job preview-item" + (tr.exists ? " exists" : "");
        li.style.cursor = "pointer";
        li.title = tr.exists ? "Já baixada — clique para baixar de novo" : "Clique para baixar esta faixa";
        const top = document.createElement("div");
        top.className = "job-top";
        const title = document.createElement("div");
        title.className = "job-title";
        title.textContent = `${tr.track_number || i + 1}. ${tr.title || "?"}`;
        const state = document.createElement("span");
        state.className = "job-state " + (tr.exists ? "done" : "");
        state.textContent = tr.exists ? "✓ existe" : (tr.duration ? Math.round(tr.duration) + "s" : "");
        top.append(title, state);
        li.append(top);
        const bar = document.createElement("div");
        bar.className = "bar";
        const fill = document.createElement("i");
        fill.style.width = tr.exists ? "100%" : "0%";
        bar.append(fill);
        li.append(bar);
        li.addEventListener("click", () => {
          const url = tr.source;
          if (url) {
            li.classList.remove("exists");
            li.classList.add("downloading");
            state.textContent = "baixando…";
            fill.style.width = "30%";
            const grp = (previewData && previewData.group) || "";
            downloadOne(url, grp).then(() => {
              li.classList.remove("downloading");
              li.classList.add("exists");
              state.textContent = "✓ existe";
              fill.style.width = "100%";
              if (previewData) {
                send({ type: "resolve", url: currentUrl }).then((r) => {
                  if (r.ok && r.data) {
                    previewData = r.data;
                    const tracks2 = r.data.tracks || [];
                    const ex2 = r.data.existing != null ? r.data.existing : tracks2.filter((x) => x.exists).length;
                    const body2 = $("previewBody");
                    if (body2) body2.textContent = `${tracks2.length} faixas · ${ex2} já baixadas`;
                  }
                });
              }
            });
          }
        });
        ul.append(li);
      });
    }
  }
});

$("format").addEventListener("change", updateDownloadLabel);
$("nameFormat").addEventListener("change", () => {
  const sel = $("nameFormat");
  const custom = $("nameFormatCustom");
  if (custom) custom.style.display = (sel.value === "custom") ? "" : "none";
});
$("openFolder").addEventListener("click", () => send({ type: "open-folder" }));
$("clearHistory").addEventListener("click", async () => {
  const res = await send({ type: "clear-history" });
  if (res.ok) { lastHistory = []; renderHistory([]); const he = $("historyEmpty"); if (he) he.classList.remove("hidden"); updateStatusLine([], []); }
});
$("clearList").addEventListener("click", async () => {
  const res = await send({ type: "clear-list" });
  if (res.ok) { poll(); }
});
$("stopAll").addEventListener("click", async () => {
  const res = await send({ type: "stop-all" });
  if (res.ok) { poll(); }
});
$("settingsBox").addEventListener("toggle", (e) => {
  if (e.target.open) loadSettings();
});

boot();
