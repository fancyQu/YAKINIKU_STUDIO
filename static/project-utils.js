/* Shared timeline rules for imported audio and saved recording previews. */
export const IMPORT_STAGES = {
  uploading: "正在导入音频", queued: "等待处理", decoding: "正在准备音频", transcribing: "正在识别日语",
  translating: "GPT 校正与翻译中", completed: "处理完成", failed: "处理失败", interrupted: "处理已中断",
};
export function projectBusy(project) {
  if (["uploading", "queued", "decoding", "transcribing", "translating"].includes(project.import_stage)) return true;
  return project.source_type !== "import" && (["recording", "finalizing", "processing", "backfilling"].includes(project.status) || project.subtitle_status === "backfilling");
}
export function pendingSubtitleCount(project) {
  const count = (project.segments || []).filter((segment) => {
    if (segment.zh || ["no_speech", "skipped"].includes(segment.asr_status)) return false;
    return (segment.asr_status && segment.asr_status !== "done") || ((segment.ja || segment.ja_raw) && segment.translation_status !== "skipped");
  }).length;
  const tail = project.source_type !== "import" && Number.isFinite(Number(project.live_recovery_from_ms)) && project.live_recovery_from_ms != null && Number(project.live_recovery_from_ms) < Number(project.duration_ms) ? 1 : 0;
  return Math.max(count, Number(project.pending_subtitle_count) || 0) + tail;
}
export function segmentPendingText(segment) {
  if (!segment) return "";
  if (["no_speech", "skipped"].includes(segment.asr_status)) return "无语音 · 已跳过";
  if (!segment.ja && !segment.ja_raw) {
    if (segment.asr_status === "failed") return "识别未完成 · 音频已保留，可补全字幕";
    if (segment.asr_status === "deferred") return "等待补全识别 · 音频已保留";
    return "正在识别日语…";
  }
  return segment.translation_status === "failed" ? "翻译失败 · 日语与音频已保留" : segment.translation_status === "deferred" ? "等待补全翻译" : "中文翻译尚未完成";
}
export function clockTime(milliseconds = 0) {
  const seconds = Math.floor(Math.max(0, Number(milliseconds) || 0) / 1000);
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
export function normalizedSegments(segments = []) {
  return segments.filter((segment) => Number.isFinite(Number(segment.start_ms)) && Number(segment.end_ms) > Number(segment.start_ms) &&
      !(["no_speech", "skipped"].includes(segment.asr_status) && !segment.ja && !segment.ja_raw && !segment.zh))
    .map((segment) => ({ ...segment, start_ms: Number(segment.start_ms), end_ms: Number(segment.end_ms) }))
    .sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms);
}
export function cueAt(segments, milliseconds) {
  let left = 0, right = segments.length - 1, result = -1;
  while (left <= right) {
    const mid = (left + right) >> 1;
    if (segments[mid].start_ms <= milliseconds) { result = mid; left = mid + 1; }
    else right = mid - 1;
  }
  return result >= 0 && milliseconds < segments[result].end_ms ? result : -1;
}
export function scrollTimelineToCue(container, row, { behavior = "smooth", padding = 12 } = {}) {
  if (!container || !row || !container.contains(row) || container.clientHeight <= 0) return false;
  const viewportTop = container.getBoundingClientRect().top + container.clientTop;
  const rowBounds = row.getBoundingClientRect();
  const inset = Math.min(Math.max(0, padding), container.clientHeight / 4);
  const visibleTop = viewportTop + inset;
  const visibleBottom = viewportTop + container.clientHeight - inset;
  let delta = 0;
  if (rowBounds.top < visibleTop || rowBounds.height > visibleBottom - visibleTop) delta = rowBounds.top - visibleTop;
  else if (rowBounds.bottom > visibleBottom) delta = rowBounds.bottom - visibleBottom;
  if (Math.abs(delta) < 1) return false;
  const top = Math.min(Math.max(0, container.scrollTop + delta), Math.max(0, container.scrollHeight - container.clientHeight));
  if (Math.abs(top - container.scrollTop) < 1) return false;
  // scrollIntoView also scrolls the page and any other ancestors. Following
  // a cue must change this list's viewport alone, even below the player.
  const reduceMotion = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  container.scrollTo({ top, behavior: reduceMotion ? "auto" : behavior });
  return true;
}
export function subtitleLines(segment, mode) {
  if (!segment) return [];
  if (mode === "ja") return [segment.ja || ""];
  if (mode === "zh") return [segment.zh || "中文翻译尚未完成"];
  return [segment.zh || "中文翻译尚未完成", segment.ja || ""];
}
export function audioParts(project) {
  const files = new Set((project.files || []).map((file) => file.name));
  const valid = (name) => /^audio(?:-\d{3,})?\.wav$/.test(name || "");
  const parts = (project.audio_parts || []).filter((part) => valid(part.name) && files.has(part.name));
  if (parts.length) return parts;
  return files.has("audio.wav") ? [{ name: "audio.wav", start_ms: 0, end_ms: project.duration_ms || 0 }] : [];
}
export function partForTime(parts, milliseconds) {
  return parts.findIndex((part) => milliseconds >= part.start_ms && milliseconds < part.end_ms);
}
export function projectFileURL(id, name) {
  return `/api/sessions/${encodeURIComponent(id)}/files/${encodeURIComponent(name)}`;
}
export async function jsonRequest(path, options = {}, timeout = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, { credentials: "same-origin", ...options, signal: controller.signal });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof body.detail === "string" ? body.detail : `本机服务返回错误（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("本机服务响应超时。请保持服务运行，检查当前状态后重试。");
    if (error instanceof TypeError) throw new Error("无法连接本机服务。请确认启动终端仍在运行，恢复连接后重试。");
    throw error;
  } finally { clearTimeout(timer); }
}
