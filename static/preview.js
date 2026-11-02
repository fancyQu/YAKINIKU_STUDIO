import { IMPORT_STAGES, projectBusy, pendingSubtitleCount, segmentPendingText, clockTime, normalizedSegments, cueAt, scrollTimelineToCue, audioParts, partForTime, projectFileURL, jsonRequest } from "./project-utils.js?v=ui-navigation-1";
const $ = (id) => document.getElementById(id);
const identifier = new URLSearchParams(location.search).get("id") || "";
const audio = $("preview-audio");
let project = null;
let segments = [];
let parts = [];
let currentPart = 0;
let currentCue = -1;
let language = "bilingual";
let rows = [];
let signature = "";
let pollTimer = null;
let seekToken = 0;
let polling = false;
let projectLoadTask = null;
let refinement = null;
let refining = false;
let checkingRefinement = false;
let retrying = false;
function notice(message, error = false, level = "info") { $("preview-notice").hidden = !message; $("preview-notice").textContent = message; $("preview-notice").dataset.level = error ? "error" : level; }
function renderRefinement() {
  const visible = project && project.source_type !== "import";
  $("preview-refinement").hidden = !visible;
  if (!visible) return;
  const state = refinement || {};
  const failed = ["failed", "interrupted"].includes(state.stage) || ["failed", "interrupted"].includes(state.project_status);
  const completed = state.stage === "completed" && state.project_status === "completed";
  const resumable = !!state.project_id && failed;
  const canStart = !!state.eligible && !!state.ready && !state.active && !state.busy && !completed && !projectBusy(project);
  const consent = $("refinement-consent");
  $("refinement-consent-label").hidden = !canStart;
  $("preview-refine").hidden = !!state.active || completed;
  $("preview-refine").textContent = refining ? "正在提交…" : resumable ? "继续精准校验" : "开始精准校验";
  $("preview-refine").disabled = refining || !canStart || !consent.checked;
  $("preview-refine").setAttribute("aria-busy", String(refining));
  $("refinement-refresh").disabled = checkingRefinement || refining;
  $("refinement-refresh").setAttribute("aria-busy", String(checkingRefinement));
  $("refinement-config-links").hidden = !!state.ready || !!state.active || completed;
  const fallback = !refinement ? "正在检查精准校验配置…" : state.busy ? "当前有录制或处理任务，请等待完成。页面会自动重新检查。" : failed ? "精准校验尚未完成，可以继续处理未完成的步骤。" : canStart ? "配置已就绪。确认后开始，将生成独立精修项目。" : !state.eligible ? "当前录音尚未就绪，请在保存完成后开始精准校验。" : "请先完成 MiniMax 识别与 GPT 校对、翻译配置。";
  $("refinement-status").textContent = state.active ? `${state.progress_label || IMPORT_STAGES[state.stage] || "精准校验中"} · ${Math.round(Number(state.progress) || 0)}%` : completed ? "精准校验完成，三种字幕已保存。点击查看精修项目，可试听并切换日语、中文或双语。" : state.message || fallback;
  $("refinement-status").dataset.level = failed || state.error ? "error" : "info";
  $("refinement-progress-wrap").hidden = !state.active;
  $("refinement-progress").value = Math.max(0, Math.min(100, Number(state.progress) || 0));
  const link = $("refinement-project-link");
  link.hidden = !/^[a-f0-9]{32}$/.test(state.project_id || "");
  if (!link.hidden) { link.href = `/static/preview.html?id=${encodeURIComponent(state.project_id)}`; link.textContent = completed ? "预览精修结果" : "查看精修项目"; }
  $("refinement-models").textContent = (state.chain || []).map((step) => typeof step === "string" ? step : [step.label || step.role, step.provider || step.provider_name, step.model].filter(Boolean).join(" · ")).join("　→　");
}
async function loadRefinement() {
  if (!project || project.source_type === "import") { refinement = null; renderRefinement(); return; }
  try { refinement = await jsonRequest(`/api/sessions/${encodeURIComponent(identifier)}/refinement`); }
  catch (error) { refinement = { ready: false, error: true, message: `精准校验配置暂时无法读取：${error.message}` }; }
  renderRefinement();
}
function currentMilliseconds() { return (parts[currentPart]?.start_ms || 0) + (Number(audio.currentTime) || 0) * 1000; }
function updateCaption(force = false) {
  const index = cueAt(segments, currentMilliseconds());
  if (!force && index === currentCue) return;
  if (rows[currentCue]) { rows[currentCue].classList.remove("playing"); rows[currentCue].removeAttribute("aria-current"); }
  currentCue = index;
  const segment = segments[index];
  $("preview-zh").hidden = language === "ja";
  $("preview-ja").hidden = language === "zh";
  $("preview-ja").textContent = segment?.ja || segment?.ja_raw || (language === "ja" ? segment ? segmentPendingText(segment) : "当前时间没有日语字幕" : "");
  $("preview-zh").textContent = segment ? segment.zh || segmentPendingText(segment) : "当前时间没有字幕";
  $("preview-caption-time").textContent = segment ? `${clockTime(segment.start_ms)} — ${clockTime(segment.end_ms)}` : clockTime(currentMilliseconds());
  if (rows[index]) {
    rows[index].classList.add("playing"); rows[index].setAttribute("aria-current", "true");
    if ($("follow-playback").checked && !audio.paused) scrollTimelineToCue($("preview-timeline"), rows[index]);
  }
}
function setPart(index, offsetSeconds = 0, play = false) {
  if (!parts[index]) return;
  const token = ++seekToken;
  currentPart = index;
  $("audio-part").value = String(index);
  const url = projectFileURL(identifier, parts[index].name);
  const apply = () => {
    if (token !== seekToken) return;
    audio.currentTime = Math.max(0, offsetSeconds);
    updateCaption(true);
    if (play) audio.play().catch(() => notice("请点击音频播放器的播放按钮继续试听。"));
  };
  if (audio.getAttribute("src") === url && audio.readyState >= 1) { apply(); return; }
  audio.src = url;
  audio.addEventListener("loadedmetadata", apply, { once: true });
  audio.load();
}
function seekTo(milliseconds) {
  const index = partForTime(parts, milliseconds);
  if (index < 0) { notice("这个时间点没有可预览的音频，请检查音频文件是否完整。", true); return; }
  setPart(index, (milliseconds - parts[index].start_ms) / 1000, true);
}
function renderTimeline() {
  const fragment = document.createDocumentFragment(); rows = [];
  for (const segment of segments) {
    const row = document.createElement("button"); row.type = "button"; row.className = "preview-cue"; row.setAttribute("aria-label", `${clockTime(segment.start_ms)}，跳到这一句`);
    const time = document.createElement("span"); time.className = "preview-cue-time"; time.textContent = clockTime(segment.start_ms);
    const text = document.createElement("span"); text.className = "preview-cue-text";
    const zh = document.createElement("span"); zh.className = "preview-cue-zh"; zh.textContent = segment.zh || segmentPendingText(segment);
    const ja = document.createElement("span"); ja.className = "preview-cue-ja"; ja.textContent = segment.ja || segment.ja_raw || segmentPendingText(segment);
    text.append(zh, ja); row.append(time, text);
    row.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      // Native focus can reveal the button by scrolling an outer ancestor.
      event.preventDefault(); row.focus({ preventScroll: true });
    });
    row.addEventListener("click", () => { row.focus({ preventScroll: true }); seekTo(segment.start_ms); });
    rows.push(row); fragment.append(row);
  }
  if (!segments.length) { const empty = document.createElement("p"); empty.className = "project-empty"; empty.textContent = projectBusy(project) ? "正在生成字幕，可稍后回来查看。" : "项目还没有字幕。"; fragment.append(empty); }
  $("preview-timeline").replaceChildren(fragment); currentCue = -1; updateCaption(true);
}
function renderDownloads() {
  const fragment = document.createDocumentFragment();
  const fileSet = new Set((project.files || []).map((file) => file.name));
  const labels = [["ja.srt", "日语 SRT", "日本語"], ["zh-CN.srt", "中文 SRT", "简体中文"], ["bilingual.srt", "双语 SRT", "中文 + 日语"], ...parts.map((part, index) => [part.name, parts.length > 1 ? `WAV 音频 ${index + 1}` : "WAV 音频", "音频文件"]), ["transcript.txt", "完整文稿 TXT", "原文与译文"]];
  const source = (project.files || []).find((file) => /^source\.[a-z0-9]+$/i.test(file.name));
  if (source) labels.push([source.name, "原始音频", project.original_filename || "导入文件"]);
  for (const [name, label, detail] of labels) {
    if (!fileSet.has(name)) continue;
    const link = document.createElement("a"); link.href = projectFileURL(identifier, name); link.download = name; link.className = "preview-download";
    const title = document.createElement("strong"); title.textContent = label; const note = document.createElement("span"); note.textContent = detail;
    const arrow = document.createElement("span"); arrow.className = "download-arrow"; arrow.textContent = "↓"; link.append(title, note, arrow); fragment.append(link);
  }
  $("preview-downloads").replaceChildren(fragment);
}
function loadProject() {
  if (projectLoadTask) return projectLoadTask;
  projectLoadTask = readProject().finally(() => { projectLoadTask = null; });
  return projectLoadTask;
}
async function readProject() {
  if (polling) return;
  polling = true; clearTimeout(pollTimer);
  try {
    project = await jsonRequest(`/api/sessions/${encodeURIComponent(identifier)}`);
    globalThis.YakinikuNavigation?.setFallback(project.source_type === "import" ? "/static/import.html" : "/#recordings");
    document.title = `${project.title || "项目预览"} · 烤肉台`;
    $("preview-title").textContent = project.title || project.original_filename || "音频项目";
    const isImport = project.source_type === "import";
    const isRefinement = isImport && /^[a-f0-9]{32}$/.test(project.refinement_source_id || "") && project.refinement_source_id !== identifier;
    $("preview-source-note").hidden = !isRefinement;
    if (isRefinement) $("preview-source-link").href = `/static/preview.html?id=${encodeURIComponent(project.refinement_source_id)}`;
    else $("preview-source-link").removeAttribute("href");
    const stage = IMPORT_STAGES[project.import_stage] || ({ completed: "已保存", partial: "音频已保存 · 字幕部分完成", interrupted: "中断后保存", recording: "录制中", processing: "音频已保存 · 字幕补全中", backfilling: "音频已保存 · 字幕补全中", finalizing: project.audio_saved ? "音频已保存 · 字幕补全中" : "正在保存音频" })[project.status] || project.status || "已保存";
    $("preview-meta").textContent = `${clockTime(project.duration_ms)} · ${project.original_filename || (project.source_type === "import" ? "导入音频" : "直播录制")} · ${stage}`;
    const pending = pendingSubtitleCount(project);
    $("preview-state").textContent = projectBusy(project) ? isImport ? `${project.import_progress_label || stage} · ${Math.round(Number(project.import_progress) || 0)}%` : `${stage}${pending ? ` · 待补全 ${pending} 段` : ""}` : "音频 · 同步字幕";
    const nextParts = audioParts(project);
    if (JSON.stringify(nextParts) !== JSON.stringify(parts)) {
      const oldName = parts[currentPart]?.name;
      parts = nextParts;
      const options = parts.map((part, index) => { const option = document.createElement("option"); option.value = String(index); option.textContent = `第 ${index + 1} 卷 · ${clockTime(part.start_ms)} — ${clockTime(part.end_ms)}`; return option; });
      $("audio-part").replaceChildren(...options); $("audio-part-label").hidden = parts.length <= 1;
      if (parts.length && !oldName) setPart(0);
      else if (oldName) { const next = parts.findIndex((part) => part.name === oldName); if (next >= 0) { currentPart = next; $("audio-part").value = String(next); } }
    }
    audio.hidden = !parts.length; $("audio-unavailable").hidden = !!parts.length;
    segments = normalizedSegments(project.segments || []);
    $("preview-count").textContent = `${segments.length} 句`;
    const nextSignature = JSON.stringify(segments);
    if (nextSignature !== signature) { signature = nextSignature; renderTimeline(); }
    renderDownloads();
    const failed = isImport ? segments.filter((segment) => segment.ja && !segment.zh).length : pending;
    const warning = project.import_error || (failed && !projectBusy(project) ? isImport ? `${failed} 句中文翻译尚未完成，日语字幕和音频已保留。` : `${failed} 段字幕尚未完成，已保留本机音频。补全会保留已有原文和成功译文。` : "");
    $("preview-warning").textContent = warning; $("preview-warning").hidden = !warning;
    $("preview-timing-note").hidden = !project.import_timestamps_approximate;
    $("preview-timing-note").textContent = project.import_timestamps_approximate ? "当前字幕时间为文本估算，剪辑前请调整；选择返回分句时间戳的语音模型可改善同步。" : "";
    $("preview-retry").hidden = projectBusy(project) || project.status === "recording" || !(failed || ["failed", "interrupted"].includes(project.import_stage));
    $("preview-retry").textContent = retrying ? "正在提交…" : isRefinement ? "继续精准校验" : isImport ? "继续处理未完成内容" : "补全字幕";
    await loadRefinement();
    if (projectBusy(project) || refinement?.active || refinement?.busy) pollTimer = setTimeout(() => loadProject().catch((error) => notice(error.message, true)), 2500);
  } catch (error) {
    if (projectBusy(project || {}) || error.status !== 404) pollTimer = setTimeout(() => loadProject().catch((next) => notice(next.message, true)), 5000);
    throw error;
  } finally { polling = false; }
}
for (const button of document.querySelectorAll(".language-switch [data-language]")) button.addEventListener("click", () => {
  language = button.dataset.language;
  for (const item of document.querySelectorAll(".language-switch [data-language]")) { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); }
  $("preview-timeline").dataset.language = language; updateCaption(true);
});
audio.addEventListener("timeupdate", () => updateCaption());
audio.addEventListener("seeked", () => updateCaption(true));
audio.addEventListener("ended", () => { if (currentPart + 1 < parts.length) setPart(currentPart + 1, 0, true); });
audio.addEventListener("error", () => notice("音频无法播放，请检查音频文件是否已准备完成；也可以下载音频后试听。", true));
for (const event of ["wheel", "touchmove"]) $("preview-timeline").addEventListener(event, () => { $("follow-playback").checked = false; }, { passive: true });
$("follow-playback").addEventListener("change", () => { if ($("follow-playback").checked) updateCaption(true); });
$("audio-part").addEventListener("change", () => setPart(Number($("audio-part").value), 0, !audio.paused));
$("refinement-consent").addEventListener("change", renderRefinement);
$("refinement-refresh").addEventListener("click", async () => {
  if (checkingRefinement || refining) return;
  checkingRefinement = true;
  $("refinement-refresh").textContent = "正在检查…";
  renderRefinement();
  notice("正在重新读取精准校验配置…");
  try {
    await loadProject();
    if (refinement?.error) notice(refinement.message, true);
    else notice("已更新精准校验配置状态，请查看上方提示。", false, "success");
  } catch (error) { notice(error.message, true); }
  finally {
    checkingRefinement = false;
    $("refinement-refresh").textContent = "重新检查配置";
    renderRefinement();
  }
});
$("preview-refine").addEventListener("click", async () => {
  if (refining || !refinement?.eligible || !refinement.ready || refinement.active || refinement.busy || (refinement.stage === "completed" && refinement.project_status === "completed") || projectBusy(project) || !$("refinement-consent").checked) return;
  const resume = refinement.project_id && (["failed", "interrupted"].includes(refinement.stage) || ["failed", "interrupted"].includes(refinement.project_status));
  refining = true; renderRefinement();
  try {
    const path = resume ? `/api/imports/${encodeURIComponent(refinement.project_id)}/retry` : `/api/sessions/${encodeURIComponent(identifier)}/refine`;
    await jsonRequest(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ consent: true }) });
    $("refinement-consent").checked = false;
    notice(resume ? "已继续精准校验，已完成的步骤会保留。" : "精准校验已开始。原录音和直播字幕已保留，精修进度会自动更新。");
    await loadProject();
  } catch (error) { notice(error.message, true); await loadRefinement(); }
  finally { refining = false; renderRefinement(); }
});
$("preview-retry").addEventListener("click", async () => {
  if (retrying || !project || projectBusy(project)) return;
  retrying = true;
  const label = $("preview-retry").textContent;
  $("preview-retry").disabled = true;
  $("preview-retry").textContent = "正在提交…";
  $("preview-retry").setAttribute("aria-busy", "true");
  notice("正在提交继续处理请求，已完成的内容会保留。");
  try {
    const path = project.source_type === "import" ? `/api/imports/${encodeURIComponent(identifier)}/retry` : `/api/sessions/${encodeURIComponent(identifier)}/retry-translations`;
    await jsonRequest(path, { method: "POST" }, 180000); notice("处理已继续，字幕会自动更新。"); await loadProject();
  } catch (error) { notice(error.message, true); }
  finally {
    retrying = false;
    $("preview-retry").disabled = false;
    $("preview-retry").textContent = label;
    $("preview-retry").setAttribute("aria-busy", "false");
  }
});
if (!/^[a-f0-9]{32}$/.test(identifier)) { $("preview-title").textContent = "找不到这个项目"; notice("项目编号无效，请从音频翻译项目列表打开。", true); }
else loadProject().catch((error) => { $("preview-title").textContent = "项目读取失败"; notice(error.message, true); });
