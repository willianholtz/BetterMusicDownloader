/* YouTube/Spotify -> FLAC: ponte entre a extensão e o serviço local (127.0.0.1:8765). */

const API = "http://127.0.0.1:8765";

const YT_MATCH = [
  "*://*.youtube.com/*",
  "*://youtu.be/*",
  "*://*.youtube-nocookie.com/*",
  "*://open.spotify.com/*",
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
  return { created, data };
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
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

function trackJob(id) {
  if (!id || tracking.has(id)) return;
  tracking.add(id);
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs?id=${encodeURIComponent(id)}`);
      if (job.status === "done") {
        clearInterval(timer);
        tracking.delete(id);
        notify(chrome.i18n.getMessage("notifyDone") || "MP3 ready", job.title || "Audio converted successfully.");
      } else if (job.status === "error" || job.status === "cancelled") {
        clearInterval(timer);
        tracking.delete(id);
        if (job.status === "error") notify(chrome.i18n.getMessage("notifyFail") || "Failed", job.error || "Download error.");
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
          sendResponse({ ok: false, error: "mensagem desconhecida" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // resposta assíncrona
});
