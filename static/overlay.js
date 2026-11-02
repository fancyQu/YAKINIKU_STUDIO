const channel = "BroadcastChannel" in window ? new BroadcastChannel("yakiniku-captions") : null;
let currentSession = "";
let lastRevision = -1;
const retiredSessions = new Set();
if (channel) {
  channel.onmessage = (event) => {
    const message = event.data;
    if (!message || message.type !== "caption") return;
    const session = String(message.session_id || "");
    if (session && retiredSessions.has(session)) return;
    if (session !== currentSession) {
      if (currentSession) retiredSessions.add(currentSession);
      currentSession = session;
      lastRevision = -1;
    }
    const revision = Number(message.revision);
    if (Number.isFinite(revision) && revision < lastRevision) return;
    if (Number.isFinite(revision)) lastRevision = revision;
    // The workbench sends one complete display snapshot. The overlay never
    // combines independent transcript and translation events itself.
    document.getElementById("overlay-ja").textContent = String(message.ja || "");
    document.getElementById("overlay-zh").textContent = String(message.zh || "等待下一句话…");
    document.getElementById("overlay-status").textContent = String(message.status || "待机");
    document.getElementById("overlay-dot").classList.toggle("active", Boolean(message.recording));
    const latest = document.getElementById("overlay-latest");
    latest.textContent = message.latest_ja ? `最新识别 · 等待翻译：${message.latest_ja}` : "";
    latest.hidden = !message.latest_ja;
  };
  channel.postMessage({ type: "overlay_ready" });
} else document.getElementById("overlay-help").textContent = "请使用 Edge 浏览器打开字幕窗口。";
let fullscreenPending = false;
function updateFullscreenButton() {
  const fullscreen = Boolean(document.fullscreenElement);
  const button = document.getElementById("overlay-fullscreen");
  button.textContent = fullscreenPending ? "正在切换…" : fullscreen ? "退出全屏" : "全屏";
  button.disabled = fullscreenPending;
  button.setAttribute?.("aria-pressed", String(fullscreen));
  button.setAttribute?.("aria-busy", String(fullscreenPending));
}
document.getElementById("overlay-fullscreen").addEventListener("click", async () => {
  if (fullscreenPending) return;
  fullscreenPending = true;
  updateFullscreenButton();
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { document.getElementById("overlay-help").textContent = "浏览器暂时无法全屏，请使用窗口最大化。"; }
  finally { fullscreenPending = false; updateFullscreenButton(); }
});
document.addEventListener?.("fullscreenchange", updateFullscreenButton);
updateFullscreenButton();
