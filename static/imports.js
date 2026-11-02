import { IMPORT_STAGES, projectBusy, clockTime, projectFileURL, jsonRequest } from "./project-utils.js?v=ui-navigation-1";
import { createImportSettings } from "./import-settings.js?v=ui-navigation-1";
import { createSubtitleSettings } from "./subtitle-settings.js?v=ui-navigation-1";
const $ = (id) => document.getElementById(id);
let selectedFile = null;
let uploading = false;
let pollTimer = null;
let loading = false;
let importSettings = null;
let subtitleSettings = null;
let retrying = null;
const retryButtons = new Set();
function updateImportControls() {
  $("start-import").disabled = uploading || Boolean(retrying) || !selectedFile || !importSettings?.ready || !subtitleSettings?.ready;
  $("choose-audio").disabled = uploading;
  $("import-title").disabled = uploading;
  importSettings?.setLocked(uploading);
  subtitleSettings?.setLocked(uploading);
  $("start-import").textContent = uploading ? "正在导入音频…" : "开始转写与翻译";
  $("start-import").setAttribute?.("aria-busy", String(uploading));
  $("import-form").setAttribute?.("aria-busy", String(uploading));
  for (const button of retryButtons) {
    button.disabled = uploading || Boolean(retrying) || Boolean(subtitleSettings?.locked);
    const busy = button.dataset.projectId === retrying;
    button.textContent = busy ? "正在继续…" : "继续处理";
    button.setAttribute?.("aria-busy", String(busy));
    button.title = subtitleSettings?.locked ? "请等待当前录制或音频处理结束。" : "从已保存的进度继续，仅补未完成步骤。";
  }
  $("start-import-help").textContent = uploading ? "正在把音频传到本机，随后发送至所选云端语音服务。"
    : retrying ? "正在提交继续处理请求，请稍候。"
    : !importSettings?.loaded ? importSettings?.busy ? "正在读取语音识别配置…" : "尚未读取识别配置，请点击上方「重新读取设置」。"
    : importSettings.dirty ? "识别 API 配置有未保存修改，请先保存。"
    : importSettings.busy ? "正在保存配置或获取模型，请稍候。"
    : !importSettings.configured ? "请先完成上方语音识别 API 设置，并配置工作台的中文翻译服务。"
    : !subtitleSettings?.loaded ? "请先读取上方字幕分句设置。"
    : subtitleSettings.dirty ? "字幕分句设置有未保存修改，请先点击「保存分句设置」。"
    : subtitleSettings.locked ? "正在录制或处理音频，完成后可开始新项目。"
    : subtitleSettings.busy ? "正在保存字幕分句设置，请稍候。"
    : !selectedFile ? "识别与翻译配置已就绪，请选择音频。"
    : "将使用已保存的云端语音识别和 GPT 翻译服务。";
}
function notice(message, error = false) {
  $("import-notice").textContent = message;
  $("import-notice").dataset.level = error ? "error" : "success";
  $("import-notice").hidden = !message;
}
function selectFile(file) {
  if (uploading || !file) return;
  selectedFile = null;
  $("selected-audio").hidden = true;
  updateImportControls();
  if (!/\.(wav|mp3|m4a|flac|ogg|opus|aac|mp4|webm|wma|aiff|aif)$/i.test(file.name)) { notice("请选择 WAV、MP3、M4A、FLAC、OGG、OPUS、AAC、MP4、WEBM、WMA 或 AIFF 音频文件。", true); $("audio-file").value = ""; return; }
  if (!file.size || file.size > 8 * 1024 ** 3) { notice("请选择非空音频文件，单个文件最大 8 GB。", true); $("audio-file").value = ""; return; }
  selectedFile = file;
  notice("");
  $("selected-audio").hidden = false;
  $("selected-audio").textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
  updateImportControls();
}
function downloadLink(id, name, label) {
  const link = document.createElement("a");
  link.href = projectFileURL(id, name); link.download = name; link.textContent = label;
  return link;
}
function render(projects) {
  retryButtons.clear();
  const fragment = document.createDocumentFragment();
  if (!projects.length) {
    const empty = document.createElement("p"); empty.className = "project-empty"; empty.textContent = "还没有音频翻译项目。导入第一份音频，开始制作字幕。"; fragment.append(empty);
  }
  for (const project of projects) {
    const article = document.createElement("article"); article.className = "import-project";
    const top = document.createElement("div"); top.className = "import-project-top";
    const title = document.createElement("a"); title.className = "import-project-title"; title.href = `/static/preview.html?id=${encodeURIComponent(project.id)}`; title.target = "_blank"; title.rel = "noopener"; title.textContent = project.title || project.original_filename || "音频项目";
    const status = document.createElement("span"); status.className = "project-status"; status.dataset.stage = project.import_stage || project.status; status.textContent = IMPORT_STAGES[project.import_stage] || project.status;
    top.append(title, status);
    const meta = document.createElement("p"); meta.className = "project-meta";
    const date = new Date(project.created_at);
    meta.textContent = [Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }), clockTime(project.duration_ms), `${project.segment_count ?? project.segments?.length ?? 0} 句`].filter(Boolean).join(" · ");
    article.append(top, meta);
    if (projectBusy(project)) {
      const progress = document.createElement("progress"); progress.max = 100; progress.value = Math.min(100, Math.max(0, Number(project.import_progress) || 0)); progress.setAttribute("aria-label", `${status.textContent} ${Math.round(progress.value)}%`);
      const detail = document.createElement("p"); detail.className = "project-meta"; detail.textContent = `${project.import_progress_label || status.textContent} · ${Math.round(progress.value)}%`;
      article.append(progress, detail);
    }
    if (project.import_error) { const error = document.createElement("p"); error.className = "project-error"; error.textContent = project.import_error; article.append(error); }
    const links = document.createElement("div"); links.className = "recording-links project-actions";
    const preview = document.createElement("a"); preview.href = title.href; preview.target = "_blank"; preview.rel = "noopener"; preview.textContent = "打开项目预览"; preview.className = "project-preview-link"; links.append(preview);
    const files = new Set((project.files || []).map((file) => file.name));
    for (const [name, label] of [["ja.srt", "日语 SRT"], ["zh-CN.srt", "中文 SRT"], ["bilingual.srt", "双语 SRT"]]) {
      if (files.has(name)) links.append(downloadLink(project.id, name, label));
    }
    if (["failed", "interrupted"].includes(project.import_stage) || (!projectBusy(project) && project.failed_translation_count > 0)) {
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "retry-button"; retry.textContent = "继续处理";
      retry.dataset.projectId = project.id; retryButtons.add(retry);
      retry.addEventListener("click", async () => {
        if (uploading || retrying || subtitleSettings?.locked) { notice("请等待当前录制或音频处理结束，再继续该项目。", true); return; }
        // A completed ASR checkpoint may only need translation. The backend
        // validates the saved project rather than requiring a new ASR setup.
        retrying = project.id; updateImportControls(); notice("正在继续处理该项目…");
        try {
          await jsonRequest(`/api/imports/${encodeURIComponent(project.id)}/retry`, { method: "POST" });
          notice("已继续处理该项目。");
          try { await loadProjects(); }
          catch { notice("已继续处理该项目，但项目列表暂时无法读取，请点击「刷新」。"); }
        }
        catch (error) { notice(error.message, true); }
        finally { retrying = null; updateImportControls(); }
      }); links.append(retry);
    }
    article.append(links); fragment.append(article);
  }
  $("import-projects").replaceChildren(fragment);
  updateImportControls();
}
async function loadProjects({ manual = false } = {}) {
  if (loading) return;
  loading = true; clearTimeout(pollTimer);
  $("refresh-imports").disabled = true; $("refresh-imports").textContent = "刷新中…";
  $("refresh-imports").setAttribute?.("aria-busy", "true");
  try {
    const body = await jsonRequest("/api/sessions");
    const projects = (Array.isArray(body) ? body : body.sessions || []).filter((project) => project.source_type === "import");
    render(projects);
    if (manual) notice("项目列表已更新。");
    if (projects.some(projectBusy)) pollTimer = setTimeout(() => loadProjects().catch((error) => notice(error.message, true)), 2500);
  } catch (error) {
    pollTimer = setTimeout(() => loadProjects().catch((next) => notice(next.message, true)), 5000);
    throw error;
  } finally {
    loading = false; $("refresh-imports").disabled = false; $("refresh-imports").textContent = "刷新";
    $("refresh-imports").setAttribute?.("aria-busy", "false");
  }
}
function upload(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/imports");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    if ($("import-title").value.trim()) request.setRequestHeader("X-Title", encodeURIComponent($("import-title").value.trim()));
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round(event.loaded * 100 / event.total);
        $("upload-progress").value = percent;
        $("upload-label").textContent = percent >= 100 ? "音频已传到本机，正在建立项目…" : `正在上传到本机… ${percent}%`;
      }
    };
    request.onload = () => {
      let body = {}; try { body = JSON.parse(request.responseText); } catch {}
      if (request.status < 200 || request.status >= 300) reject(new Error(typeof body.detail === "string" ? body.detail : `导入失败（${request.status}）`));
      else resolve(body);
    };
    request.onerror = () => reject(new Error("无法连接本机服务，请保持启动终端运行。"));
    request.onabort = () => reject(new Error("音频导入已中断。"));
    request.send(file);
  });
}
$("choose-audio").addEventListener("click", () => $("audio-file").click());
$("audio-file").addEventListener("change", () => selectFile($("audio-file").files[0]));
for (const event of ["dragenter", "dragover"]) $("audio-dropzone").addEventListener(event, (e) => { e.preventDefault(); $("audio-dropzone").classList.add("dragover"); });
for (const event of ["dragleave", "drop"]) $("audio-dropzone").addEventListener(event, (e) => { e.preventDefault(); $("audio-dropzone").classList.remove("dragover"); });
$("audio-dropzone").addEventListener("drop", (e) => selectFile(e.dataTransfer.files[0]));
$("refresh-imports").addEventListener("click", () => loadProjects({ manual: true }).catch((error) => notice(error.message, true)));
$("import-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!selectedFile || uploading) return;
  if (!importSettings?.ready) { notice("请先保存并完成识别 API 设置，再开始转写。", true); return; }
  if (!subtitleSettings?.ready) { notice("请先读取并保存字幕分句设置，待当前录制或处理结束后再开始。", true); return; }
  uploading = true; updateImportControls();
  $("upload-progress-wrap").hidden = false; $("upload-progress").value = 0; $("upload-label").textContent = "正在上传到本机…"; notice("");
  try {
    await upload(selectedFile);
    notice("音频已导入，后台正在处理。完成后点开项目即可预览并下载三份 SRT。");
    selectedFile = null; $("audio-file").value = ""; $("selected-audio").hidden = true; $("import-title").value = "";
    try { await loadProjects(); }
    catch { notice("音频已导入，后台正在处理；项目列表暂时无法读取，请点击「刷新」。"); }
  } catch (error) { notice(error.message, true); }
  finally { uploading = false; updateImportControls(); $("upload-progress-wrap").hidden = true; }
});
importSettings = createImportSettings({ onChange: updateImportControls, onError: (message) => notice(message, true) });
importSettings.load();
subtitleSettings = createSubtitleSettings({ onChange: updateImportControls, onError: (message) => notice(message, true) });
subtitleSettings.load();
window.addEventListener("beforeunload", (event) => {
  if (uploading || importSettings?.dirty || subtitleSettings?.dirty || selectedFile || $("import-title").value.trim()) {
    event.preventDefault(); event.returnValue = "";
  }
});
loadProjects().catch((error) => notice(error.message, true));
