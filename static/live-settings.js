import { jsonRequest } from "./project-utils.js?v=ui-navigation-1";
import { createLocalTranslationSettings, DEFAULT_LOCAL_TRANSLATION_MODEL } from "./local-translation-settings.js?v=local-translation-1";

const MODEL_LABELS = { "mlx-whisper-small": "MLX · Whisper Small", "mlx-whisper-large-v3-turbo": "MLX · Whisper large-v3-turbo", tiny: "CPU · Whisper Tiny" };
const STATE_LABELS = { not_prepared: "未准备", loading: "加载中", ready: "已就绪", failed: "加载失败" };
const DOWNLOAD_LABELS = { missing: "未下载", downloading: "下载中", verifying: "校验中", downloaded: "已下载", failed: "下载失败", cancelled: "已取消" };

export function createLiveSettingsUI({ document: doc = document, request = jsonRequest, confirmDiscard = (message) => confirm(message) } = {}) {
  const $ = (id) => doc.getElementById(id);
  let loaded = false, busy = false, locked = true, fetching = false, config = null;
  let baseline = "", models = [], modelStatus = {}, timer, stopped = true, revision = 0;
  let pendingAction = "", feedback = { message: "", level: "info" };
  let connectionLost = false, checkingModel = true;
  const controls = ["live-asr-model", "live-translation-backend", "live-translation-provider", "live-translation-model", "live-glossary", "live-segment-seconds", "live-silence-ms", "live-translation-model-list", "live-model-search"];
  const localTranslation = createLocalTranslationSettings({ document: doc, request, onChange: renderState, onFeedback: status, refreshLock, getContext: () => ({ loaded, busy, locked, fetching, asrDownloading: Boolean(activeDownload()), backend: $("live-translation-backend").value || "cloud" }) });
  function selectedModel() { const [backend, model] = $("live-asr-model").value.split(":"); return { backend, model }; }
  function value() { return { ...selectedModel(), translation_backend: $("live-translation-backend").value || "cloud", local_translation_model: localTranslation.model || DEFAULT_LOCAL_TRANSLATION_MODEL, translation_route: { provider_id: $("live-translation-provider").value, model: $("live-translation-model").value.trim() }, glossary: $("live-glossary").value, segment_seconds: Number($("live-segment-seconds").value), silence_ms: Number($("live-silence-ms").value) }; }
  function dirty() { return loaded && JSON.stringify(value()) !== baseline; }
  function status(message, level = "info") { feedback = { message, level }; $("live-settings-status").textContent = message; $("live-settings-status").dataset.level = level; }
  function option(label, value) { const item = doc.createElement("option"); item.textContent = label; item.value = value; return item; }
  function provider() { return (config?.providers || []).find((item) => item.id === $("live-translation-provider").value); }
  function translationReady() { if ($("live-translation-backend").value === "local") return Boolean(localTranslation.ready); const p = provider(); return Boolean(p?.enabled !== false && p?.base_url && p?.key_configured && $("live-translation-model").value.trim()); }
  function mutationBlocked() { return busy || locked || fetching || Boolean(activeDownload()) || localTranslation.busy || Boolean(localTranslation.activeDownload); }
  function downloadState() { return modelStatus.download?.state || (modelStatus.prepared_files || modelStatus.state === "ready" ? "downloaded" : "missing"); }
  function downloadActive() { return ["downloading", "verifying"].includes(downloadState()); }
  function activeDownload() {
    const active = modelStatus.active_download;
    if (active?.backend && active?.model && ["downloading", "verifying"].includes(active.state)) return active;
    return downloadActive() ? { ...selectedModel(), ...modelStatus.download } : null;
  }
  function displayedDownload() { return activeDownload() || modelStatus.download || {}; }
  function displayedDownloadState() { return displayedDownload().state || downloadState(); }
  function modelFilesReady() { return !checkingModel && !downloadActive() && (selectedModel().backend !== "mlx" || Boolean(modelStatus.prepared_files || modelStatus.state === "ready" || downloadState() === "downloaded")); }
  function bytes(value) { const size = Number(value) || 0; if (size < 1000 ** 2) return `${(size / 1000).toFixed(0)} KB`; return size < 1000 ** 3 ? `${(size / 1000 ** 2).toFixed(1)} MB` : `${(size / 1000 ** 3).toFixed(2)} GB`; }
  function renderState() {
    const disabled = !loaded || busy || locked || Boolean(activeDownload()) || localTranslation.busy || Boolean(localTranslation.activeDownload);
    for (const id of controls) $(id).disabled = disabled;
    $("save-live-settings").disabled = disabled || fetching;
    $("reload-live-settings").disabled = busy || fetching || localTranslation.busy;
    $("prepare-live-model").disabled = disabled || fetching || modelStatus.state === "loading" || !modelFilesReady();
    const downloading = Boolean(activeDownload()), downloaded = downloadState() === "downloaded";
    $("download-live-model").disabled = disabled || fetching || checkingModel || downloading || downloaded || modelStatus.state === "loading";
    $("download-live-model").textContent = pendingAction === "download" ? "正在开始下载…" : downloading ? displayedDownloadState() === "verifying" ? "正在校验…" : "正在下载…" : downloaded ? "模型已下载" : ["failed", "cancelled"].includes(downloadState()) ? "重新下载模型" : "下载模型";
    $("cancel-live-model-download").hidden = !downloading;
    $("cancel-live-model-download").disabled = busy || !loaded || !downloading;
    $("cancel-live-model-download").textContent = pendingAction === "cancel-download" ? "正在取消…" : "取消下载";
    $("live-fetch-models").disabled = disabled || fetching || $("live-translation-backend").value === "local" || !provider()?.base_url || !provider()?.key_configured;
    $("save-live-settings").textContent = pendingAction === "save" ? "正在保存直播配置…" : "保存直播配置";
    $("reload-live-settings").textContent = pendingAction === "load" ? "正在读取…" : "重新读取";
    $("prepare-live-model").textContent = pendingAction === "prepare" ? "正在保存并加载模型…" : "保存并加载模型";
    $("live-fetch-models").textContent = fetching ? "正在获取模型…" : "获取全部模型";
    for (const [id, running] of [["save-live-settings", pendingAction === "save"], ["reload-live-settings", pendingAction === "load"], ["prepare-live-model", pendingAction === "prepare"], ["live-fetch-models", fetching], ["download-live-model", pendingAction === "download" || downloading], ["cancel-live-model-download", pendingAction === "cancel-download"]]) $(id).setAttribute("aria-busy", String(running));
    $("live-settings-lock").hidden = !locked || !loaded;
    $("live-save-state").textContent = busy ? ({ load: "正在读取直播配置…", prepare: "正在保存并加载本机模型…", download: "正在提交模型下载；当前配置修改仍保留…", "cancel-download": "正在取消模型下载…" }[pendingAction] || "正在保存直播配置…") : feedback.level === "error" ? feedback.message : dirty() ? "直播配置有未保存的修改" : loaded ? config?.configured ? "直播配置已保存" : "当前显示初始选择，尚未独立保存" : "读取中";
    $("live-save-state").dataset.level = feedback.level;
    $("live-translation-badge").textContent = $("live-translation-backend").value === "local" ? localTranslation.readinessLabel : translationReady() ? "已配置" : "未配置";
    $("live-translation-badge").dataset.ready = String(translationReady());
    const p = provider();
    $("live-provider-state").textContent = p ? `${p.name}${p.enabled === false ? " · 此连接已停用" : !p.base_url || !p.key_configured ? " · 请先在服务商配置中补充地址和密钥" : " · 地址与密钥已配置（尚不代表 API 已验证）"}` : "请选择已保存的文字服务商；也可与完整音频共用连接，模型选择分别保存。";
    localTranslation.render();
  }
  function renderModelStatus() {
    const state = modelStatus.state || "not_prepared";
    $("live-model-badge").textContent = STATE_LABELS[state] || "未准备";
    $("live-model-badge").dataset.ready = String(state === "ready");
    $("live-model-status").textContent = checkingModel ? "正在检查所选模型的本机文件…" : state === "ready" ? `${MODEL_LABELS[selectedModel().model] || selectedModel().model} 已加载并预热。` : modelStatus.error || (downloadActive() ? "模型文件正在下载或校验，完成后才能加载。" : selectedModel().backend === "mlx" && !modelFilesReady() ? "本机尚无完整模型文件。请先点击「下载模型」，下载完成后再保存并加载。" : modelStatus.message || (state === "loading" ? "正在加载并预热，请稍候；此操作不会下载模型。" : "点击「保存并加载模型」，使用本机权重加载并预热。就绪后可开启实时字幕。"));
    $("live-model-status").dataset.level = state === "failed" ? "error" : "info";
    $("live-model-path").textContent = modelStatus.path || "—";
    $("live-model-version").textContent = typeof modelStatus.version === "object" ? JSON.stringify(modelStatus.version) : modelStatus.version || "—";
    const active = activeDownload(), download = displayedDownload(), ds = displayedDownloadState(), total = Number(download.total_bytes) || 0, received = Number(download.downloaded_bytes) || 0;
    $("live-model-download").hidden = selectedModel().backend !== "mlx" && !active;
    $("live-download-heading").textContent = active ? `${ds === "verifying" ? "正在校验" : "正在下载"}：${MODEL_LABELS[active.model] || active.model}` : "MLX 模型下载";
    $("live-download-badge").textContent = checkingModel && !active ? "等待检查" : DOWNLOAD_LABELS[ds] || "未下载";
    $("live-download-badge").dataset.ready = String(ds === "downloaded");
    $("live-download-source").textContent = download.repo_id ? `Hugging Face · ${download.repo_id}${download.revision ? ` · ${download.revision.slice(0, 12)}` : ""}` : "由本机服务提供模型来源";
    $("live-download-size").textContent = total > 0 ? `${bytes(total)}${ds === "downloaded" ? "" : "（预估，以实际下载为准）"}` : "由模型来源返回实际大小";
    $("live-download-progress").hidden = !active || total <= 0;
    $("live-download-progress").value = total > 0 ? Math.min(100, received / total * 100) : 0;
    const otherModel = active && (active.backend !== selectedModel().backend || active.model !== selectedModel().model);
    const activeNote = otherModel ? " 当前下载与上方保存的识别模型不同；可在这里取消下载，保存选择保持不变。" : "";
    $("live-download-status").textContent = (checkingModel && !active ? "正在检查本机模型文件…" : ds === "downloaded" ? "模型文件已在本机；点击「保存并加载模型」后才会加载并预热。" : ds === "downloading" ? `${download.message || "正在下载模型文件"}${total > 0 ? ` · ${bytes(received)} / ${bytes(total)}` : received > 0 ? ` · 已下载 ${bytes(received)}` : ""}` : ds === "verifying" ? download.message || "模型已下载，正在校验本机文件…" : download.error || download.message || (ds === "cancelled" ? "下载已取消。可自行重新下载。" : "尚未下载。点击「下载模型」后才会联网下载权重。加载与保存不会开始下载。")) + activeNote;
    $("live-download-status").dataset.level = ds === "failed" ? "error" : ds === "downloaded" ? "success" : "info";
    renderState();
  }
  async function updateModelStatus() {
    if (!loaded) return;
    const selected = selectedModel(), key = JSON.stringify(selected);
    try {
      const result = await request(`/api/live-model/status?backend=${encodeURIComponent(selected.backend)}&model=${encodeURIComponent(selected.model)}`);
      if (key !== JSON.stringify(selectedModel())) return;
      modelStatus = result; checkingModel = false; renderModelStatus();
    } catch (error) {
      if (key !== JSON.stringify(selectedModel())) return;
      checkingModel = true; renderState();
      throw error;
    }
  }
  function renderModels() {
    const query = $("live-model-search").value.trim().toLowerCase();
    const filtered = models.filter((model) => model.toLowerCase().includes(query));
    $("live-translation-model-list").replaceChildren(option(filtered.length ? "选择模型" : "没有匹配模型", ""), ...filtered.map((model) => option(model, model)));
    $("live-translation-model-list").value = filtered.includes($("live-translation-model").value) ? $("live-translation-model").value : "";
    $("live-model-search").hidden = $("live-translation-model-list").hidden = models.length === 0;
  }
  function setSelect(id, selected, label = String(selected)) {
    const field = $(id);
    if (![...field.children].some((item) => item.value === String(selected))) field.append(option(label, String(selected)));
    field.value = String(selected);
  }
  function accept(result) {
    const previousSelection = JSON.stringify(selectedModel());
    config = result; locked = Boolean(result.locked);
    setSelect("live-asr-model", `${result.backend}:${result.model}`, MODEL_LABELS[result.model] || result.model);
    if (previousSelection !== JSON.stringify(selectedModel())) { modelStatus = {}; checkingModel = true; }
    const profiles = result.providers || [], route = result.translation_route || {};
    $("live-translation-backend").value = result.translation_backend === "local" ? "local" : "cloud";
    localTranslation.select(result.local_translation_model || DEFAULT_LOCAL_TRANSLATION_MODEL);
    $("live-translation-provider").replaceChildren(option("请选择文字服务商连接", ""), ...profiles.map((item) => option(`${item.name}${item.enabled === false ? "（已停用）" : ""}`, item.id)));
    if (route.provider_id && !profiles.some((p) => p.id === route.provider_id)) $("live-translation-provider").append(option("原连接已删除，请重新选择", route.provider_id));
    $("live-translation-provider").value = route.provider_id || "";
    $("live-translation-model").value = route.model || ""; $("live-glossary").value = result.glossary || "";
    setSelect("live-segment-seconds", result.segment_seconds || 5, `${result.segment_seconds} 秒`);
    setSelect("live-silence-ms", result.silence_ms || 600, `${result.silence_ms} 毫秒`);
    loaded = true; baseline = JSON.stringify(value()); revision++; models = []; renderModels(); renderState();
  }
  async function load() {
    if (busy || fetching || localTranslation.busy) return;
    if (dirty() && !confirmDiscard("重新读取会放弃尚未保存的直播模型、翻译和术语修改。是否继续？")) return;
    busy = true; pendingAction = "load"; status("正在读取直播配置…"); renderState();
    try {
      accept(await request("/api/live-settings"));
      status(config.configured ? "直播设置已读取。准备模型后，可回到工作台选择直播标签页。" : "已从原翻译配置读取初始选择。保存后，直播设置独立管理，不会修改完整音频配置。");
      try { await updateModelStatus(); } catch (error) { status(`直播设置已读取；本机模型状态暂时无法读取：${error.message}`, "error"); }
      await localTranslation.refresh();
    }
    catch (error) { status(error.message, "error"); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function persist() {
    const result = await request("/api/live-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value()) });
    accept(result); status("直播配置已保存。完整音频识别、校对和翻译配置保留。", "success");
  }
  async function save(event) {
    event?.preventDefault(); if (!loaded || mutationBlocked()) return;
    busy = true; pendingAction = "save"; status("正在保存直播配置…"); renderState();
    try {
      await persist();
      try { await updateModelStatus(); } catch (error) { status(`直播配置已保存；本机模型状态暂时无法读取：${error.message}`, "error"); }
    } catch (error) { status(`${error.message} 当前修改仍保留，可重试保存。`, "error"); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function prepare() {
    if (!loaded || mutationBlocked()) return;
    if (!modelFilesReady()) { status("请先下载所选模型并等待文件校验完成，再保存并加载模型。此操作不会自动下载。", "error"); renderState(); return; }
    busy = true; pendingAction = "prepare"; status("正在保存选择并加载本机模型…"); renderState();
    let saved = false;
    try {
      await persist(); saved = true; modelStatus = { ...modelStatus, state: "loading" }; renderModelStatus();
      modelStatus = await request("/api/live-model/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selectedModel()) }, 120000);
      renderModelStatus(); status(modelStatus.state === "ready" ? "模型已加载并预热，可回到直播工作台开始。" : modelStatus.error || modelStatus.message || "模型正在准备，请查看状态。", modelStatus.state === "ready" ? "success" : "info");
    } catch (error) { status(saved ? `直播配置已保存；本机模型准备未完成：${error.message}` : `${error.message} 当前修改仍保留，可重试保存。`, "error"); try { await updateModelStatus(); } catch {} }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function downloadModel() {
    if (!loaded || mutationBlocked() || checkingModel || selectedModel().backend !== "mlx" || downloadState() === "downloaded" || modelStatus.state === "loading") return;
    const selected = selectedModel(), key = JSON.stringify(selected);
    busy = true; pendingAction = "download"; status("正在开始下载所选模型。当前配置修改仍保留，下载不会自动加载模型。"); renderState();
    try {
      const result = await request("/api/live-model/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...selected, consent: true }) });
      if (key !== JSON.stringify(selectedModel())) return;
      const download = result.download || result;
      modelStatus = { ...modelStatus, download, active_download: ["downloading", "verifying"].includes(download.state) ? { ...selected, ...download } : null };
      if (activeDownload()) locked = true;
      renderModelStatus();
      status(downloadState() === "downloaded" ? "模型文件已在本机。请自行保存并加载模型。" : "模型下载已开始，可查看进度或取消。当前配置修改仍保留。", "success");
    } catch (error) { status(`模型下载未能开始：${error.message} 当前配置修改仍保留。`, "error"); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function cancelDownload() {
    const active = activeDownload();
    if (!loaded || busy || localTranslation.busy || !active || active.backend !== "mlx") return;
    const target = { backend: active.backend, model: active.model };
    busy = true; pendingAction = "cancel-download"; renderState();
    try {
      const result = await request("/api/live-model/download/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(target) });
      const download = result.download || result, current = selectedModel();
      const forSelectedModel = target.backend === current.backend && target.model === current.model;
      modelStatus = { ...modelStatus, ...(forSelectedModel ? { download } : {}), active_download: ["downloading", "verifying"].includes(download.state) ? { ...target, ...download } : null }; renderModelStatus();
      status(activeDownload() ? "正在取消模型下载，请等待当前写入结束。" : `${MODEL_LABELS[target.model] || target.model} 下载已取消。当前配置修改仍保留。`);
      await refreshLock();
    } catch (error) { status(`取消下载未完成：${error.message}`, "error"); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function fetchModels() {
    const p = provider(); if (!loaded || mutationBlocked() || $("live-translation-backend").value === "local" || !p?.base_url || !p?.key_configured) return;
    const currentRevision = revision; fetching = true; renderState(); $("live-translation-model-status").textContent = "正在读取服务商返回的全部模型…"; $("live-translation-model-status").dataset.level = "info";
    try {
      const result = await request("/api/service-config/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider_id: p.id, base_url: p.base_url, text_api: p.text_api || "chat_completions", anthropic_auth: p.anthropic_auth || "api_key" }) });
      if (revision !== currentRevision || provider()?.id !== p.id) return;
      models = [...new Set((result.models || []).filter((model) => typeof model === "string" && model.trim()))].sort((a, b) => a.localeCompare(b)); renderModels();
      $("live-translation-model-status").textContent = models.length ? `已获取 ${models.length} 个模型，请自行选择；模型名称不代表一定支持文字翻译。` : "服务商未返回模型列表，可按文档手动填写模型。";
      $("live-translation-model-status").dataset.level = models.length ? "success" : "info";
    } catch (error) { $("live-translation-model-status").textContent = error.message + " 仍可手动填写模型。"; $("live-translation-model-status").dataset.level = "error"; }
    finally { fetching = false; renderState(); }
  }
  for (const id of controls) $(id).addEventListener(["live-translation-model", "live-glossary", "live-model-search"].includes(id) ? "input" : "change", async () => {
    if (!loaded || mutationBlocked()) return;
    if (feedback.level === "error") status("当前修改尚未保存，请确认后保存直播配置。");
    if (id === "live-translation-provider") { revision++; models = []; $("live-translation-model").value = ""; $("live-translation-model-status").textContent = ""; renderModels(); }
    if (id === "live-model-search") renderModels();
    if (id === "live-translation-model-list" && $(id).value) $("live-translation-model").value = $(id).value;
    if (id === "live-asr-model") { modelStatus = {}; checkingModel = true; renderModelStatus(); try { await updateModelStatus(); } catch (error) { $("live-model-status").textContent = error.message; } }
    renderState();
  });
  $("live-settings-form").addEventListener("submit", save); $("reload-live-settings").addEventListener("click", load);
  $("prepare-live-model").addEventListener("click", prepare); $("live-fetch-models").addEventListener("click", fetchModels);
  $("download-live-model").addEventListener("click", downloadModel); $("cancel-live-model-download").addEventListener("click", cancelDownload);
  async function refreshLock() {
    const currentRevision = revision;
    const result = await request("/api/live-settings");
    if (currentRevision !== revision) return;
    locked = Boolean(result.locked);
    $("live-settings-lock").textContent = activeDownload() || localTranslation.activeDownload ? "模型正在下载或校验，可取消下载；完成后再修改配置或加载模型。" : "正在录制、加载模型或补全字幕。当前场次使用固定配置，结束后再修改。";
    renderState();
  }
  async function poll() {
    if (stopped) return;
    try {
      await refreshLock();
      if (connectionLost) { connectionLost = false; status("本机服务连接已恢复。当前修改仍保留，可继续查看或保存直播配置。", "success"); }
      if (!busy && !localTranslation.busy) {
        try { await updateModelStatus(); }
        catch (error) { $("live-model-status").textContent = `本机模型状态暂时无法读取：${error.message}`; $("live-model-status").dataset.level = "error"; }
        await localTranslation.refresh();
      }
      renderState();
    }
    catch { connectionLost = true; locked = true; status("暂时无法连接本机服务。当前修改仍保留，连接恢复后可继续。", "error"); renderState(); }
    finally { if (!stopped) timer = setTimeout(poll, 4000); }
  }
  renderState();
  return { load, save, prepare, downloadModel, cancelDownload, updateModelStatus, fetchModels, refreshLock, localTranslation, get dirty() { return dirty(); }, startPolling() { stopped = false; poll(); }, stopPolling() { stopped = true; clearTimeout(timer); } };
}

// Bootstrap the live settings page.
const liveSettingsUI = createLiveSettingsUI();
liveSettingsUI.load(); liveSettingsUI.startPolling();
window.addEventListener("beforeunload", (event) => { if (liveSettingsUI.dirty) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener("pagehide", () => liveSettingsUI.stopPolling());
window.addEventListener("pageshow", (event) => { if (event.persisted) liveSettingsUI.startPolling(); });
