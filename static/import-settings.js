import { jsonRequest } from "./project-utils.js?v=ui-navigation-1";

export function createImportSettings({ onChange = () => {}, onError = () => {} } = {}) {
  const $ = (id) => document.getElementById(id);
  const fields = ["import-api-base-url", "import-api-key", "import-asr-model", "import-response-format", "clear-import-api-key"];
  let saved = null;
  let dirty = false;
  let busy = false;
  let locked = false;
  let managed = false;
  let models = [];
  let revision = 0;
  let operation = "";
  function setStatus(message, level = "info") {
    $("import-settings-status").textContent = message;
    $("import-settings-status").dataset.level = level;
  }
  function formatHelp() {
    const help = {
      verbose_json: "优先选择。需要语音服务支持此格式并返回分句时间戳，字幕可以与音频对齐。",
      json: "JSON 未返回时间戳时按文本估算，剪辑前需调整。支持时间戳的服务会保留实际时间。",
      srt: "语音服务直接返回带时间戳的 SRT。请确认服务与所选模型支持此响应格式。",
    };
    $("import-format-help").textContent = help[$("import-response-format").value] || "请按语音服务文档选择支持的格式。";
  }
  function renderControls() {
    const disabled = busy || locked || managed || !saved;
    for (const id of fields) $(id).disabled = disabled;
    $("import-api-key").disabled = disabled || $("clear-import-api-key").checked;
    $("save-import-settings").disabled = disabled;
    $("reload-import-settings").disabled = busy || locked;
    $("save-import-settings").textContent = operation === "save" ? "正在保存…" : "保存识别 API 设置";
    $("reload-import-settings").textContent = operation === "load" ? "正在读取…" : dirty ? "放弃修改并重新读取" : "重新读取设置";
    $("import-settings-form").setAttribute?.("aria-busy", String(busy));
    $("save-import-settings").setAttribute?.("aria-busy", String(operation === "save"));
    $("reload-import-settings").setAttribute?.("aria-busy", String(operation === "load"));
    $("fetch-import-models").disabled = disabled;
    $("import-model-search").disabled = disabled;
    $("import-model-list").disabled = disabled || !models.length;
    $("import-api-badge").textContent = !saved ? "尚未读取配置" : dirty ? "修改待保存" : saved.import_configuration_ready ? "配置已就绪" : "尚未配置完成";
    $("import-api-badge").dataset.stage = !saved || dirty || !saved.import_configuration_ready ? "queued" : "completed";
    formatHelp();
  }
  function notify() { renderControls(); onChange(); }
  function clearModels(message = "服务配置已改变，请重新获取模型；也可直接手动填写。") {
    models = []; revision += 1;
    $("import-model-search").value = ""; $("import-model-search").hidden = true;
    const option = document.createElement("option"); option.value = ""; option.textContent = "先获取模型列表，或在上方手动填写";
    $("import-model-list").replaceChildren(option);
    $("import-model-status").textContent = message;
    $("import-model-status").dataset.level = "info";
  }
  function markDirty(event) {
    dirty = true;
    if (["import-api-base-url", "import-api-key", "clear-import-api-key"].includes(event?.target?.id)) clearModels();
    if ($("clear-import-api-key").checked) $("import-api-key").value = "";
    setStatus("配置已修改，请先保存识别 API 设置，再开始转写。", "info");
    notify();
  }
  function draft() {
    const values = {
      import_transcription_base_url: $("import-api-base-url").value.trim(),
      import_transcription_model: $("import-asr-model").value.trim(),
      import_transcription_response_format: $("import-response-format").value,
      clear_import_transcription_api_key: $("clear-import-api-key").checked,
    };
    const key = $("import-api-key").value.trim();
    if (key && !values.clear_import_transcription_api_key) values.import_transcription_api_key = key;
    return values;
  }
  function fill(result) {
    saved = result;
    managed = Boolean(result.service_profiles_enabled);
    const legacyCard = $("import-settings-form").closest?.(".import-cloud-settings");
    if (legacyCard) legacyCard.hidden = managed;
    $("import-api-base-url").value = result.import_transcription_base_url || "";
    $("import-api-key").value = "";
    $("clear-import-api-key").checked = false;
    $("import-asr-model").value = result.import_transcription_model || "";
    $("import-response-format").value = ["verbose_json", "json", "srt"].includes(result.import_transcription_response_format) ? result.import_transcription_response_format : "verbose_json";
    $("import-effective-base").textContent = result.import_effective_base_url ? `已保存的实际识别地址：${result.import_effective_base_url}${result.import_transcription_base_url ? "" : "（继承共用地址）"}` : "尚未配置识别地址；填写独立地址，或先在工作台填写共用地址。";
    $("import-key-state").textContent = result.import_transcription_has_own_key ? "已保存独立语音密钥；留空保留，密钥不会回显。" : result.import_transcription_key_configured ? "当前使用工作台共用密钥；填写后可改用独立语音密钥。" : "尚未配置密钥；请填写独立语音密钥，或在工作台配置共用密钥。";
    dirty = false;
  }
  function renderModels() {
    const query = $("import-model-search").value.trim().toLowerCase();
    const filtered = models.filter((model) => model.toLowerCase().includes(query));
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = filtered.length ? "选择一个语音识别模型" : "没有匹配的模型，可在上方手动填写";
    const options = filtered.map((model) => { const option = document.createElement("option"); option.value = model; option.textContent = model; return option; });
    $("import-model-list").replaceChildren(placeholder, ...options);
    $("import-model-list").value = filtered.includes($("import-asr-model").value.trim()) ? $("import-asr-model").value.trim() : "";
    $("import-model-status").textContent = `服务返回 ${models.length} 个模型${query ? `，当前匹配 ${filtered.length} 个` : ""}。请选择支持 /audio/transcriptions 的语音模型；列表本身不证明模型具备语音能力。`;
  }
  async function load() {
    if (busy || locked) return;
    busy = true; operation = "load"; setStatus("正在读取完整音频识别 API 配置…"); notify();
    try {
      const result = await jsonRequest("/api/import-settings");
      clearModels("获取语音服务返回的全部模型，或按平台文档手动填写支持语音识别的模型。");
      fill(result); setStatus(managed ? "当前使用统一流程配置，请点击「服务商与处理流程」修改识别、校对与翻译服务。" : saved.import_configuration_ready ? "识别与翻译配置已就绪。选择音频后即可开始。" : "请填写语音识别地址、密钥与模型并保存；中文校正与翻译沿用工作台已保存的翻译服务。"); }
    catch (error) { setStatus(error.message, "error"); onError(error.message); }
    finally { busy = false; operation = ""; notify(); }
  }
  async function save(event) {
    event?.preventDefault();
    if (busy || locked || managed || !saved) return;
    const values = draft();
    if (!values.import_transcription_model) { setStatus("请选择或填写该服务实际支持的语音识别模型。", "error"); return; }
    busy = true; operation = "save"; setStatus("正在保存识别 API 设置…"); notify();
    try {
      fill(await jsonRequest("/api/import-settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) }));
      setStatus(saved.import_configuration_ready ? "已保存识别 API 设置，选择音频后即可开始。" : !saved.translation_key_configured ? "已保存识别设置。请在工作台配置中文翻译 API 后再开始。" : "已保存设置，语音识别地址、密钥或模型尚未配置完成。", saved.import_configuration_ready ? "success" : "info");
    } catch (error) { setStatus(error.message, "error"); }
    finally { busy = false; operation = ""; notify(); }
  }
  async function fetchModels() {
    if (busy || locked || managed || !saved) return;
    const values = draft();
    if (!values.import_transcription_api_key && !saved.import_transcription_key_configured) { $("import-model-status").textContent = "请先填写语音 API 密钥，再获取模型；无需先保存。"; $("import-model-status").dataset.level = "error"; return; }
    const requestRevision = ++revision;
    busy = true; operation = "models"; $("fetch-import-models").textContent = "正在获取…"; $("fetch-import-models").setAttribute?.("aria-busy", "true"); $("import-model-status").textContent = "正在获取语音服务返回的全部模型…"; $("import-model-status").dataset.level = "info"; notify();
    try {
      const body = { role: "import_transcription", import_transcription_base_url: values.import_transcription_base_url, clear_import_transcription_api_key: values.clear_import_transcription_api_key };
      if (values.import_transcription_api_key) body.import_transcription_api_key = values.import_transcription_api_key;
      const response = await jsonRequest("/api/settings/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (requestRevision !== revision) return;
      models = [...new Set((response.models || []).filter((model) => typeof model === "string" && model.trim()))].sort((a, b) => a.localeCompare(b));
      $("import-model-search").hidden = !models.length;
      renderModels();
      if (!models.length) $("import-model-status").textContent = "服务没有返回可选模型。请按平台文档在上方手动填写支持语音识别的模型。";
    } catch (error) { if (requestRevision === revision) { clearModels(error.message); $("import-model-status").dataset.level = "error"; } }
    finally { busy = false; operation = ""; $("fetch-import-models").textContent = "获取模型"; $("fetch-import-models").setAttribute?.("aria-busy", "false"); notify(); }
  }
  for (const id of fields) $(id).addEventListener(id === "clear-import-api-key" || id === "import-response-format" ? "change" : "input", markDirty);
  $("import-settings-form").addEventListener("submit", save);
  $("reload-import-settings").addEventListener("click", () => {
    if (dirty && typeof window !== "undefined" && !window.confirm("重新读取会放弃本页尚未保存的识别 API 修改。确定重新读取吗？")) return;
    return load();
  });
  $("fetch-import-models").addEventListener("click", fetchModels);
  $("import-model-search").addEventListener("input", renderModels);
  $("import-model-list").addEventListener("change", () => { if (!$("import-model-list").value) return; $("import-asr-model").value = $("import-model-list").value; markDirty(); });
  return {
    load,
    get ready() { return Boolean(saved?.import_configuration_ready && !dirty && !busy); },
    get dirty() { return dirty; },
    get busy() { return busy; },
    get loaded() { return Boolean(saved); },
    get configured() { return Boolean(saved?.import_configuration_ready); },
    setLocked(value) { locked = Boolean(value); renderControls(); },
  };
}
