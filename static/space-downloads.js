import { clockTime, jsonRequest } from "./project-utils.js?v=ui-navigation-1";

export const SPACE_STAGES = {
  waiting_browser: "等待浏览器", capturing: "正在捕获回放", downloading: "下载与转码中",
  completed: "已完成", failed: "下载失败", interrupted: "已中断", cancelled: "已取消",
};
const ACTIVE = new Set(["waiting_browser", "capturing", "downloading"]);
export function canonicalSpaceURL(value) {
  let url;
  try { url = new URL(String(value || "").trim()); }
  catch { throw new Error("请粘贴有效的 X Space 回放链接。"); }
  const match = /^\/i\/spaces\/([a-zA-Z0-9]{13})\/?$/.exec(url.pathname);
  if (url.protocol !== "https:" || !["x.com", "twitter.com", "www.x.com", "www.twitter.com"].includes(url.hostname) || url.port || url.username || url.password || !match) throw new Error("请粘贴有效的 X Space 回放链接。");
  return `https://x.com/i/spaces/${match[1]}`;
}
export function validExtensionReply(event, origin, requestId) {
  return event.source === window && event.origin === origin && event.data?.channel === "yakiniku-space-extension" && event.data.request_id === requestId;
}

const $ = (id) => document.getElementById(id);
let connected = false, submitting = false, pollTimer = null, loading = false, checking = false;
const pending = new Map();
const knownJobs = new Set();
const cancelling = new Set();
const completedRows = new Map();
function notice(message, error = false) {
  $("space-notice").textContent = message;
  $("space-notice").dataset.level = error ? "error" : "success";
  $("space-notice").hidden = !message;
}
function updateControls() {
  $("space-start").disabled = !connected || submitting;
  $("space-url").disabled = submitting;
  $("space-start").textContent = submitting ? "正在建立下载任务…" : "打开回放并下载 MP3";
  $("space-form").setAttribute?.("aria-busy", String(submitting));
  $("space-start").setAttribute?.("aria-busy", String(submitting));
  $("extension-check").disabled = checking || submitting;
  $("extension-check").textContent = checking ? "检测中…" : "检测连接";
  $("extension-check").setAttribute?.("aria-busy", String(checking));
}
function bridge(type, job, timeout = 5000) {
  const request_id = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(request_id); reject(new Error("浏览器组件未响应，请加载组件并刷新此页。")); }, timeout);
    pending.set(request_id, { resolve, reject, timer });
    window.postMessage({ channel: "yakiniku-space-ui", type, request_id, ...(job ? { job, job_id: job.id } : {}) }, location.origin);
  });
}
async function checkExtension({ manual = false } = {}) {
  if (checking || submitting) return;
  checking = true; $("extension-status").textContent = "正在检测浏览器组件…"; updateControls();
  try {
    const reply = await bridge("hello", null, 1600);
    connected = reply.type === "ready";
  } catch { connected = false; }
  finally { checking = false; }
  $("extension-status").dataset.connected = String(connected);
  $("extension-status").textContent = connected ? "浏览器组件已连接" : "浏览器组件未连接";
  $("space-setup").open = !connected;
  updateControls();
  if (manual) notice(connected ? "浏览器组件已连接，可以开始下载。" : "未连接浏览器组件，请按下方首次使用说明加载组件，再重新检测。", !connected);
}
function fileURL(id, name) {
  return `/api/space-downloads/${encodeURIComponent(id)}/files/${encodeURIComponent(name)}`;
}
function render(jobs) {
  const rows = [];
  const present = new Set(jobs.map((job) => job.id));
  for (const id of completedRows.keys()) if (!present.has(id)) completedRows.delete(id);
  if (!jobs.length) {
    const p = document.createElement("p"); p.className = "project-empty"; p.textContent = "还没有下载任务，粘贴一条 Space 回放链接开始。"; rows.push(p);
  }
  for (const job of jobs) {
    const signature = JSON.stringify(job);
    const saved = completedRows.get(job.id);
    if (job.stage === "completed" && saved?.signature === signature) { rows.push(saved.node); continue; }
    completedRows.delete(job.id);
    const article = document.createElement("article"); article.className = "import-project";
    const top = document.createElement("div"); top.className = "import-project-top";
    const title = document.createElement("strong"); title.className = "import-project-title space-job-id"; title.textContent = `Space ${job.space_id}`;
    const status = document.createElement("span"); status.className = "project-status"; status.dataset.stage = job.stage; status.textContent = SPACE_STAGES[job.stage] || "等待处理";
    top.append(title, status); article.append(top);
    const meta = document.createElement("p"); meta.className = "project-meta";
    meta.textContent = [job.duration_ms ? clockTime(job.duration_ms) : "时长待确认", job.segments ? `${job.segments} 个分片` : "", job.message || ""].filter(Boolean).join(" · "); article.append(meta);
    const actions = document.createElement("div"); actions.className = "project-actions";
    if (job.stage === "completed" && (job.files || []).some((f) => f.name === "audio.mp3")) {
      const player = document.createElement("audio"); player.controls = true; player.preload = "none"; player.className = "space-job-player"; player.src = fileURL(job.id, "audio.mp3"); article.append(player);
      const link = document.createElement("a"); link.href = fileURL(job.id, "audio.mp3"); link.download = `Space-${job.space_id}.mp3`; link.textContent = "下载 MP3"; actions.append(link);
      if ((job.files || []).some((f) => f.name === "audio.json")) { const report = document.createElement("a"); report.href = fileURL(job.id, "audio.json"); report.download = "audio.json"; report.textContent = "完整性报告"; actions.append(report); }
    }
    if (ACTIVE.has(job.stage)) {
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "button button-outline space-cancel"; cancel.textContent = "取消任务";
      cancel.disabled = cancelling.has(job.id); cancel.textContent = cancelling.has(job.id) ? "正在取消…" : "取消任务";
      cancel.setAttribute?.("aria-busy", String(cancelling.has(job.id)));
      cancel.addEventListener("click", async () => {
        if (cancelling.has(job.id)) return;
        cancelling.add(job.id); cancel.disabled = true; cancel.textContent = "正在取消…"; cancel.setAttribute?.("aria-busy", "true");
        try {
          await jsonRequest(`/api/space-downloads/${encodeURIComponent(job.id)}/cancel`, {method:"POST"});
          bridge("cancel", {id:job.id}, 1500).catch(() => {});
          notice("该下载任务已取消，已完成的其他文件仍保留。");
          await loadJobs();
        } catch (error) { notice(error.message, true); }
        finally { cancelling.delete(job.id); cancel.disabled = false; cancel.textContent = "取消任务"; cancel.setAttribute?.("aria-busy", "false"); }
      }); actions.append(cancel);
    }
    article.append(actions); rows.push(article);
    if (job.stage === "completed") completedRows.set(job.id, { signature, node: article });
  }
  // Keep unchanged completed players attached. Replacing the whole list on
  // every progress poll reloads their audio and interrupts ongoing listening.
  const container = $("space-jobs");
  const retained = new Set(rows);
  for (const child of Array.from(container.children)) if (!retained.has(child)) child.remove();
  rows.forEach((row, index) => {
    const current = container.children[index];
    if (current !== row) container.insertBefore(row, current || null);
  });
}
async function loadJobs({ manual = false } = {}) {
  if (loading) return;
  loading = true; clearTimeout(pollTimer);
  $("space-refresh").disabled = true; $("space-refresh").textContent = "刷新中…"; $("space-refresh").setAttribute?.("aria-busy", "true");
  try { const body = await jsonRequest("/api/space-downloads"); const jobs = body.jobs || []; for (const job of jobs) knownJobs.add(job.id); render(jobs); if (jobs.some((j) => ACTIVE.has(j.stage))) pollTimer = setTimeout(loadJobs, 2000); if (manual) notice("下载任务列表已更新。"); }
  catch (error) { notice(error.message, true); pollTimer = setTimeout(loadJobs, 5000); }
  finally { loading = false; $("space-refresh").disabled = false; $("space-refresh").textContent = "刷新"; $("space-refresh").setAttribute?.("aria-busy", "false"); }
}
async function start(event) {
  event.preventDefault();
  if (submitting || !connected) return;
  let job = null;
  try {
    const url = canonicalSpaceURL($("space-url").value);
    submitting = true; updateControls(); notice("正在建立下载任务…");
    job = await jsonRequest("/api/space-downloads", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url})});
    knownJobs.add(job.id);
    await bridge("start", {id:job.id, space_id:job.space_id, source_url:job.source_url, capture_token:job.capture_token});
    notice("已打开专用回放标签页。若没有自动开始，请在该页点击「播放录音」。");
    $("space-url").value = "";
  } catch (error) {
    if (job?.id) { await jsonRequest(`/api/space-downloads/${encodeURIComponent(job.id)}/cancel`, {method:"POST"}).catch(() => {}); bridge("cancel", {id:job.id}, 1000).catch(() => {}); }
    notice(error.message, true);
  } finally { submitting = false; updateControls(); await loadJobs(); }
}
if (typeof document !== "undefined" && $("space-form")) {
  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin || event.data?.channel !== "yakiniku-space-extension") return;
    const entry = pending.get(event.data?.request_id);
    if (!entry) {
      if (!event.data.request_id && knownJobs.has(event.data.job_id) && ["error", "status"].includes(event.data.type)) {
        notice(event.data.message || "浏览器捕获状态已更新。", event.data.type === "error");
        if (event.data.type === "error") jsonRequest(`/api/space-downloads/${encodeURIComponent(event.data.job_id)}/cancel`, {method:"POST"}).finally(loadJobs).catch(() => {});
        else loadJobs();
      }
      return;
    }
    if (!validExtensionReply(event, location.origin, event.data.request_id)) return;
    clearTimeout(entry.timer); pending.delete(event.data.request_id);
    if (event.data.type === "error") entry.reject(new Error(event.data.message || "浏览器捕获无法启动。")); else entry.resolve(event.data);
  });
  $("space-form").addEventListener("submit", start);
  $("space-refresh").addEventListener("click", () => loadJobs({ manual: true }));
  $("extension-check").addEventListener("click", () => checkExtension({ manual: true }));
  checkExtension(); loadJobs();
}
