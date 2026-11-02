/* SPDX-License-Identifier: GPL-3.0-only */
importScripts("rules.js");
const R = SpaceCaptureRules;
const jobs = new Map();
const startingJobs = new Set();
const uploads = new Map();
const restored = chrome.storage.session.get("spaceJobs").then(saved => {
  for (const job of saved.spaceJobs || []) if (job.expires > Date.now()) jobs.set(job.tab_id, job);
});
const persist = () => chrome.storage.session.set({spaceJobs: Array.from(jobs.values())});
const notice = async (job, message, level = "info") => {
  try { await chrome.tabs.sendMessage(job.local_tab_id, {type: "space:notice", job_id: job.id, message, level}); } catch { /* local page closed */ }
};
async function stop(tabId, message = "") {
  const job = jobs.get(tabId);
  if (!job) return;
  jobs.delete(tabId);
  await persist();
  try { await chrome.tabs.sendMessage(tabId, {type: "space:stop"}); } catch { /* task tab closed */ }
  if (message) await notice(job, message, "error");
}
async function prune() {
  for (const [tab, job] of jobs) if (job.expires <= Date.now()) await stop(tab, "捕获已超时。请检查该 Space 是否可播放，然后重新创建下载任务。");
}
function senderIsTask(sender, job) {
  return !!(sender.tab?.id === job.tab_id && R.xURL(sender.url || sender.tab.url));
}
async function forwardCapture(job, payload) {
  if (jobs.get(job.tab_id) !== job) return {active: false};
  let response;
  try {
    response = await fetch(`${job.base_url}/api/space-downloads/${encodeURIComponent(job.id)}/capture`, {
      method: "POST", headers: {"Content-Type": "application/json", "X-Space-Capture-Token": job.capture_token},
      body: JSON.stringify(payload), credentials: "omit", redirect: "error", signal: AbortSignal.timeout(20000)
    });
  } catch {
    if (jobs.get(job.tab_id) !== job) return {active: false};
    await notice(job, "无法把音频资源交给本机服务，请确认启动终端仍在运行。", "error"); return {active: true};
  }
  if (jobs.get(job.tab_id) !== job) return {active: false};
  if (!response.ok) { await stop(job.tab_id, "本机服务拒绝了捕获结果，请回到任务页查看状态。任务内容未发送到其他网站。"); return {active: false}; }
  const result = await response.json().catch(() => ({}));
  const stage = result.stage || result.job?.stage;
  if (["downloading", "completed", "failed", "cancelled", "interrupted"].includes(stage)) {
    await stop(job.tab_id);
    await notice(job, stage === "downloading" ? "播放列表和所需密钥已交给本机，浏览器捕获已停止。" : "浏览器捕获已结束，请在任务页查看结果。");
    return {active: false};
  }
  return {active: true};
}
async function handle(message, sender) {
  await restored;
  await prune();
  const page = R.localPage(sender.url || "");
  if (message.type === "space:hello") return page ? {type: "ready"} : {type: "error", message: "仅接受烤肉台下载页面的请求。"};
  if (message.type === "space:start") {
    if (!page || !R.validJob(message.job)) return {type: "error", message: "下载任务来源或参数无效。"};
    const existing = Array.from(jobs.values()).find(job => job.id === message.job.id);
    if (existing) {
      if (existing.local_tab_id === sender.tab?.id && existing.base_url === page.origin &&
          existing.space_id === message.job.space_id && existing.capture_token === message.job.capture_token) {
        return {type: "started", job_id: existing.id, message: "此任务已在捕获中，请继续使用已打开的 Space 标签页。"};
      }
      return {type: "error", message: "此任务 ID 已在使用，请勿重复启动。"};
    }
    if (startingJobs.has(message.job.id)) return {type: "error", message: "此任务正在打开，请勿重复启动。"};
    if (jobs.size + startingJobs.size >= 4) return {type: "error", message: "已有多个捕获任务，请先完成或取消。"};
    startingJobs.add(message.job.id);
    let tab;
    let job;
    try {
      tab = await chrome.tabs.create({url: "about:blank", active: true});
      job = {...message.job, source_url: R.canonicalSpace(message.job.source_url).url, base_url: page.origin,
        local_tab_id: sender.tab.id, tab_id: tab.id, nonce: crypto.randomUUID(), expires: Date.now() + 10 * 60 * 1000, roots: [], starting: true};
      jobs.set(tab.id, job);
      startingJobs.delete(job.id);
      await persist();
      await chrome.tabs.update(tab.id, {url: job.source_url}); job.starting = false; await persist();
    } catch {
      if (tab) await stop(tab.id);
      return {type: "error", message: "无法打开本次 Space 标签页。"};
    } finally { startingJobs.delete(message.job.id); }
    setTimeout(() => stop(tab.id, "捕获已超时。若网页无法播放，请在任务页查看说明。"), 10 * 60 * 1000);
    return {type: "started", job_id: job.id, message: "已打开专用 Space 标签页；如浏览器未自动播放，请点击本场的播放录音。"};
  }
  if (message.type === "space:cancel") {
    if (!page) return {type: "error", message: "任务来源无效。"};
    for (const [tab, job] of jobs) if (job.id === message.job_id && job.local_tab_id === sender.tab?.id) await stop(tab);
    return {type: "status", job_id: message.job_id, message: "已停止浏览器捕获。"};
  }
  const job = jobs.get(sender.tab?.id);
  if (!job || !senderIsTask(sender, job)) return {active: false};
  if (message.type === "space:config") return {active: true, space_id: job.space_id, nonce: job.nonce, expires: job.expires};
  if (message.type === "space:expired") { await stop(job.tab_id, "捕获已超时，请重新创建下载任务。"); return {active: false}; }
  if (message.type !== "space:capture" || message.nonce !== job.nonce) return {active: false};
  const p = message.payload || {};
  if (p.kind === "scope") {
    const root = R.cdnRoot(p.url);
    if (p.space_id === job.space_id && root && !job.roots.includes(root)) { job.roots.push(root); await persist(); }
    return {active: true};
  }
  let payload;
  if (p.kind === "playlist" && R.playlist(p.url, p.text, job.space_id, job.roots)) payload = {kind: "playlist", url: p.url, text: p.text};
  if (p.kind === "key" && R.keyURL(p.url, job.space_id) && R.validKey(p.key_base64)) payload = {kind: "key", url: p.url, key_base64: p.key_base64};
  if (!payload) return {active: true, rejected: true};
  const queued = (uploads.get(job.tab_id) || Promise.resolve()).catch(() => {}).then(() => forwardCapture(job, payload));
  uploads.set(job.tab_id, queued);
  try { return await queued; }
  finally { if (uploads.get(job.tab_id) === queued) uploads.delete(job.tab_id); }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handle(message || {}, sender).then(sendResponse, () => sendResponse({type: "error", message: "扩展处理任务失败，请重新创建下载任务。"}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => { stop(tabId).catch(() => {}); });
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url === "about:blank" && jobs.get(tabId)?.starting) return;
  if (change.url && jobs.has(tabId) && !R.xURL(change.url)) stop(tabId, "标签页已离开 X，本次捕获已停止。").catch(() => {});
});
