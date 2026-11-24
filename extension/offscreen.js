/* Documento offscreen: o service worker MV3 nao tem DOM/audio, entao o "ding"
   de conclusao toca aqui. Som sintetizado (Web Audio) — nenhum arquivo de audio. */

let ctx = null;

function tocarDing() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!ctx) ctx = new Ctx();
    if (ctx.state === "suspended") ctx.resume();
    const now = ctx.currentTime;

    // dois tons curtos (A5 + E6), envelope suave sem estalo
    [[880, 0], [1318.5, 0.14]].forEach(([freq, delay]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + delay);
      gain.gain.exponentialRampToValueAtTime(0.28, now + delay + 0.025);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.42);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + delay);
      osc.stop(now + delay + 0.45);
    });
  } catch (_) {}
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "ping-done-sound") { sendResponse(true); return; }
  if (msg.type === "play-done-sound") { tocarDing(); sendResponse(true); }
});

// Quando o documento e' criado com ?play=1, toca imediatamente (evita corrida
// entre a criacao e o envio da mensagem pelo background).
if (new URLSearchParams(location.search).get("play") === "1") tocarDing();
