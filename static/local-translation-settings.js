export const DEFAULT_LOCAL_TRANSLATION_MODEL = "qwen3-4b-instruct-2507-4bit";

const LOCAL_RUNTIME_LABELS = { not_prepared: "未加载", loading: "加载中", ready: "已就绪", failed: "加载失败" };
const LOCAL_DOWNLOAD_LABELS = { missing: "未下载", downloading: "下载中", verifying: "校验中", downloaded: "已下载", failed: "下载失败", cancelled: "已取消" };
const localTransferActive = (value) => value && ["downloading", "verifying"].includes(value.state);

export function createLocalTranslationSettings({ document: doc, request, onChange = () => {}, onFeedback = () => {}, getContext = () => ({}), refreshLock = async () => {}, getOrigin = () => doc.defaultView?.location?.origin || "" }) {
  const $ = (id) => doc.getElementById(id);
  let catalog = [], modelStatus = {}, active = null, hardware = {}, runtimeAvailable = null;
  let catalogLoaded = false, checking = true, catalogError = "", statusError = "", busy = false, action = "", selectionRevision = 0;
  let catalogErrorStatus = 0, statusErrorStatus = 0;
  function selected() { return $("live-local-translation-model").value || ""; }
  function entry() { return catalog.find((item) => item.id === selected()); }
  function runtime() { return { ...(entry() || {}), ...modelStatus }; }
  function download() { const value = runtime(); return value.download || { state: value.prepared_files || value.state === "ready" ? "downloaded" : "missing" }; }
  function filesReady() { return !checking && !statusError && !localTransferActive(download()) && Boolean(runtime().prepared_files || runtime().state === "ready" || download().state === "downloaded"); }
  function bytes(value) { const amount = Number(value) || 0; return amount >= 1e9 ? `${(amount / 1e9).toFixed(2)} GB` : `${(amount / 1e6).toFixed(0)} MB`; }
  function memory(item) {
    const range = item.estimated_runtime_memory_gb || [item.memory_gb_min, item.memory_gb_max];
    return Number.isFinite(Number(range[0])) && Number.isFinite(Number(range[1])) ? `约 ${Number(range[0])}–${Number(range[1])} GB（估计）` : "待提供估计";
  }
  function label(item) { const text = item.label || item.id; return `${text}${item.recommended && !text.includes("推荐") ? " · 推荐" : ""}`; }
  function sourceLabel(transfer = {}, item = {}) {
    const source = transfer.download_source || item.download_source;
    const labels = { modelscope: "阿里 ModelScope（国内源）", huggingface: "Hugging Face" };
    const platform = transfer.source_label || (transfer.download_source ? labels[source] : item.source_label) || labels[source] || "由本机服务提供的下载平台";
    const repository = transfer.repo_id || item.repo_id;
    return `${platform}${repository ? ` · ${repository}` : ""}`;
  }
  function stateLabel(item) {
    const value = item.id === selected() ? runtime() : item;
    if (["ready", "loading", "failed"].includes(value.state)) return LOCAL_RUNTIME_LABELS[value.state];
    return LOCAL_DOWNLOAD_LABELS[value.download?.state || (value.prepared_files ? "downloaded" : "missing")] || "未下载";
  }
  function renderCatalog() {
    const rows = catalog.map((item) => {
      const row = doc.createElement("article"); row.className = "local-translation-model-row"; row.dataset.selected = String(item.id === selected());
      const heading = doc.createElement("div"); heading.className = "local-translation-model-row-heading";
      const title = doc.createElement("strong"); title.textContent = label(item);
      const badge = doc.createElement("span"); badge.className = "workflow-ready"; badge.textContent = stateLabel(item); badge.dataset.ready = String((item.id === selected() ? runtime() : item).state === "ready");
      heading.append(title, badge);
      const detail = doc.createElement("p"); detail.textContent = `下载约 ${bytes(item.disk_bytes)} · 运行 ${memory(item)}`;
      const description = doc.createElement("p"); description.textContent = item.description || "4-bit 本机文字模型";
      row.append(heading, detail, description); return row;
    });
    $("local-translation-catalog").replaceChildren(...rows);
  }
  function render() {
    const ctx = getContext(), local = ctx.backend === "local", value = runtime(), transfer = download();
    const disabled = !ctx.loaded || ctx.busy || ctx.locked || ctx.fetching || ctx.asrDownloading || busy || Boolean(active);
    $("live-cloud-translation-settings").hidden = local;
    $("live-local-translation-settings").hidden = !local;
    $("live-local-translation-model").disabled = disabled || !catalogLoaded;
    $("download-local-translation-model").disabled = disabled || checking || Boolean(catalogError || statusError) || !entry() || filesReady() || value.state === "loading";
    $("prepare-local-translation-model").disabled = disabled || !runtimeAvailable || !entry() || !filesReady() || value.state === "loading" || value.state === "ready";
    $("download-local-translation-model").textContent = action === "download" ? "正在开始下载…" : filesReady() ? "模型已下载" : ["failed", "cancelled"].includes(transfer.state) ? "重新下载模型" : "下载模型";
    $("prepare-local-translation-model").textContent = action === "prepare" || value.state === "loading" ? "正在加载模型…" : value.state === "ready" ? "模型已就绪" : "加载模型";
    $("download-local-translation-model").setAttribute("aria-busy", String(action === "download"));
    $("prepare-local-translation-model").setAttribute("aria-busy", String(action === "prepare" || value.state === "loading"));
    $("local-translation-description").textContent = entry()?.description || "从本机服务返回的模型目录中选择模型。";
    $("local-translation-size").textContent = entry()?.disk_bytes ? `约 ${bytes(entry().disk_bytes)}（以实际文件为准）` : "等待模型目录";
    $("local-translation-memory").textContent = entry() ? memory(entry()) : "等待模型目录";
    $("local-translation-source").textContent = sourceLabel(transfer, entry());
    $("local-translation-memory-note").textContent = "估计：4bit 模型、单请求、约 2K tokens 短上下文，含模型权重、KV 缓存与推理运行时；不包含语音识别模型、macOS、浏览器和其他应用，请保留余量。";
    $("local-translation-hardware").textContent = hardware.total_memory_gb ? `这台 Mac 共有约 ${Number(hardware.total_memory_gb).toFixed(0)} GB 统一内存。4B 为默认推荐，3B 适合内存较紧张的情况。` : "4B 为默认推荐；内存较紧张时可选 3B，并为语音识别和系统留出余量。";
    $("local-translation-runtime-badge").textContent = checking ? "等待检查" : !runtimeAvailable ? "环境未就绪" : stateLabel({ id: selected() });
    $("local-translation-runtime-badge").dataset.ready = String(!checking && value.state === "ready");
    const issue = catalogError || statusError;
    const needsRestart = catalogErrorStatus === 404 || statusErrorStatus === 404;
    $("local-translation-runtime-status").textContent = issue ? `${issue} 当前选择仍保留${needsRestart ? "。" : "，将自动重试状态读取。"}云端翻译仍可配置。` : !runtimeAvailable ? "本机翻译运行环境不可用。请按启动说明安装 MLX 文字推理依赖后重新启动；不会自动切换云端。" : checking ? "正在检查所选模型的本机文件…" : value.error || (value.state === "ready" ? "模型已加载，可保存为直播翻译方式。" : value.state === "loading" ? "正在加载本机权重并预热，不会联网下载。" : filesReady() ? "模型文件已在本机。点击「加载模型」准备推理；保存配置使选择生效。" : transfer.error || transfer.message || "尚未准备模型文件。仅点击「下载模型」后才联网下载，不会自动保存选择。");
    $("local-translation-runtime-status").dataset.level = issue || value.state === "failed" || transfer.state === "failed" || runtimeAvailable === false ? "error" : value.state === "ready" ? "success" : "info";
    $("local-translation-active-download").hidden = !active;
    const activeEntry = catalog.find((item) => item.id === active?.model), total = Number(active?.total_bytes) || 0, received = Number(active?.downloaded_bytes) || 0;
    $("local-translation-active-heading").textContent = active ? `${active.state === "verifying" ? "正在校验" : "正在下载"}：${activeEntry?.label || active.model}` : "本机翻译模型下载";
    $("local-translation-download-badge").textContent = LOCAL_DOWNLOAD_LABELS[active?.state] || "下载中";
    $("local-translation-download-progress").hidden = !active || total <= 0;
    $("local-translation-download-progress").value = total > 0 ? Math.min(100, received / total * 100) : 0;
    $("local-translation-download-status").textContent = active ? `${active.message || (active.state === "verifying" ? "正在校验模型文件" : "正在下载模型文件")} · ${sourceLabel(active, activeEntry)}${total ? ` · ${bytes(received)} / ${bytes(total)}` : received ? ` · 已下载 ${bytes(received)}` : ""}${active.model !== selected() ? "。当前任务与上方模型选择不同；取消将作用于正在下载的模型。" : "。"} 下载不会改变已保存的翻译配置。` : "";
    $("cancel-local-translation-download").disabled = !ctx.loaded || ctx.busy || busy || !active;
    $("cancel-local-translation-download").textContent = action === "cancel" ? "正在取消…" : "取消下载";
    $("cancel-local-translation-download").setAttribute("aria-busy", String(action === "cancel"));
    renderCatalog();
  }
  function notify() { render(); onChange(); }
  function readError(error, label) {
    if (error.status !== 404) return `${label}暂时无法读取：${error.message}`;
    const origin = getOrigin();
    return `当前页面使用的本机服务${origin ? `（${origin}）` : ""}尚未提供本机翻译接口（404），可能仍在运行旧版本。请结束该服务上的录制或字幕补全后，重启${origin ? "该地址对应的" : ""}本机服务，再点击「重新读取」。此错误与模型下载源无关。`;
  }
  function updateActive(result) {
    if (Object.hasOwn(result, "active_download")) active = localTransferActive(result.active_download) ? result.active_download : null;
  }
  function select(model) {
    const field = $("live-local-translation-model");
    if (![...field.children].some((item) => item.value === model)) { const option = doc.createElement("option"); option.value = model; option.textContent = catalog.find((item) => item.id === model)?.label || model || "请选择本机模型"; field.append(option); }
    if (selected() !== model) { modelStatus = {}; checking = true; statusError = ""; statusErrorStatus = 0; selectionRevision++; }
    field.value = model;
  }
  async function updateStatus() {
    if (!getContext().loaded || !selected()) return;
    const model = selected(), currentRevision = selectionRevision;
    try {
      const result = await request(`/api/live-translation/status?model=${encodeURIComponent(model)}`);
      if (currentRevision !== selectionRevision || model !== selected()) return;
      modelStatus = result; checking = false; statusError = ""; statusErrorStatus = 0; updateActive(result);
      if (Object.hasOwn(result, "runtime_available")) runtimeAvailable = Boolean(result.runtime_available);
      notify();
    } catch (error) {
      if (currentRevision !== selectionRevision || model !== selected()) return;
      checking = true; statusErrorStatus = error.status || 0; statusError = readError(error, "本机翻译状态"); notify();
    }
  }
  async function refresh() {
    if (busy) return;
    try {
      const result = await request("/api/live-translation/models");
      catalog = (result.models || []).filter((item) => item && typeof item.id === "string");
      catalogLoaded = true; catalogError = ""; catalogErrorStatus = 0; hardware = result.hardware || {}; runtimeAvailable = result.runtime_available !== false; updateActive(result);
      const model = selected(), field = $("live-local-translation-model"), options = catalog.map((item) => { const option = doc.createElement("option"); option.value = item.id; option.textContent = `${label(item)} · ${bytes(item.disk_bytes)} · ${memory(item)}`; return option; });
      field.replaceChildren(...options); select(model);
      if (!catalog.some((item) => item.id === model)) { checking = true; statusErrorStatus = 0; statusError = "已选模型不在可用目录中，请重新选择；原保存选择保持不变。"; notify(); return; }
      notify(); await updateStatus();
    } catch (error) { catalogErrorStatus = error.status || 0; catalogError = readError(error, "本机翻译模型目录"); notify(); }
  }
  async function perform(kind) {
    const ctx = getContext(), target = kind === "cancel" ? active?.model : selected();
    if (!ctx.loaded || ctx.busy || busy || !target || (kind !== "cancel" && (ctx.locked || ctx.fetching || ctx.asrDownloading || active || !entry() || checking || catalogError || statusError))) return;
    if (kind === "prepare" && (!runtimeAvailable || !filesReady() || ["ready", "loading"].includes(runtime().state))) return;
    if (kind === "download" && (filesReady() || runtime().state === "loading")) return;
    busy = true; action = kind; notify();
    const path = kind === "cancel" ? "download/cancel" : kind;
    try {
      const result = await request(`/api/live-translation/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: target, ...(kind === "download" ? { consent: true } : {}) }) }, kind === "prepare" ? 120000 : undefined);
      const transfer = result.download || result;
      if (kind === "prepare" && target === selected()) { modelStatus = { ...modelStatus, ...result }; checking = false; }
      else {
        if (target === selected()) modelStatus = { ...modelStatus, download: transfer };
        active = localTransferActive(transfer) ? { ...transfer, model: target } : null;
      }
      updateActive(result);
      onFeedback(kind === "prepare" ? result.state === "ready" ? "本机翻译模型已加载。当前配置修改仍保留；点击「保存直播配置」使选择生效。" : result.error || result.message || "本机翻译模型正在加载，请查看状态。" : kind === "cancel" ? active ? "正在取消本机翻译模型下载，请等待当前写入结束。" : "本机翻译模型下载已取消。已保存配置和当前修改均保留。" : "本机翻译模型下载已开始；下载不会改变配置或自动加载。", kind === "prepare" && result.state === "failed" ? "error" : "success");
    } catch (error) { onFeedback(`${kind === "prepare" ? "本机翻译模型加载未完成" : kind === "cancel" ? "取消本机翻译下载未完成" : "本机翻译模型下载未能开始"}：${error.message} 当前修改仍保留。`, "error"); }
    finally { busy = false; action = ""; notify(); try { await refreshLock(); } catch {} await refresh(); }
  }
  $("live-local-translation-model").addEventListener("change", async () => { modelStatus = {}; checking = true; statusError = ""; statusErrorStatus = 0; selectionRevision++; notify(); await updateStatus(); });
  $("download-local-translation-model").addEventListener("click", () => perform("download"));
  $("prepare-local-translation-model").addEventListener("click", () => perform("prepare"));
  $("cancel-local-translation-download").addEventListener("click", () => perform("cancel"));
  return { select, refresh, updateStatus, render, download: () => perform("download"), prepare: () => perform("prepare"), cancelDownload: () => perform("cancel"), get model() { return selected(); }, get busy() { return busy; }, get activeDownload() { return active; }, get ready() { return runtimeAvailable && !checking && !catalogError && !statusError && runtime().state === "ready"; }, get readinessLabel() { return catalogError || statusError ? "状态未知" : !runtimeAvailable ? "环境未就绪" : checking ? "等待检查" : stateLabel({ id: selected() }); } };
}
