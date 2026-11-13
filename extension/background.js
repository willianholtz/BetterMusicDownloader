/* YouTube/Spotify -> FLAC: ponte entre a extensão e o serviço local (127.0.0.1:8765). */

const API = "http://127.0.0.1:8765";

const YT_MATCH = [
  "*://*.youtube.com/*",
  "*://youtu.be/*",
  "*://*.youtube-nocookie.com/*",
  "*://open.spotify.com/*",
  "*://*.qobuz.com/*",
  "*://play.qobuz.com/*",
];

/* Cria os jobs (um vídeo, ou várias faixas de um álbum/playlist) e acompanha
   todos em segundo plano. */
async function startJobs(url) {
  const data = await api("/api/jobs", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
  const created = (data && data.jobs) || [];
  for (const job of created) trackJob(job.id);
  if (data && data.name && created.length > 1) {
    groupTrackers.set(data.name, { ids: created.map((j) => j.id), notified: false });
  }
  return { created, data };
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(API + path, {
      ...options,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  } finally {
    clearTimeout(t);
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon128.png",
      title,
      message,
    });
  } catch {
    /* notificações podem estar desativadas */
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "ytflac-page",
      title: "Baixar áudio em MP3",
      contexts: ["page", "video"],
      documentUrlPatterns: YT_MATCH,
    });
    chrome.contextMenus.create({
      id: "ytflac-link",
      title: "Baixar este link em MP3",
      contexts: ["link"],
      targetUrlPatterns: YT_MATCH,
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.linkUrl || info.pageUrl || info.frameUrl;
  if (!url) return;
  try {
    const { created, data } = await startJobs(url);
    if (!created.length) throw new Error("nenhum job criado");
    const label = data.name
      ? `${data.name} (${created.length} faixas)`
      : "Convertendo para MP3…";
    notify(chrome.i18n.getMessage("notifyStarted") || "Download started", label);
  } catch (e) {
    notify(chrome.i18n.getMessage("notifyStartFail") || "Failed to start", String(e.message || e));
  }
});

/* Acompanha o job em segundo plano para avisar quando terminar, mesmo com o
   popup fechado. */
const tracking = new Set();
const groupTrackers = new Map();

function trackJob(id) {
  if (!id || tracking.has(id)) return;
  tracking.add(id);
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs?id=${encodeURIComponent(id)}`);
      if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
        clearInterval(timer);
        tracking.delete(id);
        if (job.status === "done") notify(chrome.i18n.getMessage("notifyDone") || "MP3 ready", job.title || "Audio converted successfully.");
        else if (job.status === "error") notify(chrome.i18n.getMessage("notifyFail") || "Failed", job.error || "Download error.");
        if (job.group && groupTrackers.has(job.group)) {
          const trk = groupTrackers.get(job.group);
          if (!trk.notified) {
            try {
              const all = await api("/api/jobs");
              const grpJobs = (all.jobs || []).filter((j) => j.group === job.group);
              const finished = grpJobs.filter((j) => ["done","error","cancelled"].includes(j.status));
              if (grpJobs.length && finished.length === grpJobs.length) {
                const ok = grpJobs.filter((j) => j.status === "done").length;
                trk.notified = true;
                notify(chrome.i18n.getMessage("notifyPlaylistDone") || "Playlist finished",
                       `${job.group} — ${ok}/${grpJobs.length}`);
              }
            } catch (e) {}
          }
        }
      }
    } catch {
      clearInterval(timer);
      tracking.delete(id);
    }
  }, 2000);
  setTimeout(() => {
    clearInterval(timer);
    tracking.delete(id);
  }, 30 * 60 * 1000);
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      switch (msg.type) {
        case "health":
          sendResponse({ ok: true, data: await api("/api/health") });
          break;
        case "settings":
          sendResponse({ ok: true, data: await api("/api/settings") });
          break;
        case "resolve":
          sendResponse({ ok: true, data: await api("/api/resolve?url=" + encodeURIComponent(msg.url)) });
          break;
        case "exists":
          sendResponse({ ok: true, data: await api("/api/exists?url=" + encodeURIComponent(msg.url)) });
          break;
        case "save-settings":
          sendResponse({
            ok: true,
            data: await api("/api/settings", {
              method: "POST",
              body: JSON.stringify(msg.payload || {}),
            }),
          });
          break;
        case "clear-list":
          sendResponse({ ok: true, data: await api("/api/jobs/clear", { method: "POST" }) });
          break;
        case "play":
          sendResponse({ ok: true, data: await api("/api/play", { method: "POST", body: JSON.stringify({ id: msg.id, file: msg.file || "" }) }) });
          break;
        case "stop-all": {
          const r = await api("/api/jobs");
          const active = (r.jobs || []).filter((j) => ["downloading","queued","converting","searching","tagging"].includes(j.status));
          for (const j of active) {
            try { await api("/api/jobs/cancel", { method: "POST", body: JSON.stringify({ id: j.id }) }); } catch (e) {}
          }
          sendResponse({ ok: true, data: { stopped: active.length } });
          break;
        }
        case "history":
          sendResponse({ ok: true, data: await api("/api/history") });
          break;
        case "clear-history":
          sendResponse({ ok: true, data: await api("/api/history/clear", { method: "POST" }) });
          break;
        case "download": {
          const { created, data } = await startJobs(msg.url);
          sendResponse({ ok: true, data: { ...data, jobs: created } });
          break;
        }
        case "jobs":
          sendResponse({ ok: true, data: await api("/api/jobs") });
          break;
        case "cancel":
          sendResponse({
            ok: true,
            data: await api("/api/jobs/cancel", {
              method: "POST",
              body: JSON.stringify({ id: msg.id }),
            }),
          });
          break;
        case "open-folder":
          sendResponse({ ok: true, data: await api("/api/open-folder", { method: "POST" }) });
          break;
        default:
          sendResponse({ ok: false, error: "mensagem desconhecida: " + (msg && msg.type) });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // resposta assíncrona
});
