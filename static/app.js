const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "settings-form", "api-key", "transcription-model", "translation-model", "glossary",
  "save-settings", "settings-status", "settings-dialog", "open-settings", "close-settings",
  "session-title", "start-button", "stop-button", "demo-button", "overlay-button",
  "fullscreen-button", "recording-status", "status-dot", "timer", "audio-level", "waveform",
  "live-ja", "live-zh", "transcript-list", "empty-transcript", "segment-count", "source-label",
  "notice", "recordings-list", "refresh-recordings", "connection-status", "latency-label",
  "provider-preset", "api-base-url", "transcription-mode", "translation-api", "segment-seconds",
  "segment-settings", "transcription-mode-help", "translation-api-help", "api-destinations", "test-connection", "test-connection-help",
  "transcription-base-url", "transcription-api-key", "translation-base-url", "translation-api-key",
  "clear-transcription-api-key", "clear-translation-api-key",
  "fetch-translation-models", "translation-model-list", "translation-model-search", "translation-model-status",
  "fetch-transcription-models", "transcription-model-list", "transcription-model-search", "transcription-model-status",
  "local-model-settings", "local-transcription-model", "cloud-transcription-model", "transcription-model-picker", "transcription-api-settings",
  "live-model-name", "live-model-state", "live-translation-state", "prepare-live-model", "open-model-download", "live-prepare-help", "live-pending-count", "latest-recognized", "latest-recognized-text",
].map((id) => [id, $(id)]));

const emptyTranscript = ui["empty-transcript"].cloneNode(true);
const emptyLibrary = ui["recordings-list"].firstElementChild.cloneNode(true);
const initialStartLabel = ui["start-button"].cloneNode(true);
const initialDemoLabel = ui["demo-button"].cloneNode(true);
const BUFFER_LIMIT = 4 * 1024 * 1024;
const CAPTION_LIMIT = 300;
let settings = null;
let liveSettings = null;
let liveHealth = {};
let liveModelStatus = { state: "not_prepared" };
let modelPreparing = false;
let readinessTimer = null;
let completionPoll = null;
let settingsDirty = false;
let settingsBusy = false;
let recordingsLoadTask = null;
let refreshingRecordings = false;
let fullscreenPending = false;
const modelPickers = Object.fromEntries(["transcription", "translation"].map((role) => [role, {
  models: [], busy: false, revision: 0,
  label: role === "transcription" ? "语音识别" : "文字翻译",
  capability: role === "transcription" ? "日语识别和所选语音接口" : "文字翻译和所选翻译接口",
}]));
function modelListsBusy() { return Object.values(modelPickers).some((picker) => picker.busy); }
let phase = "idle";
let capture = null;
let demoTimers = [];
let demoActive = false;
let renderPending = false;
let segments = new Map();
let seenIds = new Set();
let liveSegmentId = null;
let latestStart = -1;
let latestTranslatedStart = -1;
let latestTranslatedSequence = -1;
let latestRecognizedSequence = -1;
let captionRevision = 0;
let waveformLevels = Array(65).fill(0);
let captions = { type: "caption", session_id: "", revision: 0, ja: "", zh: "等待直播开始", status: "待机", recording: false, latest_ja: "" };
const captionChannel = "BroadcastChannel" in window ? new BroadcastChannel("yakiniku-captions") : null;
if (captionChannel) captionChannel.onmessage = (event) => {
  if (event.data?.type === "overlay_ready") captionChannel.postMessage(captions);
};

function showNotice(message, level = "info") {
  ui.notice.textContent = String(message);
  ui.notice.dataset.level = level;
  ui.notice.hidden = !message;
}

async function requestJSON(path, options = {}, timeout = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
      credentials: "same-origin",
      signal: controller.signal,
    });
    let body;
    try { body = await response.json(); } catch { body = {}; }
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : body.message;
      throw new Error(detail || `本机服务返回错误（${response.status}），请稍后重试。`);
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("本机服务响应超时，请检查服务是否仍在运行。");
    if (error instanceof TypeError) throw new Error("无法连接本机服务，请保持启动终端运行。");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function timecode(milliseconds = 0) {
  const seconds = Math.floor(Math.max(0, Number(milliseconds) || 0) / 1000);
  const hours = Math.floor(seconds / 3600);
  return `${String(hours).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function setTimer(milliseconds) { ui.timer.textContent = timecode(milliseconds); }

function setStatus(label, connection, recording = false) {
  ui["recording-status"].textContent = label;
  ui["connection-status"].textContent = connection;
  ui["status-dot"].classList.toggle("recording", recording);
  captions.status = label;
  captions.recording = recording;
  publishCaptions();
}

function publishCaptions() {
  captions = { ...captions, session_id: capture?.session?.id || captions.session_id || "", revision: ++captionRevision };
  captionChannel?.postMessage(captions);
}

function setCaptions(ja, zh) {
  ui["live-ja"].textContent = ja || "日语原文将在这里实时显示";
  ui["live-zh"].textContent = zh || "等待下一句话…";
  captions = { ...captions, ja: ja || "", zh: zh || "等待下一句话…" };
  publishCaptions();
}

function setLatestRecognition(ja, visible = true) {
  const text = visible ? String(ja || "") : "";
  ui["latest-recognized"].hidden = !text;
  ui["latest-recognized-text"].textContent = text;
  captions.latest_ja = text;
  publishCaptions();
}

function modelState() {
  const state = liveModelStatus.state || liveModelStatus.status || "not_prepared";
  if (state === "ready" && liveSettings && ((liveModelStatus.model && liveModelStatus.model !== liveSettings.model) || (liveModelStatus.backend && liveModelStatus.backend !== liveSettings.backend))) return "not_prepared";
  return state;
}
function liveBusyMessage() {
  if (liveHealth.model_downloading || liveModelStatus.active_download) return "本机模型正在下载或校验。可打开「模型下载」查看进度，完成后再加载并预热。";
  if (liveHealth.model_preparing) return "本机模型仍在后台加载与预热，关闭设置页也不会中断。准备完成后可开始录制。";
  if (liveHealth.subtitle_active) return "字幕补全仍在后台运行，完成后可开始新直播。可在项目预览中查看进度。";
  if (liveHealth.import_active) return "完整音频项目仍在处理，完成后可开始新直播。可在音频翻译模块查看进度。";
  return "本机已有录制或字幕任务正在处理，完成后可开始新直播。进度可在项目列表查看。";
}
function liveModelFilesReady() {
  if (!liveSettings || ["downloading", "verifying"].includes(liveModelStatus.download?.state)) return false;
  return liveSettings.backend !== "mlx" || Boolean(liveModelStatus.prepared_files || modelState() === "ready" || liveModelStatus.download?.state === "downloaded");
}
function renderLiveReadiness() {
  const state = modelState();
  const names = { "mlx-whisper-small": "MLX Whisper Small", "mlx-whisper-large-v3-turbo": "MLX Whisper large-v3-turbo", tiny: "Tiny · CPU 兼容模式" };
  const model = liveSettings?.model || liveModelStatus.model || "";
  ui["live-model-name"].textContent = names[model] || model || "尚未选择模型";
  const labels = { not_prepared: "未准备", unprepared: "未准备", loading: "加载与预热中", ready: "已就绪", failed: "准备失败" };
  ui["live-model-state"].textContent = labels[state] || "未准备";
  ui["live-model-state"].dataset.state = state;
  const localTranslation = liveSettings?.translation_backend === "local";
  ui["live-translation-state"].textContent = localTranslation
    ? `${liveSettings.translation_ready ? "本地已就绪" : "本地待加载"} · ${liveSettings.local_translation_model || "本地翻译模型"}`
    : liveSettings?.translation_ready ? `已配置 · ${liveSettings.translation_route?.model || "直播翻译模型"}` : "未配置直播翻译";
  ui["live-prepare-help"].textContent = phase === "idle" && liveSettings?.locked ? liveBusyMessage() : state === "failed" && (liveModelStatus.error || liveModelStatus.message) ? liveModelStatus.error || liveModelStatus.message : liveSettings?.backend === "mlx" && !liveModelFilesReady() ? "本机尚无完整模型文件。请点击「模型下载」，由你决定是否下载；完成后再加载并预热。" : liveModelStatus.message || liveModelStatus.error || (state === "ready" ? "本机日语分段识别 → 一次中文翻译；音频不上传。" : "开始前请加载并预热本机模型，此操作不会下载权重。");
  ui["prepare-live-model"].textContent = modelPreparing || state === "loading" || liveHealth.model_preparing ? "正在加载…" : state === "ready" ? "重新检查" : "加载并预热";
  ui["prepare-live-model"].disabled = phase !== "idle" || modelPreparing || state === "loading" || !liveSettings || liveSettings.locked || !liveModelFilesReady();
  ui["open-model-download"].hidden = liveSettings?.backend === "tiny" && !liveModelStatus.active_download && !liveHealth.model_downloading;
  ui["open-model-download"].textContent = liveHealth.model_downloading || liveModelStatus.active_download ? "查看下载进度" : liveSettings?.backend === "mlx" && !liveModelFilesReady() ? "下载所选模型" : "模型下载";
}

async function loadLiveReadiness() {
  const [configuration, status, health] = await Promise.all([requestJSON("/api/live-settings"), requestJSON("/api/live-model/status"), requestJSON("/api/health")]);
  liveHealth = health;
  liveSettings = { ...configuration, locked: Boolean(configuration.locked || health.active || health.import_active || health.subtitle_active || health.model_preparing || health.model_downloading || status.active_download) };
  liveModelStatus = status;
  renderLiveReadiness();
  updateControls();
  clearTimeout(readinessTimer);
  if (modelState() === "loading" || (phase === "idle" && liveSettings.locked)) readinessTimer = setTimeout(() => {
    if (liveSettings.locked) loadRecordings().catch(() => {});
    loadLiveReadiness().catch((error) => showNotice(error.message, "error"));
  }, modelState() === "loading" ? 1200 : 2500);
}

async function prepareLiveModel() {
  if (phase !== "idle" || modelPreparing || liveSettings?.locked) return;
  if (!liveModelFilesReady()) { showNotice("请先点击「模型下载」下载所选模型，文件校验完成后再加载并预热。", "error"); return; }
  modelPreparing = true;
  liveModelStatus = { ...liveModelStatus, state: "loading", message: "正在加载并预热本机模型，首次准备需要稍候。" };
  renderLiveReadiness(); updateControls();
  try {
    liveModelStatus = await requestJSON("/api/live-model/prepare", { method: "POST", body: "{}" }, 120000);
    await loadLiveReadiness();
    if (modelState() === "ready") showNotice("本机模型已就绪。请自行选择正在播放直播的 Edge 标签页。", "success");
  } catch (error) {
    liveModelStatus = { ...liveModelStatus, state: "failed", message: error.message };
    showNotice(error.message, "error");
  } finally { modelPreparing = false; renderLiveReadiness(); updateControls(); }
}

function updateControls() {
  const idle = phase === "idle";
  const modelListBusy = modelListsBusy();
  ui["start-button"].disabled = !idle || !liveSettings || liveSettings.locked || !liveSettings.translation_ready || modelState() !== "ready" || modelPreparing;
  ui["stop-button"].disabled = phase !== "recording";
  ui["demo-button"].disabled = !idle && phase !== "demo";
  ui["session-title"].disabled = !idle;
  ui["save-settings"].disabled = !idle || settingsBusy || modelListBusy;
  ui["test-connection"].disabled = !idle || !settings || settingsDirty || settingsBusy || modelListBusy;
  const checkHelp = !idle ? "请先结束当前录制或字幕演示，再检查 API 配置。"
    : !settings ? "正在加载设置，请稍候。"
    : settingsBusy ? "正在保存或检查设置，请稍候。"
    : modelListBusy ? "正在获取模型，完成后即可继续。"
    : settingsDirty ? "当前修改尚未保存。请先点击下方「保存设置」，保存成功后即可检查。"
    : "检查已保存的地址、鉴权与模型列表，不代替实际语音测试。";
  ui["test-connection-help"].textContent = checkHelp;
  ui["test-connection"].title = checkHelp;
  for (const [role, picker] of Object.entries(modelPickers)) {
    ui[`fetch-${role}-models`].disabled = !idle || settingsBusy || modelListBusy || (role === "transcription" && ui["transcription-mode"].value === "local");
    ui[`fetch-${role}-models`].textContent = picker.busy ? "正在获取…" : "获取模型";
    ui[`${role}-model-list`].disabled = !idle || settingsBusy || modelListBusy || !picker.models.length;
    ui[`${role}-model-search`].disabled = !idle || settingsBusy || modelListBusy;
  }
  ui["local-transcription-model"].disabled = !idle || settingsBusy;
  if (phase === "connecting") ui["start-button"].textContent = "正在连接本机录制…";
  else if (phase === "finalizing") ui["start-button"].textContent = "正在保存已接收音频…";
  else if (phase === "backfilling") ui["start-button"].textContent = "音频已保存 · 正在补全字幕";
  else ui["start-button"].replaceChildren(...Array.from(initialStartLabel.childNodes, (node) => node.cloneNode(true)));
  ui["stop-button"].textContent = phase === "finalizing" ? "正在保存，请稍候…" : "结束并保存";
  renderLiveReadiness();
}

function resetTranscript() {
  segments.clear();
  seenIds.clear();
  liveSegmentId = null;
  latestStart = -1;
  latestTranslatedStart = -1;
  latestTranslatedSequence = -1;
  latestRecognizedSequence = -1;
  setLatestRecognition("");
  ui["segment-count"].textContent = "0 句";
  ui["transcript-list"].replaceChildren(emptyTranscript.cloneNode(true));
}

function translationText(segment) {
  if (segment.zh) return segment.zh;
  if (["failed", "error"].includes(segment.translation_status)) return "翻译暂时失败，可在录制结束后重试。";
  if (segment.translation_status === "deferred" || segment.asr_status === "deferred") return "已保留音频，停止后补全。";
  if (["no_speech", "skipped"].includes(segment.asr_status)) return "无语音 · 已跳过";
  if (!segment.ja && !segment.ja_raw) return segment.asr_status === "failed" ? "识别失败，可在录制结束后重试。" : "正在识别…";
  return "正在翻译…";
}

function segmentOrder(a, b) {
  const timed = (Number(a.start_ms) || 0) - (Number(b.start_ms) || 0);
  if (timed) return timed;
  const sequence = (Number(a.sequence) || 0) - (Number(b.sequence) || 0);
  return sequence || String(a.id).localeCompare(String(b.id));
}

function reportCaptionDisplayed(segment) {
  const current = capture;
  if (!current?.session?.id || !current.socket || current.socket.readyState !== WebSocket.OPEN) return;
  const id = String(segment.segment_id || segment.id);
  current.displayedIds ||= new Set();
  if (current.displayedIds.has(id)) return;
  const receivedAt = performance.now();
  // A second animation frame lets the first frame paint the selected pair.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (capture !== current || captions.session_id !== current.session.id || captions.segment_id !== id || current.socket.readyState !== WebSocket.OPEN) return;
    current.socket.send(JSON.stringify({ type: "caption_displayed", session_id: current.session.id, segment_id: id, sequence: segment.sequence, render_delay_ms: Math.max(0, performance.now() - receivedAt) }));
    current.displayedIds.add(id);
  }));
}

function updateSegment(segment) {
  if (!segment || !(segment.id || segment.segment_id)) return;
  if (segment.session_id && capture?.session?.id && String(segment.session_id) !== String(capture.session.id)) return;
  const id = String(segment.segment_id || segment.id);
  const liveIsUncommitted = Boolean(liveSegmentId && !segments.has(liveSegmentId));
  if (segment.ja || segment.ja_raw) seenIds.add(id);
  segments.set(id, { ...segments.get(id), ...segment, id });
  if (segments.size > CAPTION_LIMIT) {
    const oldest = [...segments.values()].sort(segmentOrder)[0];
    segments.delete(String(oldest.id));
  }
  ui["segment-count"].textContent = `${seenIds.size} 句`;
  const current = segments.get(id) || segment;
  const start = Number(current.start_ms) || 0;
  const sequence = Number.isFinite(Number(current.sequence)) ? Number(current.sequence) : start;
  const ja = current.ja || current.ja_raw || "";
  if (["local", "mlx", "tiny"].includes(capture?.recognitionMode) || capture?.session?.live_snapshot) {
    // Keep a translated pair visible while newer recognition waits for GPT.
    // Independent translation requests may finish in a different order.
    if (ja && sequence >= latestRecognizedSequence) {
      latestRecognizedSequence = sequence;
      setLatestRecognition(ja, !current.zh && latestTranslatedSequence >= 0);
    }
    if ((current.zh && sequence >= latestTranslatedSequence) || (ja && !current.zh && latestTranslatedSequence < 0 && sequence >= latestRecognizedSequence)) {
      if (current.zh) {
        latestTranslatedStart = start;
        latestTranslatedSequence = sequence;
        if (sequence >= latestRecognizedSequence) setLatestRecognition("");
      }
      latestStart = Math.max(latestStart, start);
      liveSegmentId = id;
      captions.segment_id = id;
      captions.sequence = sequence;
      setCaptions(ja, translationText(current));
      if (current.zh) reportCaptionDisplayed(current);
    }
  } else if (id === liveSegmentId || (!liveIsUncommitted && start >= latestStart)) {
    latestStart = start;
    liveSegmentId = id;
    setCaptions(current.ja, translationText(current));
  }
  if (!renderPending) {
    renderPending = true;
    requestAnimationFrame(renderTranscript);
  }
}

function renderTranscript() {
  renderPending = false;
  const list = ui["transcript-list"];
  const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
  const fragment = document.createDocumentFragment();
  const ordered = [...segments.values()].sort(segmentOrder);
  for (const segment of ordered) {
    if (["no_speech", "skipped"].includes(segment.asr_status) && !segment.ja && !segment.ja_raw && !segment.zh) continue;
    const row = document.createElement("article");
    row.className = "transcript-item";
    const timestamp = document.createElement("time");
    timestamp.className = "transcript-time";
    timestamp.textContent = timecode(segment.start_ms);
    const body = document.createElement("div");
    const ja = document.createElement("p");
    ja.className = "transcript-ja";
    ja.textContent = segment.ja || segment.ja_raw || "";
    const zh = document.createElement("p");
    zh.className = "transcript-zh";
    zh.textContent = translationText(segment);
    if (!segment.zh) zh.classList.add(["failed", "error"].includes(segment.translation_status) ? "error-text" : "pending-text");
    body.append(ja, zh);
    row.append(timestamp, body);
    fragment.append(row);
  }
  list.replaceChildren(fragment);
  if (follow) list.scrollTop = list.scrollHeight;
}

function drawWaveform() {
  const canvas = ui.waveform;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext("2d");
  const dark = document.documentElement?.dataset?.theme === "dark";
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = dark ? "#ffffff20" : "#82926830";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke();
  const gap = width / waveformLevels.length;
  ctx.strokeStyle = dark ? (phase === "demo" ? "#ec926e" : "#b7b7b7") : (phase === "demo" ? "#c1b584" : "#a3b78b");
  ctx.lineWidth = Math.max(1.3, Math.min(2.2, gap / 3));
  ctx.lineCap = "round";
  for (let index = 0; index < waveformLevels.length; index++) {
    const amplitude = Math.max(1, Math.min(height * .43, waveformLevels[index] * height * 1.6));
    const x = gap * (index + .5);
    ctx.beginPath(); ctx.moveTo(x, height / 2 - amplitude); ctx.lineTo(x, height / 2 + amplitude); ctx.stroke();
  }
}

function showAudioLevel(rms) {
  ui["audio-level"].value = Math.min(1, Math.max(0, rms * 4));
  waveformLevels.push(rms);
  waveformLevels.shift();
  drawWaveform();
}

function displayDestination(value) {
  if (!value.trim()) return "尚未填写 API 地址";
  try {
    const url = new URL(value.trim());
    return `${url.origin}${url.pathname.replace(/\/$/, "")}`;
  } catch { return "请填写有效的 API 地址"; }
}

function updateSettingsHints() {
  const local = ui["transcription-mode"].value === "local";
  const segmented = ui["transcription-mode"].value === "segmented";
  ui["segment-settings"].hidden = !(segmented || local);
  ui["local-model-settings"].hidden = !local;
  ui["cloud-transcription-model"].hidden = local;
  ui["transcription-model-picker"].hidden = local;
  ui["transcription-api-settings"].hidden = local;
  ui["transcription-mode-help"].textContent = local
    ? "日语识别在本机运行，每 3–5 秒提交一段语音；识别文本交给 GPT 校正和翻译。首次加载模型需稍候，实际延迟取决于电脑速度和翻译 API。"
    : segmented
    ? "分段转写每 3–10 秒上传一小段音频，需要平台提供 /audio/transcriptions 和支持日语识别的语音模型。模型名称与接口支持请以服务商文档为准。"
    : "实时转写需要平台提供 Realtime 转写接口和对应的语音模型。普通文字模型不能代替语音模型，兼容 OpenAI 文字接口也不代表支持语音转写。";
  ui["translation-api-help"].textContent = ui["translation-api"].value === "chat_completions"
    ? "Chat Completions 是兼容平台常用的文字接口，需要支持 /chat/completions。填写该平台实际可用的翻译模型。"
    : "Responses 接口需要平台支持 /responses。填写你在该平台实际可用的翻译模型。";
  const common = ui["api-base-url"].value.trim();
  const speech = ui["transcription-base-url"].value.trim() || common;
  const translation = ui["translation-base-url"].value.trim() || common;
  ui["api-destinations"].textContent = `直播音频 → ${local ? "本机识别（不上传）" : displayDestination(speech)}\n日语文本 → ${displayDestination(translation)}${local ? "（GPT 校正与中文翻译）" : ""}\n以上为当前表单配置；保存后生效。云端服务按平台规则计费。`;
}

function markSettingsDirty(event) {
  const id = event?.target?.id;
  for (const role of Object.keys(modelPickers)) {
    if (["api-base-url", "api-key", `${role}-base-url`, `${role}-api-key`, `clear-${role}-api-key`].includes(id)) {
      invalidateModelPicker(role, "API 地址或密钥已修改，请重新获取模型列表。");
    }
  }
  settingsDirty = true;
  ui["settings-status"].textContent = "配置有未保存的修改，请先保存后再检查 API 配置。";
  ui["settings-status"].dataset.level = "info";
  updateSettingsHints();
  updateControls();
}

function renderModelPicker(role) {
  const picker = modelPickers[role];
  const query = String(ui[`${role}-model-search`].value || "").trim().toLowerCase();
  const visible = picker.models.filter((name) => name.toLowerCase().includes(query));
  const fragment = document.createDocumentFragment();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = picker.models.length ? visible.length ? `请选择${picker.label}模型` : "没有匹配的模型" : "先获取模型列表";
  fragment.append(placeholder);
  for (const name of visible) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    fragment.append(option);
  }
  ui[`${role}-model-list`].replaceChildren(fragment);
  const current = String(ui[`${role}-model`].value || "").trim();
  ui[`${role}-model-list`].value = visible.includes(current) ? current : "";
  ui[`${role}-model-search`].hidden = !picker.models.length;
  updateControls();
}

function invalidateModelPicker(role, message) {
  const picker = modelPickers[role];
  picker.revision += 1;
  picker.busy = false;
  picker.models = [];
  ui[`${role}-model-search`].value = "";
  ui[`${role}-model-status`].textContent = message || `获取所配置${picker.label}服务返回的全部模型，无需先保存。请按服务商文档选择支持${picker.capability}的模型；列表本身不保证模型具备这些能力，也可在上方手动填写。`;
  ui[`${role}-model-status`].dataset.level = "info";
  renderModelPicker(role);
}

function invalidateModelPickers() {
  for (const role of Object.keys(modelPickers)) invalidateModelPicker(role);
}

async function fetchModels(role) {
  if (phase !== "idle" || settingsBusy || modelListsBusy() || (role === "transcription" && ui["transcription-mode"].value === "local")) return;
  const picker = modelPickers[role];
  const revision = ++picker.revision;
  picker.busy = true;
  updateControls();
  ui[`${role}-model-status`].textContent = `正在读取当前${picker.label}服务的模型列表…`;
  ui[`${role}-model-status`].dataset.level = "info";
  try {
    const payload = {
      api_base_url: ui["api-base-url"].value.trim(),
      api_key: ui["api-key"].value.trim(),
      [`${role}_base_url`]: ui[`${role}-base-url`].value.trim(),
      [`${role}_api_key`]: ui[`${role}-api-key`].value.trim(),
      [`clear_${role}_api_key`]: ui[`clear-${role}-api-key`].checked,
    };
    if (role === "transcription") payload.role = role;
    const result = await requestJSON("/api/settings/models", { method: "POST", body: JSON.stringify(payload) }, 30000);
    if (revision !== picker.revision) return;
    if (!Array.isArray(result.models) || result.models.some((name) => typeof name !== "string")) throw new Error("服务未返回有效的模型列表，请核对 API 地址。");
    picker.models = [...new Set(result.models.filter((name) => name.trim()))];
    ui[`${role}-model-search`].value = "";
    renderModelPicker(role);
    const current = ui[`${role}-model`].value.trim();
    const missing = current && !picker.models.includes(current) ? "当前填写的模型未在列表中，请核对服务商文档。" : "";
    ui[`${role}-model-status`].textContent = picker.models.length
      ? `已获取 ${picker.models.length} 个模型。${missing}请按服务商文档选择支持${picker.capability}的模型，选择后保存设置。列表本身不证明接口支持。`
      : "平台返回的模型列表为空。可以稍后重新获取，或在上方手动填写平台提供的模型名称。";
    ui[`${role}-model-status`].dataset.level = picker.models.length && !missing ? "success" : "info";
  } catch (error) {
    if (revision !== picker.revision) return;
    ui[`${role}-model-status`].textContent = `${error.message} 可在上方手动填写模型名称。`;
    ui[`${role}-model-status`].dataset.level = "error";
  } finally {
    if (revision === picker.revision) { picker.busy = false; updateControls(); }
  }
}

function chooseModel(role, event) {
  event?.stopPropagation();
  const model = ui[`${role}-model-list`].value;
  if (!modelPickers[role].models.includes(model)) return;
  ui[`${role}-model`].value = model;
  markSettingsDirty();
}

function fetchTranslationModels() { return fetchModels("translation"); }
function fetchTranscriptionModels() { return fetchModels("transcription"); }

function changeTranscriptionMode() {
  const local = ui["transcription-mode"].value === "local";
  if (local) {
    ui["transcription-model"].value = ui["local-transcription-model"].value || "tiny";
  } else if (["tiny", "base"].includes(ui["transcription-model"].value)) {
    ui["transcription-model"].value = "";
  }
  invalidateModelPicker("transcription");
  markSettingsDirty();
}

function applyProviderPreset() {
  const local = ui["provider-preset"].value === "local";
  const custom = ui["provider-preset"].value === "custom";
  ui["transcription-mode"].value = local ? "local" : custom ? "segmented" : "realtime";
  if (local) { ui["local-transcription-model"].value = "tiny"; ui["transcription-model"].value = "tiny"; }
  else if (["tiny", "base"].includes(ui["transcription-model"].value)) ui["transcription-model"].value = "";
  if (!local) ui["translation-api"].value = custom ? "chat_completions" : "responses";
  for (const role of Object.keys(modelPickers)) renderModelPicker(role);
  markSettingsDirty();
  ui["settings-status"].textContent = local
    ? "已选择本机日语识别，识别文本交给 GPT 校正和中文翻译。请保存设置后开始录制。"
    : custom
    ? "已选择通用兼容接口方式。请自行填写 API 地址、密钥，并从服务获取或按文档填写可用模型；不会自动替换模型。"
    : "已选择 OpenAI 实时接口方式。请确认服务商支持 Realtime 转写，并选择该服务实际可用的语音模型；不会自动替换模型。";
}

function fillSettings() {
  if (!settings) return;
  invalidateModelPickers();
  settingsDirty = false;
  ui["api-base-url"].value = settings.api_base_url || "";
  ui["api-key"].value = "";
  ui["api-key"].placeholder = (settings.shared_key_configured ?? settings.key_configured) ? "已配置共用密钥；留空即可保留" : "填写服务平台的共用密钥，或分别配置";
  ui["transcription-mode"].value = settings.transcription_mode || "local";
  ui["translation-api"].value = settings.translation_api || "responses";
  ui["segment-seconds"].value = String(settings.segment_seconds || 5);
  ui["transcription-model"].value = settings.transcription_model || (ui["transcription-mode"].value === "local" ? "tiny" : "");
  ui["local-transcription-model"].value = settings.transcription_model === "base" ? "base" : "tiny";
  ui["translation-model"].value = settings.translation_model || "";
  for (const role of ["transcription", "translation"]) {
    ui[`${role}-base-url`].value = settings[`${role}_base_url`] || "";
    ui[`${role}-api-key`].value = "";
    ui[`${role}-api-key`].disabled = false;
    ui[`${role}-api-key`].placeholder = settings[`${role}_has_own_key`] ? "已配置独立密钥；留空即可保留" : "留空使用共用密钥";
    ui[`clear-${role}-api-key`].checked = false;
    ui[`clear-${role}-api-key`].disabled = !settings[`${role}_has_own_key`];
  }
  ui["provider-preset"].value = ui["transcription-mode"].value === "local" ? "local" : ui["transcription-mode"].value === "realtime" && ui["translation-api"].value === "responses" ? "official" : "custom";
  ui.glossary.value = settings.glossary || "";
  ui["settings-status"].textContent = settings.key_from_environment
    ? "共用密钥由启动环境提供；表单中的共用密钥不会覆盖它。切换其他服务时，请为对应角色配置独立密钥。"
    : ui["transcription-mode"].value === "local"
      ? "本机识别无需语音 API 密钥；请确认 GPT 翻译服务的地址、密钥和模型已配置。"
      : settings.key_configured ? "语音识别和翻译服务的密钥已在本机配置。" : "请配置语音识别和翻译服务的 API 密钥后开始真实录制。";
  ui["settings-status"].dataset.level = "info";
  updateSettingsHints();
  updateControls();
}

function openSettings() {
  if (!new URLSearchParams(location.search || "").has("legacy_settings")) {
    const url = globalThis.YakinikuNavigation?.withReturnTo("/static/live-settings.html") || "/static/live-settings.html";
    const popup = window.open(url, "_blank");
    if (!popup) showNotice("直播设置页面被浏览器拦截，请允许此本机页面打开新窗口后重试。", "error");
    else showNotice("已在新标签页打开直播设置，当前录制页面会保留。保存后可返回这里继续。", "success");
    return;
  }
  fillSettings();
  if (!ui["settings-dialog"].open) ui["settings-dialog"].showModal();
}

function openModelDownload() {
  const destination = "/static/live-settings.html#live-model-download";
  const url = globalThis.YakinikuNavigation?.withReturnTo(destination) || destination;
  const popup = window.open(url, "_blank");
  if (!popup) showNotice("模型下载页面被浏览器拦截，请允许此本机页面打开新窗口后重试。", "error");
  else showNotice("已打开模型下载页面。点击「下载模型」才会开始，当前直播页面会保留。", "success");
}

async function saveSettings(event) {
  event.preventDefault();
  if (phase !== "idle") return;
  settingsBusy = true;
  updateControls();
  ui["settings-status"].textContent = "正在保存…";
  try {
    if (!ui["api-base-url"].value.trim() && !ui["translation-base-url"].value.trim()) throw new Error("请填写共用 API 地址或独立翻译 API 地址，避免将密钥发送到错误的平台。");
    settings = await requestJSON("/api/settings", { method: "POST", body: JSON.stringify({
      api_base_url: ui["api-base-url"].value.trim(),
      api_key: ui["api-key"].value.trim(),
      transcription_mode: ui["transcription-mode"].value,
      translation_api: ui["translation-api"].value,
      segment_seconds: Number(ui["segment-seconds"].value),
      transcription_model: ui["transcription-model"].value.trim(),
      translation_model: ui["translation-model"].value.trim(),
      transcription_base_url: ui["transcription-base-url"].value.trim(),
      transcription_api_key: ui["transcription-api-key"].value.trim(),
      translation_base_url: ui["translation-base-url"].value.trim(),
      translation_api_key: ui["translation-api-key"].value.trim(),
      clear_transcription_api_key: ui["clear-transcription-api-key"].checked,
      clear_translation_api_key: ui["clear-translation-api-key"].checked,
      glossary: ui.glossary.value.trim(),
    }) });
    fillSettings();
    const ready = Boolean((settings.configuration_ready ?? settings.key_configured) && settings.transcription_model?.trim() && settings.translation_model?.trim());
    const missingModels = [!settings.transcription_model?.trim() && "语音识别模型", !settings.translation_model?.trim() && "翻译模型"].filter(Boolean);
    const message = ready ? "设置已保存。可以检查 API 配置，再用一小段直播测试实际识别效果。"
      : missingModels.length ? `设置已保存。尚未配置${missingModels.join("和")}，请获取该服务支持的模型或按其文档填写后再录制。`
      : settings.transcription_mode === "local" ? "设置已保存。本机识别已选择，请配置 GPT 翻译服务的 API 地址、密钥和模型后开始录制。"
      : "设置已保存。语音识别或翻译仍缺少 API 地址或密钥，请完成配置后开始录制。";
    ui["settings-status"].textContent = message;
    ui["settings-status"].dataset.level = ready ? "success" : "info";
    showNotice(message, ready ? "success" : "info");
  } catch (error) {
    ui["settings-status"].textContent = error.message;
    ui["settings-status"].dataset.level = "error";
  } finally { settingsBusy = false; updateControls(); }
}

async function testConnection() {
  if (phase !== "idle" || settingsBusy) return;
  if (settingsDirty) {
    ui["settings-status"].textContent = "请先保存当前修改，再检查已保存的 API 配置。";
    return;
  }
  settingsBusy = true;
  updateControls();
  ui["settings-status"].textContent = "正在检查已保存的 API 地址、鉴权与模型列表…";
  try {
    const result = await requestJSON("/api/settings/test", { method: "POST", body: "{}" }, 90000);
    ui["settings-status"].textContent = `语音识别：${result.transcription?.message || "未返回检查结果"}\n文字翻译：${result.translation?.message || "未返回检查结果"}\n此检查不上传音频或调用付费翻译，不代替实际语音测试。`;
    const allPassed = result.transcription?.ok && result.translation?.ok;
    const modelUnlisted = result.transcription?.model_found === false || result.translation?.model_found === false;
    ui["settings-status"].dataset.level = allPassed ? modelUnlisted ? "info" : "success" : "error";
  } catch (error) {
    ui["settings-status"].textContent = error.message;
    ui["settings-status"].dataset.level = "error";
  } finally { settingsBusy = false; updateControls(); }
}

function humanSize(bytes) {
  const number = Number(bytes) || 0;
  if (number >= 1024 ** 3) return `${(number / 1024 ** 3).toFixed(1)} GB`;
  if (number >= 1024 ** 2) return `${(number / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(number / 1024))} KB`;
}

function fileLabel(name) {
  const labels = { "audio.wav": "WAV 音频", "ja.srt": "日语 SRT", "zh-CN.srt": "中文 SRT", "bilingual.srt": "双语 SRT", "bilingual.vtt": "双语 VTT", "transcript.txt": "文稿 TXT", "session.json": "时间轴 JSON" };
  const part = /^audio-(\d+)\.wav$/.exec(name);
  return labels[name] || (part ? `WAV 音频 ${Number(part[1])}` : name);
}

function renderRecordings(recordings) {
  const list = ui["recordings-list"];
  if (!recordings.length) { list.replaceChildren(emptyLibrary.cloneNode(true)); return; }
  const fragment = document.createDocumentFragment();
  for (const session of recordings) {
    const card = document.createElement("article");
    card.className = "recording-item";
    const title = document.createElement("h3");
    title.className = "recording-title";
    const preview = document.createElement("a");
    preview.href = `/static/preview.html?id=${encodeURIComponent(session.id)}`;
    preview.target = "_blank";
    preview.rel = "noopener";
    preview.textContent = session.title || "直播录制";
    preview.title = "打开音频与字幕预览";
    title.append(preview);
    const meta = document.createElement("div");
    meta.className = "recording-meta";
    const date = new Date(session.created_at);
    const dateLabel = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    const statuses = { recording: "录制中", processing: "音频已保存 · 字幕补全中", complete: "已保存", completed: "已保存", partial: "音频已保存 · 字幕部分完成", backfilling: "音频已保存 · 字幕补全中", saved: "已保存", interrupted: "中断后保存", recovered: "已恢复", error: "请检查录制", finalizing: session.audio_saved ? "音频已保存 · 字幕补全中" : "正在保存音频" };
    const pending = livePendingCount(session);
    meta.textContent = [dateLabel, timecode(session.duration_ms), `${session.segment_count ?? session.segments?.length ?? 0} 句`, statuses[session.status] || session.status || "已保存", pending ? `待补全 ${pending} 段` : ""].filter(Boolean).join(" · ");
    const links = document.createElement("div");
    links.className = "recording-links";
    for (const file of session.files || []) {
      let url;
      try { url = new URL(file.url, location.origin); } catch { continue; }
      if (url.origin !== location.origin || !url.pathname.startsWith(`/api/sessions/${encodeURIComponent(session.id)}/files/`)) continue;
      const link = document.createElement("a");
      link.href = url.pathname;
      link.download = file.name;
      link.textContent = fileLabel(file.name);
      link.title = `${fileLabel(file.name)} · ${humanSize(file.size)}`;
      links.append(link);
    }
    card.append(title, meta, links);
    const failed = Math.max(pending, Number(session.failed_translation_count ?? session.translation_failed_count) || 0);
    if (failed > 0 && !["recording", "finalizing", "backfilling", "processing"].includes(session.status) && session.subtitle_status !== "backfilling") {
      const retry = document.createElement("button");
      retry.className = "retry-button";
      retry.type = "button";
      retry.textContent = `补全 ${failed} 段字幕`;
      retry.addEventListener("click", async () => {
        if (phase !== "idle") { showNotice("请在当前录制与字幕补全结束后重试。"); return; }
        retry.disabled = true;
        retry.textContent = "正在补全字幕…";
        try {
          await requestJSON(`/api/sessions/${encodeURIComponent(session.id)}/retry-translations`, { method: "POST", body: "{}" }, 180000);
          await loadRecordings();
          showNotice("字幕补全已完成，文件已更新。请查看是否仍有未完成片段。", "success");
        } catch (error) {
          showNotice(error.message, "error");
          retry.disabled = false;
          retry.textContent = `补全 ${failed} 段字幕`;
        }
      });
      card.append(retry);
    }
    fragment.append(card);
  }
  list.replaceChildren(fragment);
}

function livePendingCount(session) {
  const calculated = (session.segments || []).filter((segment) => {
    if (segment.zh || ["no_speech", "skipped"].includes(segment.asr_status)) return false;
    return (segment.asr_status && segment.asr_status !== "done") || ((segment.ja || segment.ja_raw) && segment.translation_status !== "skipped");
  }).length;
  const tail = session.live_recovery_from_ms != null && Number.isFinite(Number(session.live_recovery_from_ms)) && Number(session.live_recovery_from_ms) < Number(session.duration_ms) ? 1 : 0;
  return Math.max(calculated, Number(session.pending_subtitle_count) || 0) + tail;
}

function loadRecordings() {
  if (recordingsLoadTask) return recordingsLoadTask;
  recordingsLoadTask = requestJSON("/api/sessions").then((result) => {
    const recordings = (Array.isArray(result) ? result : result.sessions || []).filter((session) => session.source_type !== "import");
    renderRecordings(recordings);
    return recordings;
  }).finally(() => { recordingsLoadTask = null; });
  return recordingsLoadTask;
}

async function refreshRecordings() {
  if (refreshingRecordings) return;
  refreshingRecordings = true;
  const button = ui["refresh-recordings"];
  button.disabled = true;
  button.setAttribute?.("aria-busy", "true");
  button.setAttribute?.("aria-label", "正在刷新录制文件");
  button.title = "正在刷新录制文件…";
  showNotice("正在读取录制文件…");
  try {
    const recordings = await loadRecordings();
    showNotice(`录制文件已刷新，共 ${recordings.length} 个直播项目。`, "success");
  } catch (error) { showNotice(error.message, "error"); }
  finally {
    refreshingRecordings = false;
    button.disabled = false;
    button.setAttribute?.("aria-busy", "false");
    button.setAttribute?.("aria-label", "刷新录制文件");
    button.title = "刷新录制文件";
  }
}

function captureError(error) {
  if (error.name === "NotAllowedError") return "没有开始共享。请在 Edge 中重新选择直播标签页，并勾选「分享标签页音频」。";
  if (error.name === "NotFoundError") return "未找到可采集的标签页音频。请在 Edge 中先打开并播放直播。";
  if (error.name === "NotReadableError") return "Edge 暂时无法采集音频。请检查系统的屏幕录制权限，然后重新选择直播标签页。";
  return error.message || "无法开始录制，请检查浏览器和本机服务。";
}

function sendPCM(current, prefix, buffer) {
  if (!buffer.byteLength) return;
  const packet = new Uint8Array(buffer.byteLength + 1);
  packet[0] = prefix;
  packet.set(new Uint8Array(buffer), 1);
  current.socket.send(packet);
}

function onWorkletMessage(current, message) {
  if (capture !== current) return;
  if (message.type === "flushed") { current.flushResolve?.(); return; }
  if (!(message.recording instanceof ArrayBuffer)) return;
  if (current.socket?.readyState !== WebSocket.OPEN) return;
  if (current.socket.bufferedAmount > BUFFER_LIMIT) {
    if (!current.overloaded) {
      current.overloaded = true;
      showNotice("本机连接积压，已停止继续采集并开始保存已接收的录音。请检查电脑负载后再录制。", "error");
      stopCapture("overload");
    }
    return;
  }
  sendPCM(current, 0, message.recording);
  sendPCM(current, 1, message.transcription);
  current.frames += message.recording.byteLength / 4;
  setTimer(current.frames * 1000 / current.sampleRate);
  const rms = Number(message.rms) || 0;
  showAudioLevel(rms);
  if (rms > .001) current.lastAudible = performance.now();
}

function handleProviderMessage(current, message) {
  if (capture !== current) return;
  if (message.session_id && current.session?.id && String(message.session_id) !== String(current.session.id)) return;
  switch (message.type) {
    case "ready":
      current.mode = message.mode;
      current.recognitionMode = message.recognition_mode || (liveSettings?.backend === "mlx" ? "mlx" : "local");
      current.segmentSeconds = message.segment_seconds || liveSettings?.segment_seconds || 5;
      current.readyResolve?.(message);
      break;
    case "partial": {
      // MLX emits confirmed segments only; legacy streaming partials remain a
      // separate display path for recordings made with the old cloud backend.
      if (["local", "mlx", "tiny"].includes(current.recognitionMode)) break;
      const hasTimestamp = message.start_ms !== null && message.start_ms !== undefined && Number.isFinite(Number(message.start_ms));
      const start = hasTimestamp ? Number(message.start_ms) : null;
      if (!hasTimestamp || start >= latestStart) {
        if (hasTimestamp) latestStart = start;
        liveSegmentId = String(message.id);
        setCaptions(message.ja || message.text || "", "正在听取与翻译…");
      }
      break;
    }
    case "transcript":
    case "correction":
    case "translation":
    case "segment":
    case "segment_status":
      updateSegment(message.segment);
      break;
    case "warning":
      current.lastProviderWarning = message.message || "";
      showNotice(message.message || "识别服务暂时异常，录音仍会保存在本机。", "warning");
      if (message.recording_only && phase === "recording") setStatus("仅录音 · 识别暂时不可用", "仅录音", true);
      break;
    case "connection":
      if (message.status === "connected") {
        if (phase === "recording") setStatus("录制中 · 本机日语识别", "识别已连接", true);
      } else if (["failed", "reconnecting", "recording_only"].includes(message.status) || message.mode === "recording_only") {
        setStatus(phase === "recording" ? "仅录音 · 识别暂时不可用" : "正在连接", message.status === "reconnecting" ? "识别重连中" : "识别未连接", phase === "recording");
      }
      break;
    case "progress":
      if (Number.isFinite(message.duration_ms)) setTimer(message.duration_ms);
      updatePendingState(message);
      break;
    case "audio_ack":
      current.audioAckResolve?.(Number(message.received_frames ?? message.frames));
      break;
    case "audio_saved":
      current.audioSavedReceived = true;
      current.audioSavedSession = message.session;
      current.audioSavedResolve?.({ session: message.session, pending_count: message.pending_count });
      updatePendingState(message);
      break;
    case "saved":
      current.savedReceived = true;
      current.savedSession = message.session;
      current.audioSavedResolve?.({ session: message.session, pending_count: 0 });
      current.savedResolve?.({ session: message.session });
      break;
  }
}

function updatePendingState(message) {
  const pending = Math.max(0, Number(message.pending_count) || 0);
  const asr = Number(message.asr_queue) || 0;
  const translation = Number(message.translation_queue) || 0;
  const age = Math.max(0, Number(message.oldest_age_ms) || 0);
  ui["live-pending-count"].textContent = pending ? `待补全 ${pending} 段` : "暂无待补全";
  ui["live-pending-count"].title = `识别队列 ${asr} · 翻译队列 ${translation} · 最早等待 ${(age / 1000).toFixed(1)} 秒`;
  if (phase === "backfilling") setStatus(`音频已保存 · 字幕补全中${pending ? `（${pending} 段）` : ""}`, "后台补全");
}

async function openSocket(current) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const token = encodeURIComponent(current.session.token || "");
  const socket = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(current.session.id)}?token=${token}`);
  current.socket = socket;
  current.savedPromise = new Promise((resolve) => { current.savedResolve = resolve; });
  current.audioSavedPromise = new Promise((resolve) => { current.audioSavedResolve = resolve; });
  const ready = new Promise((resolve, reject) => {
    current.readyResolve = resolve;
    current.readyReject = reject;
  });
  const readyTimeout = setTimeout(() => current.readyReject(new Error("本机直播连接超时，请检查模型状态与本机服务。")), 45000);
  socket.addEventListener("message", (event) => {
    try { handleProviderMessage(current, JSON.parse(event.data)); }
    catch { showNotice("收到无法解析的服务消息，录音会继续。", "warning"); }
  });
  socket.addEventListener("error", () => {
    if (phase === "connecting") current.readyReject(new Error("无法建立本机音频连接，请检查启动终端中的错误信息。"));
  });
  socket.addEventListener("close", () => {
    current.readyReject(new Error("本机音频连接已关闭。"));
    if (current.savedReceived || capture !== current) return;
    current.savedResolve({ error: "本机音频连接意外中断。" });
    current.audioSavedResolve({ error: "本机音频连接意外中断，尚未收到音频保存确认。" });
    if (phase === "recording") recoverFromDisconnect(current);
  });
  try { return await ready; }
  finally { clearTimeout(readyTimeout); }
}

async function closeAudio(current) {
  clearInterval(current.silenceTimer);
  try { current.source?.disconnect(); } catch { /* Node may already be disconnected. */ }
  try { current.node?.disconnect(); } catch { /* Node may already be disconnected. */ }
  current.stream?.getTracks().forEach((track) => track.stop());
  if (current.context && current.context.state !== "closed") {
    try { await current.context.close(); } catch { /* Capture is already stopped. */ }
  }
  showAudioLevel(0);
}

function scheduleLibraryRecovery() {
  loadRecordings().catch(() => {});
  setTimeout(() => loadRecordings().catch(() => {}), 1800);
  setTimeout(() => loadRecordings().catch(() => {}), 5500);
}

async function recoverFromDisconnect(current) {
  if (capture !== current) return;
  phase = "finalizing";
  updateControls();
  await closeAudio(current);
  if (capture !== current) return;
  capture = null;
  phase = "idle";
  setStatus("连接中断 · 已停止采集", "已断开");
  updateControls();
  showNotice("本机音频连接中断，已停止采集。服务会尝试保存已接收的音频；请刷新「录制文件」检查恢复结果。若服务已退出，请重新启动后查看。", "error");
  scheduleLibraryRecovery();
}

async function startCapture() {
  if (phase !== "idle") return;
  if (liveSettings?.locked) { showNotice(liveBusyMessage()); return; }
  if (!liveSettings?.translation_ready) {
    showNotice(liveSettings?.translation_backend === "local"
      ? "请先在「直播设置」中下载并加载所选本地翻译模型；本地翻译不需要 API 密钥。"
      : "请先在「直播设置」中选择直播翻译服务商和文字模型；文件识别与独立校对无需配置即可直播。");
    openSettings();
    return;
  }
  if (modelState() !== "ready") {
    showNotice("本机识别模型尚未就绪，请先点击「准备本机模型」。");
    return;
  }
  if (!navigator.mediaDevices?.getDisplayMedia || !window.AudioWorkletNode) {
    showNotice("当前浏览器不支持标签页音频采集。请用最新版 Edge 打开本机地址，然后选择直播标签页。", "error");
    return;
  }
  const current = { stream: null, context: null, source: null, node: null, socket: null, session: null, frames: 0, savedReceived: false, cancelled: false, mode: "live" };
  capture = current;
  phase = "connecting";
  updateControls();
  showNotice("");
  try {
    // Keep this call in the click gesture, before any network request or await.
    const streamPromise = navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false },
      selfBrowserSurface: "exclude",
      systemAudio: "exclude",
      surfaceSwitching: "exclude",
    });
    current.stream = await streamPromise;
    if (!current.stream.getAudioTracks().length) throw new Error("没有收到标签页音频。请重新选择 Edge 中播放直播的「标签页」，并勾选「分享标签页音频」。");
    for (const track of current.stream.getTracks()) track.addEventListener("ended", () => {
      current.cancelled = true;
      if (capture === current && phase === "recording") {
        showNotice("标签页共享已结束，正在保存本次录制。");
        stopCapture("source-ended");
      }
    });
    current.context = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
    await current.context.resume();
    await current.context.audioWorklet.addModule("/static/audio-worklet.js");
    current.sampleRate = current.context.sampleRate;
    if (current.cancelled) throw new Error("标签页共享已结束，请重新开始录制。");
    current.session = await requestJSON("/api/sessions", { method: "POST", body: JSON.stringify({ title: ui["session-title"].value.trim(), sample_rate: current.sampleRate, channels: 2 }) });
    const ready = await openSocket(current);
    if (current.cancelled) throw new Error("标签页共享已结束，请重新开始录制。");
    resetTranscript();
    captions.session_id = current.session.id;
    captions.segment_id = "";
    captions.sequence = -1;
    publishCaptions();
    waveformLevels = Array(65).fill(0);
    setTimer(0);
    current.source = current.context.createMediaStreamSource(new MediaStream(current.stream.getAudioTracks()));
    current.node = new AudioWorkletNode(current.context, "yakiniku-capture", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 2, channelCountMode: "explicit" });
    current.node.port.onmessage = (event) => onWorkletMessage(current, event.data);
    current.node.addEventListener("processorerror", () => {
      if (capture === current && phase === "recording") {
        showNotice("浏览器音频处理发生错误，正在停止采集并保存已有录音。", "error");
        stopCapture("audio-error");
      }
    });
    current.lastAudible = performance.now();
    current.source.connect(current.node);
    current.node.connect(current.context.destination);
    phase = "recording";
    ui["source-label"].textContent = current.stream.getAudioTracks()[0].label || "Edge 直播标签页音频";
    ui["latency-label"].textContent = `本机分段识别 → 一次中文翻译 · 最长 ${current.segmentSeconds} 秒 / 段`;
    setCaptions("", "正在听取直播…");
    setStatus(ready.mode === "recording_only" ? "仅录音 · 识别未连接" : "录制中 · 本机日语识别", ready.mode === "recording_only" ? "仅录音" : "识别已连接", true);
    if (ready.mode === "recording_only") {
      const reason = ready.session?.warnings?.at(-1) || current.lastProviderWarning || "语音转写连接失败。";
      showNotice(`${reason} 当前仅保存音频，没有生成字幕。`, "warning");
    }
    current.silenceTimer = setInterval(() => {
      if (capture !== current || phase !== "recording") return;
      if (!current.silenceWarned && performance.now() - current.lastAudible >= 8000) {
        current.silenceWarned = true;
        showNotice("目前没有检测到声音，录制仍在继续。请确认直播正在播放，并已分享所选标签页的音频。");
      }
    }, 1000);
    updateControls();
  } catch (error) {
    if (current.socket?.readyState === WebSocket.OPEN) {
      try { current.socket.send(JSON.stringify({ type: "stop" })); } catch { /* Service will recover a disconnected session. */ }
    }
    await closeAudio(current);
    if (current.socket && current.socket.readyState < WebSocket.CLOSING) current.socket.close();
    if (capture === current) capture = null;
    phase = "idle";
    setStatus("等待开始", "未连接");
    updateControls();
    showNotice(captureError(error), "error");
    if (current.session) scheduleLibraryRecovery();
  }
}

async function flushAudio(current) {
  if (!current.node) return;
  if (current.context.state === "suspended") await current.context.resume();
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { current.flushResolve = null; reject(new Error("音频尾部缓冲未及时响应，末尾可能缺少不足一秒的声音。")); }, 3000);
    current.flushResolve = () => { clearTimeout(timeout); current.flushResolve = null; resolve(); };
    current.node.port.postMessage({ type: "flush" });
  });
}

async function confirmReceivedAudio(current) {
  if (current.socket?.readyState !== WebSocket.OPEN) throw new Error("音频连接已中断，无法确认尾包。");
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      current.audioAckResolve = null;
      reject(new Error("未及时收到音频尾包确认，请检查已保存录音的末尾。"));
    }, 5000);
    current.audioAckResolve = (frames) => {
      if (!Number.isFinite(frames) || frames < current.frames) return;
      clearTimeout(timeout);
      current.audioAckResolve = null;
      resolve();
    };
    // WebSocket preserves message order: all binary chunks precede this marker.
    current.socket.send(JSON.stringify({ type: "flush", frames: current.frames }));
  });
}

function releaseCapture(current) {
  clearInterval(completionPoll);
  completionPoll = null;
  if (current.socket && current.socket.readyState < WebSocket.CLOSING) current.socket.close();
  if (capture === current) capture = null;
  phase = "idle";
  updateControls();
  loadLiveReadiness().catch(() => {});
}

async function finishSubtitleCompletion(current) {
  try {
    const result = await current.savedPromise;
    if (capture !== current) return;
    if (result.error) throw new Error(result.error);
    if (!result.session) throw new Error("服务未返回字幕整理结果。");
    setTimer(result.session.duration_ms);
    const incomplete = ["partial", "interrupted", "error"].includes(result.session.status) || livePendingCount(result.session) > 0 || Number(result.session.failed_translation_count) > 0;
    setStatus(incomplete ? "音频已保存 · 字幕部分完成" : "音频与字幕已保存", "录制结束");
    ui["live-pending-count"].textContent = incomplete ? "仍有未完成字幕，可在项目中重试" : "字幕已完成";
    await loadRecordings().catch(() => {});
    showNotice(incomplete ? "音频已保存，部分字幕未完成。可打开项目预览并重试失败片段，成功字幕不会重复处理。" : "音频与字幕已保存。可在录制文件中预览，并分别下载 WAV、日语、中文和双语字幕。", incomplete ? "warning" : "success");
  } catch (error) {
    if (capture !== current) return;
    setStatus(current.audioSavedReceived ? "音频已保存 · 字幕补全中断" : "请检查保存结果", "已停止采集");
    showNotice(`${current.audioSavedReceived ? "音频已收到保存确认。" : ""}${error.message} 请在录制文件中查看项目状态并重试未完成字幕。`, "warning");
    scheduleLibraryRecovery();
  } finally {
    if (capture === current) releaseCapture(current);
  }
}

async function stopCapture(reason = "user") {
  const current = capture;
  if (!current || phase !== "recording") return;
  phase = "finalizing";
  updateControls();
  setStatus("停止采集 · 正在保存音频", "确认音频尾包");
  let flushWarning = "";
  try { await flushAudio(current); } catch (error) { flushWarning = error.message; }
  await closeAudio(current);
  try {
    if (current.socket?.readyState !== WebSocket.OPEN) throw new Error("保存时连接已中断。");
    try { await confirmReceivedAudio(current); } catch (error) { flushWarning = [flushWarning, error.message].filter(Boolean).join(" "); }
    current.socket.send(JSON.stringify({ type: "stop" }));
    let timeout;
    const result = await Promise.race([
      current.audioSavedPromise,
      current.savedPromise,
      new Promise((resolve) => { timeout = setTimeout(() => resolve({ error: "尚未收到音频保存确认。服务可能仍在处理，请检查录制文件。" }), 30000); }),
    ]);
    clearTimeout(timeout);
    if (result.error) throw new Error(result.error);
    if (!result.session) throw new Error("服务未返回音频保存结果，请刷新录制文件检查。");
    current.audioSavedReceived = true;
    setTimer(result.session.duration_ms);
    await loadRecordings().catch(() => {});
    if (current.savedReceived) {
      await finishSubtitleCompletion(current);
      return;
    }
    phase = "backfilling";
    updateControls();
    setStatus("音频已保存 · 字幕补全中", "后台补全");
    const reasonNote = reason === "overload" ? "采集因本机连接积压而停止。" : reason === "audio-error" ? "浏览器音频处理出错后已停止采集。" : "";
    showNotice(`${reasonNote}音频已保存，可以立即下载。未完成字幕正在后台补全，请保持本机服务运行。${flushWarning ? ` ${flushWarning}` : ""}`, flushWarning || reasonNote ? "warning" : "success");
    completionPoll = setInterval(() => loadRecordings().catch(() => {}), 5000);
    finishSubtitleCompletion(current);
  } catch (error) {
    setStatus("请检查保存结果", "已停止采集");
    showNotice(`${error.message} 已停止采集，请刷新「录制文件」检查服务恢复结果。`, "error");
    scheduleLibraryRecovery();
    releaseCapture(current);
  }
}

const demoLines = [
  { ja: "皆さん、こんばんは。今日も来てくれてありがとうございます。", zh: "大家晚上好，谢谢你们今天也来听直播。" },
  { ja: "今日は最近あった、ちょっと嬉しいことを話そうと思います。", zh: "今天想和大家聊聊最近遇到的一件小开心。" },
  { ja: "それでは、ゆっくり楽しんでいってくださいね。", zh: "那么，就请放轻松，一起享受接下来的时间吧。" },
];

function endDemo() {
  demoTimers.forEach(clearTimeout);
  demoTimers = [];
  demoActive = false;
  phase = "idle";
  ui["demo-button"].replaceChildren(...Array.from(initialDemoLabel.childNodes, (node) => node.cloneNode(true)));
  ui["source-label"].textContent = "尚未连接直播标签页";
  ui["latency-label"].textContent = "日语原文 · 中文翻译";
  resetTranscript();
  setTimer(0);
  waveformLevels = Array(65).fill(0);
  showAudioLevel(0);
  setCaptions("", "下一句话，即将被看见。");
  setStatus("等待开始", "未连接");
  showNotice("");
  updateControls();
}

function toggleDemo() {
  if (demoActive) { endDemo(); return; }
  if (phase !== "idle") return;
  phase = "demo";
  demoActive = true;
  resetTranscript();
  setTimer(0);
  ui["demo-button"].textContent = "结束字幕演示";
  ui["source-label"].textContent = "演示内容 · 未采集音频";
  ui["latency-label"].textContent = "模拟字幕，不调用 API，不保存文件";
  setStatus("字幕演示 · 未录音", "演示模式");
  showNotice("当前为字幕演示：使用预设日中对照文本，不会采集直播、调用 API 或生成录制文件。");
  setCaptions("", "字幕演示即将开始…");
  updateControls();
  demoLines.forEach((line, index) => {
    const start = index * 3200 + 400;
    demoTimers.push(setTimeout(() => {
      if (!demoActive) return;
      setCaptions(line.ja, "正在翻译…");
      updateSegment({ id: `demo-${index}`, start_ms: index * 3200, end_ms: index * 3200 + 3000, ja: line.ja, zh: "", translation_status: "pending" });
    }, start));
    demoTimers.push(setTimeout(() => {
      if (!demoActive) return;
      updateSegment({ id: `demo-${index}`, start_ms: index * 3200, end_ms: index * 3200 + 3000, ja: line.ja, zh: line.zh, translation_status: "completed" });
    }, start + 650));
  });
}

ui["open-settings"].addEventListener("click", openSettings);
ui["close-settings"].addEventListener("click", () => ui["settings-dialog"].close());
ui["settings-dialog"].addEventListener("close", invalidateModelPickers);
ui["settings-form"].addEventListener("submit", saveSettings);
ui["settings-form"].addEventListener("input", markSettingsDirty);
ui["settings-form"].addEventListener("change", markSettingsDirty);
ui["provider-preset"].addEventListener("change", (event) => { event.stopPropagation(); applyProviderPreset(); });
ui["test-connection"].addEventListener("click", testConnection);
ui["transcription-mode"].addEventListener("change", changeTranscriptionMode);
ui["local-transcription-model"].addEventListener("change", () => { ui["transcription-model"].value = ui["local-transcription-model"].value; markSettingsDirty(); });
for (const role of Object.keys(modelPickers)) {
  ui[`fetch-${role}-models`].addEventListener("click", () => fetchModels(role));
  ui[`${role}-model-list`].addEventListener("change", (event) => chooseModel(role, event));
  ui[`${role}-model-list`].addEventListener("input", (event) => event.stopPropagation());
  ui[`${role}-model-search`].addEventListener("input", (event) => { event.stopPropagation(); renderModelPicker(role); });
  ui[`${role}-model-search`].addEventListener("change", (event) => event.stopPropagation());
  ui[`${role}-model`].addEventListener("input", () => renderModelPicker(role));
}
for (const role of ["transcription", "translation"]) {
  ui[`clear-${role}-api-key`].addEventListener("change", () => {
    if (ui[`clear-${role}-api-key`].checked) ui[`${role}-api-key`].value = "";
    ui[`${role}-api-key`].disabled = ui[`clear-${role}-api-key`].checked;
  });
}
ui["start-button"].addEventListener("click", startCapture);
ui["prepare-live-model"].addEventListener("click", prepareLiveModel);
ui["open-model-download"].addEventListener("click", openModelDownload);
ui["stop-button"].addEventListener("click", () => stopCapture());
ui["demo-button"].addEventListener("click", toggleDemo);
ui["refresh-recordings"].addEventListener("click", refreshRecordings);
ui["overlay-button"].addEventListener("click", () => {
  if (!captionChannel) { showNotice("当前浏览器不支持独立字幕窗口，请在 Edge 中使用此功能。", "error"); return; }
  const url = globalThis.YakinikuNavigation?.withReturnTo("/static/overlay.html") || "/static/overlay.html";
  const popup = window.open(url, "yakiniku-captions", "popup,width=980,height=330");
  if (!popup) showNotice("字幕窗口被浏览器拦截，请允许此本机页面弹出窗口后再试。");
  else showNotice("已打开独立字幕窗口。可将窗口放在直播旁，或在 OBS 中使用窗口捕获。字幕内容与工作台同步。");
});
ui["fullscreen-button"].addEventListener("click", async () => {
  if (fullscreenPending) return;
  fullscreenPending = true;
  ui["fullscreen-button"].disabled = true;
  ui["fullscreen-button"].setAttribute?.("aria-busy", "true");
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.querySelector(".monitor-card").requestFullscreen();
  } catch { showNotice("当前浏览器未允许全屏显示，可以使用独立字幕窗口。"); }
  finally {
    fullscreenPending = false;
    ui["fullscreen-button"].disabled = false;
    ui["fullscreen-button"].setAttribute?.("aria-busy", "false");
    updateFullscreenButton();
  }
});
function updateFullscreenButton() {
  const fullscreen = Boolean(document.fullscreenElement);
  const label = fullscreen ? "退出字幕全屏" : "全屏显示字幕";
  ui["fullscreen-button"].title = label;
  ui["fullscreen-button"].setAttribute?.("aria-label", label);
  ui["fullscreen-button"].setAttribute?.("aria-pressed", String(fullscreen));
}
function updateNavigationSection() {
  const current = location.hash === "#recordings" ? "#recordings" : "#workspace";
  for (const link of document.querySelectorAll?.('.navigation a[href="#workspace"], .navigation a[href="#recordings"]') || []) {
    const selected = link.getAttribute("href") === current;
    link.classList.toggle("active", selected);
    if (selected) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}
document.addEventListener?.("fullscreenchange", updateFullscreenButton);
window.addEventListener("hashchange", updateNavigationSection);
updateFullscreenButton();
updateNavigationSection();
window.addEventListener("beforeunload", (event) => {
  if (["connecting", "recording", "finalizing"].includes(phase)) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("pagehide", () => {
  const current = capture;
  if (current && phase === "recording") {
    // Best effort only: navigation may end the process before the worklet replies.
    // Already received audio is recovered by the server on socket disconnect.
    flushAudio(current).then(() => {
      if (current.socket?.readyState === WebSocket.OPEN) current.socket.send(JSON.stringify({ type: "stop" }));
    }).catch(() => {});
  }
  captionChannel?.postMessage({ type: "caption", session_id: captions.session_id, revision: ++captionRevision, ja: "", zh: "工作台已关闭", latest_ja: "", status: "已断开", recording: false });
});
window.addEventListener("focus", () => { if (phase === "idle") loadLiveReadiness().catch(() => {}); });
window.addEventListener("resize", drawWaveform);
if ("ResizeObserver" in window) new ResizeObserver(drawWaveform).observe(ui.waveform);
updateControls();
drawWaveform();
Promise.allSettled([
  loadLiveReadiness().then(() => {
    if (!liveSettings.translation_ready) showNotice("先在「直播设置」中配置直播翻译，再准备本机识别模型。也可体验字幕演示。");
  }).catch((error) => {
    ui["live-prepare-help"].textContent = error.message;
    showNotice(error.message, "error");
  }),
  requestJSON("/api/settings").then((result) => {
    settings = result;
    fillSettings();
    updateControls();
  }).catch((error) => showNotice(error.message, "error")),
  loadRecordings().catch((error) => showNotice(error.message, "error")),
]);
