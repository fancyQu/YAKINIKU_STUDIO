import { jsonRequest } from "./project-utils.js?v=ui-navigation-1";

const TASKS = ["file_asr", "correction", "translation"];
const SECTION_TASKS = { asr: ["file_asr"], text: ["correction", "translation"] };
const SECTION_NAMES = { asr: "ASR 音频识别", text: "校对与翻译" };
const TEXT_APIS = ["chat_completions", "responses", "anthropic"];
const ANTHROPIC_AUTHS = ["api_key", "bearer"];
const SPEECH_APIS = ["none", "openai", "minimax"];
const taskId = (task) => task.replaceAll("_", "-");
const taskSection = (task) => task === "file_asr" ? "asr" : "text";
const copy = (value) => JSON.parse(JSON.stringify(value));
function sectionFromURL() {
  try { const section = new URLSearchParams(location.search).get("section"); return SECTION_TASKS[section] ? section : ""; }
  catch { return ""; }
}

export function createServiceConfigUI({ request = jsonRequest, initialSection = sectionFromURL(), confirmDiscard = (message) => confirm(message) } = {}) {
  const $ = (id) => document.getElementById(id);
  let currentSection = SECTION_TASKS[initialSection] ? initialSection : "";
  let serverConfig = { providers: [], routes: {} };
  let drafts = { asr: null, text: null };
  let presets = [];
  let ready = {};
  let loaded = false;
  let busy = false;
  let pendingAction = "";
  let feedback = { message: "", level: "info" };
  let configurationGeneration = 0;
  let connectionLost = false;
  let locked = false;
  let timer = null;
  let stopped = false;
  const models = new Map();
  const versions = new Map();
  const fetching = new Set();
  const editorFields = ["provider-name", "provider-vendor", "provider-base-url", "provider-api-key", "provider-clear-key", "provider-text-api", "provider-anthropic-auth", "provider-speech-api", "provider-enabled"];
  const draftFor = (section = currentSection) => drafts[section];
  const selected = () => draftFor()?.providers.find((provider) => provider.id === draftFor().selectedId);
  const presetFor = (provider) => presets.find((preset) => preset.id === provider?.vendor);
  const addressFor = (provider) => {
    const preset = presetFor(provider);
    return currentSection === "asr" && !["openai", "minimax"].includes(preset?.speech_api) ? "" : preset?.suggested_base_url || "";
  };
  const cacheKey = (section, providerId) => section + ":" + providerId;
  const hasSavedProvider = (id) => serverConfig.providers.some((provider) => provider.id === id);
  const routeValue = (task, route = {}) => ({ provider_id: route.provider_id || "", model: (route.model || "").trim(), ...(task === "file_asr" ? { response_format: route.response_format || "verbose_json" } : {}) });

  function status(message, level = "info") {
    feedback = { message, level };
    $("service-status").textContent = message; $("service-status").dataset.level = level;
    $("service-save-help").textContent = message;
    $("service-save-help").dataset.level = level;
  }
  function saveFailure(error) {
    status(error.status === 409 ? `${error.message} 当前草稿仍保留；请先保留需要的修改，再点击「重新读取本模块」。` : error.message, "error");
  }
  function revisionFor(section) { return draftFor(section)?.baseline.revision || ""; }
  function serializeProvider(provider) {
    return {
      id: provider.id, name: provider.name.trim(), vendor: provider.vendor, scope: provider.scope,
      base_url: provider.base_url.trim(), text_api: provider.text_api, anthropic_auth: provider.anthropic_auth,
      speech_api: provider.speech_api, enabled: provider.enabled !== false, clear_api_key: Boolean(provider.clear_api_key),
      ...(provider.api_key?.trim() && !provider.clear_api_key ? { api_key: provider.api_key.trim() } : {}),
    };
  }
  function providerDirty(provider, draft) {
    const baseline = draft?.baseline.providers.find((item) => item.id === provider.id);
    return !baseline || JSON.stringify(serializeProvider(provider)) !== JSON.stringify(serializeProvider(baseline));
  }
  function routeDirty(task, draft = draftFor(taskSection(task))) {
    return Boolean(draft && JSON.stringify(routeValue(task, draft.routes[task])) !== JSON.stringify(routeValue(task, draft.baseline.routes[task])));
  }
  function sectionDirty(section) {
    const draft = draftFor(section); if (!draft) return false;
    return draft.providers.length !== draft.baseline.providers.length || draft.providers.some((provider) => providerDirty(provider, draft)) || SECTION_TASKS[section].some((task) => routeDirty(task, draft));
  }
  function taskDirty(task) {
    const draft = draftFor(taskSection(task));
    const provider = draft?.providers.find((item) => item.id === draft.routes[task]?.provider_id);
    return routeDirty(task, draft) || Boolean(provider && providerDirty(provider, draft));
  }
  function sharedUsedElsewhere(provider, section = currentSection) {
    if (provider?.scope !== "shared") return false;
    const other = section === "asr" ? "text" : "asr";
    return SECTION_TASKS[other].some((task) => serverConfig.routes[task]?.provider_id === provider.id || draftFor(other)?.routes[task]?.provider_id === provider.id);
  }
  function normalize(config) {
    const routes = Object.fromEntries(TASKS.map((task) => [task, routeValue(task, config.routes?.[task])]));
    const ids = new Set((config.providers || []).map((provider) => provider.id));
    const providers = [];
    for (const provider of config.providers || []) {
      const asrUsed = routes.file_asr.provider_id === provider.id;
      const textUsed = ["correction", "translation"].some((task) => routes[task].provider_id === provider.id);
      const inferredScope = asrUsed && textUsed ? "shared" : asrUsed ? "asr" : textUsed ? "text" : provider.speech_api && provider.speech_api !== "none" ? "asr" : "text";
      const normalized = { ...provider, scope: ["asr", "text", "shared"].includes(provider.scope) ? provider.scope : inferredScope,
        name: provider.name || "未命名连接", vendor: provider.vendor || "custom", base_url: provider.base_url || "",
        text_api: TEXT_APIS.includes(provider.text_api) ? provider.text_api : "chat_completions",
        anthropic_auth: ANTHROPIC_AUTHS.includes(provider.anthropic_auth) ? provider.anthropic_auth : "api_key",
        speech_api: SPEECH_APIS.includes(provider.speech_api) ? provider.speech_api : "none", api_key: "", clear_api_key: false };
      if (normalized.scope !== "shared") { providers.push(normalized); continue; }
      // Old installations reused one connection for both modules. Split that
      // reference in this view; saving either module can never change the other.
      let suffix = "-asr", cloneId = provider.id.slice(0, 80 - suffix.length) + suffix, index = 2;
      while (ids.has(cloneId)) { suffix = "-asr-" + index++; cloneId = provider.id.slice(0, 80 - suffix.length) + suffix; }
      ids.add(cloneId);
      providers.push({ ...normalized, scope: "text" });
      providers.push({ ...normalized, id: cloneId, scope: "asr", name: (normalized.name + " · 音频识别").slice(0, 80) });
      if (routes.file_asr.provider_id === provider.id) routes.file_asr.provider_id = cloneId;
    }
    return { ...config, providers, routes };
  }
  function snapshot(section, config) {
    return { providers: copy(config.providers.filter((provider) => provider.scope === section)),
      routes: Object.fromEntries(SECTION_TASKS[section].map((task) => [task, copy(config.routes[task])])),
      revision: config.section_revisions?.[section] || "" };
  }
  function mergeDraft(section, config, { reset = false, providerId = "" } = {}) {
    const incoming = snapshot(section, config), previous = draftFor(section);
    if (!previous || reset) {
      drafts[section] = { ...copy(incoming), baseline: copy(incoming), selectedId: incoming.providers.some((provider) => provider.id === previous?.selectedId) ? previous.selectedId : incoming.providers[0]?.id || "" };
      return;
    }
    // Rebase clean fields while retaining every unsaved connection, key and task choice.
    const remaining = new Map(incoming.providers.map((provider) => [provider.id, provider]));
    const nextProviders = [];
    for (const provider of previous.providers) {
      const saved = remaining.get(provider.id);
      if (provider.id === providerId && saved) nextProviders.push(copy(saved));
      else if (providerDirty(provider, previous)) nextProviders.push(provider);
      else if (saved) nextProviders.push(copy(saved));
      remaining.delete(provider.id);
    }
    for (const provider of remaining.values()) {
      if (!previous.baseline.providers.some((item) => item.id === provider.id)) nextProviders.push(copy(provider));
    }
    const nextRoutes = Object.fromEntries(SECTION_TASKS[section].map((task) => [task, routeDirty(task, previous) ? previous.routes[task] : copy(incoming.routes[task])]));
    // A response for another module must not silently authorize stale drafts
    // against a newer revision. An explicit save/reload updates this baseline.
    if (!providerId && sectionDirty(section)) incoming.revision = previous.baseline.revision;
    drafts[section] = { providers: nextProviders, routes: nextRoutes, baseline: copy(incoming), selectedId: nextProviders.some((provider) => provider.id === previous.selectedId) ? previous.selectedId : nextProviders[0]?.id || "" };
  }
  function acceptConfig(raw, { resetSection = "", resetAll = false, savedProvider = "", providerSection = "" } = {}) {
    const config = normalize(raw);
    const changedIds = new Set(config.providers.filter((provider) => {
      const previous = serverConfig.providers.find((item) => item.id === provider.id);
      return !previous || JSON.stringify(serializeProvider(provider)) !== JSON.stringify(serializeProvider(previous));
    }).map((provider) => provider.id));
    if (savedProvider) changedIds.add(savedProvider);
    if (resetSection) for (const provider of draftFor(resetSection)?.providers || []) if (providerDirty(provider, draftFor(resetSection))) changedIds.add(provider.id);
    for (const section of ["asr", "text"]) mergeDraft(section, config, { reset: resetAll || resetSection === section, providerId: providerSection === section ? savedProvider : "" });
    serverConfig = config; presets = config.presets || []; ready = config.ready || {}; locked = Boolean(config.locked); loaded = true;
    configurationGeneration++;
    for (const section of ["asr", "text"]) for (const provider of draftFor(section).providers) if (changedIds.has(provider.id)) invalidate(section, provider.id);
    $("service-live-note").textContent = "直播音频：独立本机模型设置";
    render();
  }
  function invalidate(section, providerId) {
    const key = cacheKey(section, providerId); versions.set(key, (versions.get(key) || 0) + 1); models.delete(key);
    for (const task of SECTION_TASKS[section]) if (draftFor(section)?.routes[task]?.provider_id === providerId) renderModels(task);
  }
  function renderState() {
    const disabled = busy || locked || !loaded || !currentSection;
    const provider = selected();
    $("service-lock-note").hidden = !locked;
    $("add-provider").disabled = disabled;
    $("remove-provider").disabled = disabled || !provider || sharedUsedElsewhere(provider);
    $("save-provider").disabled = disabled || !provider || fetching.size > 0;
    $("save-service-config").disabled = disabled || fetching.size > 0;
    $("save-section-top").disabled = disabled || fetching.size > 0;
    $("reload-service-config").disabled = disabled || fetching.size > 0;
    $("open-asr-settings").disabled = busy;
    $("open-text-settings").disabled = busy;
    $("back-service-overview").disabled = busy;
    const saveLabel = currentSection === "asr" ? "保存 ASR 配置" : "保存校对与翻译配置";
    $("save-provider").textContent = pendingAction === "provider" ? "正在保存服务商…" : "保存服务商";
    for (const id of ["save-service-config", "save-section-top"]) {
      $(id).textContent = pendingAction === "section" ? "正在保存配置…" : saveLabel;
      $(id).setAttribute("aria-busy", String(pendingAction === "section"));
    }
    $("save-provider").setAttribute("aria-busy", String(pendingAction === "provider"));
    $("reload-service-config").textContent = pendingAction === "load" ? "正在读取…" : "重新读取本模块";
    $("reload-service-config").setAttribute("aria-busy", String(pendingAction === "load"));
    for (const id of editorFields) $(id).disabled = disabled || !provider;
    $("provider-api-key").disabled = disabled || !provider || Boolean(provider?.clear_api_key);
    $("provider-clear-key").disabled = disabled || !provider || Boolean(provider?.key_from_environment);
    $("use-provider-address").disabled = disabled || !addressFor(provider);
    $("provider-save-state").textContent = !provider ? "保存服务商后，再选择本模块需要的模型。" : providerDirty(provider, draftFor()) ? "当前服务商有未保存修改" : "服务商已保存 · 模型选择请另存本模块配置";
    $("provider-save-state").dataset.dirty = String(Boolean(provider && providerDirty(provider, draftFor())));
    for (const task of TASKS) {
      const id = taskId(task), section = taskSection(task), draft = draftFor(section);
      const targetProvider = draft?.providers.find((item) => item.id === draft.routes[task]?.provider_id);
      const key = cacheKey(section, targetProvider?.id);
      const taskDisabled = disabled || currentSection !== section;
      for (const suffix of ["provider", "model", "model-search", "model-list"]) $(id + "-" + suffix).disabled = taskDisabled;
      $(id + "-fetch-models").disabled = taskDisabled || !targetProvider || fetching.has(key);
      $(id + "-fetch-models").textContent = fetching.has(key) ? "正在获取…" : "获取全部模型";
      $(id + "-fetch-models").setAttribute("aria-busy", String(fetching.has(key)));
      const pending = taskDirty(task), isReady = !pending && Boolean(ready[task]);
      $(id + "-ready").textContent = pending ? "待保存" : isReady ? "已配置" : "未配置";
      $(id + "-ready").dataset.ready = String(isReady);
    }
    $("file-asr-format").disabled = disabled || currentSection !== "asr";
    for (const section of ["asr", "text"]) {
      const count = draftFor(section)?.providers.length || 0;
      $("overview-" + section + "-status").textContent = !loaded ? "正在读取配置" : sectionDirty(section) ? `${count} 个连接 · 有未保存修改` : `${count} 个连接 · ${SECTION_TASKS[section].every((task) => ready[task]) ? "已配置" : "待完善配置"}`;
    }
    $("service-save-state").textContent = busy ? pendingAction === "load" ? "正在读取配置…" : "正在保存，请稍候…" : !loaded ? "尚未读取配置" : locked ? "当前任务进行中 · 配置已锁定" : feedback.level === "error" ? "操作未完成 · 当前修改仍保留" : sectionDirty(currentSection) ? "本模块有未保存的修改" : "本模块配置已保存";
    $("service-save-state").dataset.level = feedback.level;
  }
  function availablePresets() {
    return currentSection === "asr" ? presets.filter((preset) => preset.id === "custom" || ["openai", "minimax"].includes(preset.speech_api)) : presets;
  }
  function renderProviders() {
    const draft = draftFor();
    const nodes = (draft?.providers || []).map((provider) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "provider-item";
      button.setAttribute("aria-pressed", String(provider.id === draft.selectedId)); button.dataset.providerId = provider.id;
      const avatar = document.createElement("span"); avatar.className = "provider-item-avatar"; avatar.textContent = (provider.name || "API").trim().slice(0, 1).toUpperCase();
      const content = document.createElement("span"); content.className = "provider-item-content";
      const name = document.createElement("span"); name.className = "provider-item-name"; name.textContent = provider.name || "未命名连接";
      const detail = document.createElement("span"); detail.className = "provider-item-detail";
      const secretState = provider.enabled === false ? "已停用" : provider.api_key || provider.key_configured ? "密钥已配置" : "待配置密钥";
      detail.textContent = `${provider.scope === "shared" ? "旧版共用连接" : presetFor(provider)?.name || "自定义"} · ${providerDirty(provider, draft) ? "待保存" : secretState}`;
      content.append(name, detail); button.append(avatar, content);
      button.addEventListener("click", () => { if (busy) return; draft.selectedId = provider.id; renderProviders(); renderEditor(); renderState(); });
      return button;
    });
    if (!nodes.length) { const empty = document.createElement("p"); empty.className = "project-empty"; empty.textContent = "本模块还没有服务商连接"; nodes.push(empty); }
    $("provider-list").replaceChildren(...nodes); $("provider-count").textContent = `${draft?.providers.length || 0} 个连接`;
  }
  function renderEndpoints() {
    const provider = selected(); if (!provider) return;
    const isText = currentSection === "text", base = provider.base_url.trim().replace(/\/+$/, "");
    const textPath = provider.text_api === "anthropic" ? (base.endsWith("/v1") ? "/messages" : "/v1/messages") : provider.text_api === "responses" ? "/responses" : "/chat/completions";
    $("provider-text-endpoint").textContent = base ? base + textPath : "填写 Base URL 后显示";
    $("provider-speech-endpoint").textContent = provider.speech_api === "none" ? "请选择音频识别协议" : base ? base + (provider.speech_api === "minimax" ? "/speech_to_text" : "/audio/transcriptions") : "填写 Base URL 后显示";
    for (const id of ["provider-text-api-field", "provider-text-endpoint-label", "provider-text-endpoint"]) $(id).hidden = !isText;
    for (const id of ["provider-speech-api-field", "provider-speech-endpoint-label", "provider-speech-endpoint"]) $(id).hidden = isText;
    $("provider-anthropic-auth-field").hidden = !isText || provider.text_api !== "anthropic";
    $("provider-protocol-help").textContent = !isText ? provider.speech_api === "minimax" ? "使用 MiniMax 原生语音识别接口。Anthropic 是文字协议，不用于音频转写。" : provider.speech_api === "none" ? "这个连接尚未选择音频识别协议。请按服务商文档选择 MiniMax 原生语音识别或 OpenAI 兼容音频转写。" : "使用 /audio/transcriptions 转写完整音频。请选择服务商实际支持的语音识别模型。" : provider.text_api === "anthropic" ? "Anthropic Messages 用于文字校对与翻译。MiniMax 使用 Bearer 鉴权，Anthropic 官方使用 x-api-key；地址和鉴权方式请以所用服务文档为准。" : "此连接用于文字校对与翻译。MiniMax 文字服务同时提供 OpenAI 与 Anthropic 兼容接口，按所用服务文档选择。";
    const preset = presetFor(provider);
    $("provider-preset-help").textContent = !isText && preset?.speech_api === "none" ? "当前保存的预设用于文字服务。请重新选择语音服务预设与音频识别协议，已有地址与密钥会保留。" : isText && provider.vendor === "minimax" ? "MiniMax 的 OpenAI 兼容文字接口，用于日语校对与中文翻译。音频识别在 ASR 模块独立配置。" : preset?.help || "预设只选择接口协议。支持的模型和费用以服务商文档为准。";
    const docs = $("provider-docs");
    const presetDocs = isText && provider.vendor === "minimax" ? "https://platform.minimax.io/docs/api-reference/text-openai-api.md" : preset?.docs_url;
    const docsURL = typeof presetDocs === "string" && /^https:\/\//i.test(presetDocs) && (isText || preset.speech_api !== "none") ? presetDocs : "";
    docs.hidden = !docsURL; if (docsURL) docs.href = docsURL;
  }
  function renderEditor() {
    const provider = selected();
    $("provider-fields").hidden = !provider; $("provider-empty").hidden = Boolean(provider);
    $("provider-editor-heading").textContent = provider?.name || "服务商设置";
    if (!provider) { for (const id of editorFields) { $(id).value = ""; $(id).checked = false; } return; }
    const choices = [...availablePresets()];
    if (!choices.some((item) => item.id === provider.vendor)) choices.push({ id: provider.vendor, name: currentSection === "asr" ? "当前旧预设（请选择语音服务）" : provider.vendor || "自定义服务商" });
    $("provider-vendor").replaceChildren(...choices.map((item) => { const option = document.createElement("option"); option.value = item.id; option.textContent = currentSection === "text" && item.id === "minimax" ? "MiniMax · OpenAI 文字" : item.name; return option; }));
    $("provider-name").value = provider.name; $("provider-vendor").value = provider.vendor; $("provider-base-url").value = provider.base_url;
    $("provider-api-key").value = provider.api_key || ""; $("provider-clear-key").checked = Boolean(provider.clear_api_key);
    $("provider-text-api").value = provider.text_api; $("provider-anthropic-auth").value = provider.anthropic_auth;
    $("provider-speech-api").value = provider.speech_api; $("provider-enabled").checked = provider.enabled !== false;
    $("provider-key-state").textContent = provider.key_from_environment ? "此连接使用本机环境变量中的密钥，页面不会回显。" : provider.key_configured ? "已保存密钥。留空保留；更换服务域名时请重新填写对应密钥。" : "尚未保存密钥。密钥只保存在本机服务，不会返回浏览器。";
    $("provider-shared-note").hidden = provider.scope !== "shared";
    $("provider-shared-note").textContent = sharedUsedElsewhere(provider) ? "旧版共用连接：另一个模块仍在使用，当前不能移除。保存服务商会更新这一个共用连接；建议分别新增专用连接。" : "旧版共用连接：也可能显示在另一个模块中。建议分别添加语音与文字连接，便于独立管理。";
    renderEndpoints();
  }
  function renderModels(task) {
    const id = taskId(task), section = taskSection(task), draft = draftFor(section), route = draft?.routes[task] || {};
    const provider = draft?.providers.find((item) => item.id === route.provider_id);
    const list = models.get(cacheKey(section, provider?.id)) || [];
    const query = $(id + "-model-search").value.trim().toLowerCase(), filtered = list.filter((model) => model.toLowerCase().includes(query));
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = filtered.length ? "选择模型填入上方输入框" : "没有匹配项，可手动填写模型";
    const choices = filtered.map((model) => { const option = document.createElement("option"); option.value = model; option.textContent = model; return option; });
    $(id + "-model-list").replaceChildren(placeholder, ...choices); $(id + "-model-list").value = filtered.includes(route.model) ? route.model : "";
    $(id + "-model-list").hidden = !list.length; $(id + "-model-search").hidden = !list.length;
    $(id + "-model-status").dataset.level = "info";
    $(id + "-model-status").textContent = list.length ? `服务返回 ${list.length} 个模型${query ? `，匹配 ${filtered.length} 个` : ""}。完整展示，模型能力以服务商文档为准。` : "可获取服务返回的全部模型，也可按文档手动填写。";
    const preset = presetFor(provider);
    $(id + "-model-hint").textContent = task === "file_asr" && provider?.speech_api === "minimax" ? `MiniMax 原生识别${preset?.asr_model_hint ? `模型：${preset.asr_model_hint}` : "支持手动填写模型"}。模型列表可能只含文字模型，请按文档填写语音模型。` : task === "file_asr" && provider?.speech_api === "none" ? "请选择音频识别协议。文字模型不能代替语音识别。" : task === "file_asr" ? "请选择实际支持日语音频转写的语音模型。" : "选择该服务实际支持的文字模型。校对与翻译可分别选用模型。";
  }
  function renderWorkflow() {
    for (const task of TASKS) {
      const id = taskId(task), draft = draftFor(taskSection(task)), route = draft?.routes[task] || {};
      $(id + "-step").hidden = taskSection(task) !== currentSection;
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "选择本模块的服务商连接";
      const choices = (draft?.providers || []).map((provider) => {
        const option = document.createElement("option"); option.value = provider.id;
        option.disabled = provider.enabled === false || task === "file_asr" && provider.speech_api === "none";
        option.textContent = provider.name + (provider.enabled === false ? "（已停用）" : task === "file_asr" && provider.speech_api === "none" ? "（请选择音频识别协议）" : ""); return option;
      });
      $(id + "-provider").replaceChildren(placeholder, ...choices); $(id + "-provider").value = route.provider_id || "";
      $(id + "-model").value = route.model || ""; renderModels(task);
    }
    $("file-asr-format").value = draftFor("asr")?.routes.file_asr.response_format || "verbose_json";
  }
  function render() {
    const isASR = currentSection === "asr";
    $("service-hero").hidden = Boolean(currentSection);
    $("service-overview").hidden = Boolean(currentSection); $("service-section").hidden = !currentSection;
    $("service-section-heading").textContent = SECTION_NAMES[currentSection] || "API 配置";
    $("service-section-description").textContent = isASR ? "配置完整音频的语音识别服务，再选择语音模型。" : "配置文字服务，再分别选择日语校对与中文翻译模型。";
    $("service-section-step").textContent = isASR ? "AUDIO → 日语原文" : "日语校对 → 中文翻译";
    $("provider-sidebar-note").textContent = isASR ? "这里只管理音频识别服务商。直播继续使用本机识别。" : "这里只管理文字服务商。校对与翻译可以复用一个连接，也可以分别选择。";
    $("workflow-heading").textContent = isASR ? "选择语音识别模型" : "先校对，再翻译";
    $("workflow-order-note").textContent = isASR ? "完整音频 → 日语原文与时间轴" : "01 日语校对 → 02 中文翻译，按顺序执行";
    $("workflow-grid").dataset.section = currentSection;
    const saveLabel = isASR ? "保存 ASR 配置" : "保存校对与翻译配置";
    $("save-service-config").textContent = saveLabel; $("save-section-top").textContent = saveLabel;
    $("service-save-help").textContent = isASR ? "保存本模块的语音服务商、识别模型与时间戳格式。文字模块草稿保留。" : "保存本模块的文字服务商、校对模型与翻译模型。ASR 模块草稿保留。";
    renderProviders(); renderEditor(); renderWorkflow(); renderState();
  }
  function openSection(section = "", { historyMode = "push" } = {}) {
    if (section && !SECTION_TASKS[section]) return;
    const changed = section !== currentSection;
    currentSection = section; render();
    try {
      const url = new URL(location.href);
      if (section) url.searchParams.set("section", section); else url.searchParams.delete("section");
      if (historyMode === "push" && changed) {
        history.pushState(null, "", url.pathname + url.search + url.hash);
        if (typeof window !== "undefined") window.dispatchEvent(new Event("yakiniku:navigation-change"));
      }
    } catch {}
    status(section ? `${SECTION_NAMES[section]}：服务商可单独保存，模型与处理步骤使用本模块的配置保存按钮。` : "选择一个模块进入配置。切换模块会保留本页尚未保存的草稿。");
  }
  async function load() {
    if (busy || loaded && locked || fetching.size) return;
    const targetSection = currentSection;
    if (loaded && targetSection && sectionDirty(targetSection) && !confirmDiscard(`重新读取会放弃${SECTION_NAMES[targetSection]}中尚未保存的修改，包括新填写的密钥。另一个模块的草稿会保留。是否继续？`)) return;
    busy = true; pendingAction = "load"; status("正在读取本模块配置…"); renderState();
    try {
      acceptConfig(await request("/api/service-config"), { resetAll: !loaded, resetSection: targetSection });
      if (targetSection) for (const provider of draftFor(targetSection).providers) invalidate(targetSection, provider.id);
      status(targetSection ? `已重新读取${SECTION_NAMES[targetSection]}配置。另一个模块的未保存草稿仍保留。` : "选择 ASR 音频识别或校对与翻译，分别配置所用服务。");
    } catch (error) { status(error.message, "error"); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function saveProvider() {
    const section = currentSection, provider = selected();
    if (!loaded || busy || locked || fetching.size || !provider || !section) return;
    busy = true; pendingAction = "provider"; status("正在保存当前服务商…"); renderState();
    try {
      const revision = revisionFor(section);
      const response = await request("/api/service-config/provider", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...serializeProvider(provider), section, ...(revision ? { expected_revision: revision } : {}) }) });
      acceptConfig(response, { savedProvider: provider.id, providerSection: section });
      status("当前服务商已保存。尚未保存的模型选择和其他服务商草稿仍保留。", "success");
    } catch (error) { saveFailure(error); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function save(event) {
    event?.preventDefault();
    const section = currentSection, draft = draftFor(section);
    if (!loaded || busy || locked || fetching.size || !draft) return;
    const revision = revisionFor(section);
    const payload = { providers: draft.providers.map(serializeProvider), routes: Object.fromEntries(SECTION_TASKS[section].map((task) => [task, routeValue(task, draft.routes[task])])), ...(revision ? { expected_revision: revision } : {}) };
    busy = true; pendingAction = "section"; status(`正在保存${SECTION_NAMES[section]}配置…`); renderState();
    try {
      const response = await request("/api/service-config/section/" + section, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      acceptConfig(response, { resetSection: section });
      status(`${SECTION_NAMES[section]}配置已保存。${SECTION_TASKS[section].every((task) => ready[task]) ? "本模块已配置完成。" : "尚未配置的服务或模型可稍后补充。"}另一个模块的草稿仍保留。`, "success");
    } catch (error) { saveFailure(error); }
    finally { busy = false; pendingAction = ""; renderState(); }
  }
  async function fetchModels(task) {
    const section = taskSection(task), draft = draftFor(section);
    if (busy || locked || !loaded || currentSection !== section || !draft) return;
    const provider = draft.providers.find((item) => item.id === draft.routes[task].provider_id);
    const target = $(taskId(task) + "-model-status"), key = cacheKey(section, provider?.id);
    if (!provider || fetching.has(key)) return;
    if (!provider.base_url.trim() || !provider.api_key?.trim() && (!provider.key_configured || provider.clear_api_key)) {
      target.textContent = "请先填写该连接的 Base URL 和 API Key，再获取模型；无需先保存。"; target.dataset.level = "error"; return;
    }
    const providerId = provider.id, version = versions.get(key) || 0;
    fetching.add(key); renderState(); target.textContent = "正在读取服务返回的全部模型…";
    try {
      const body = { base_url: provider.base_url.trim(), text_api: section === "asr" ? "chat_completions" : provider.text_api, anthropic_auth: provider.anthropic_auth, clear_api_key: Boolean(provider.clear_api_key), ...(hasSavedProvider(providerId) ? { provider_id: providerId } : {}), ...(provider.api_key?.trim() && !provider.clear_api_key ? { api_key: provider.api_key.trim() } : {}) };
      const result = await request("/api/service-config/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if ((versions.get(key) || 0) !== version || !draftFor(section)?.providers.some((item) => item.id === providerId)) return;
      const list = [...new Set((result.models || []).filter((model) => typeof model === "string" && model.trim()))].sort((a, b) => a.localeCompare(b));
      models.set(key, list);
      for (const role of SECTION_TASKS[section]) if (draftFor(section).routes[role].provider_id === providerId) renderModels(role);
      if (!list.length && draftFor(section).routes[task].provider_id === providerId) target.textContent = "服务未返回模型列表，请按文档手动填写。MiniMax 语音模型可能不在文字模型列表中。";
    } catch (error) {
      if ((versions.get(key) || 0) === version && draftFor(section)?.routes[task].provider_id === providerId) { target.textContent = error.message + " 仍可按服务商文档手动填写模型。"; target.dataset.level = "error"; }
    } finally { fetching.delete(key); renderState(); }
  }
  function addProvider() {
    const draft = draftFor(); if (!loaded || busy || locked || !draft) return;
    const random = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "").slice(0, 12) : Math.random().toString(36).slice(2, 14);
    const id = "provider-" + random;
    draft.providers.push({ id, name: `${currentSection === "asr" ? "语音" : "文字"}服务商 ${draft.providers.length + 1}`, vendor: "custom", scope: currentSection, base_url: "", text_api: "chat_completions", anthropic_auth: "api_key", speech_api: currentSection === "asr" ? "openai" : "none", enabled: true, api_key: "", clear_api_key: false, key_configured: false });
    draft.selectedId = id; render(); status("新服务商尚未保存。填写地址和密钥后，可点击编辑区上方「保存服务商」。");
  }
  function removeProvider() {
    const provider = selected(), draft = draftFor();
    if (!provider || busy || locked || sharedUsedElsewhere(provider)) return;
    draft.providers = draft.providers.filter((item) => item.id !== provider.id); invalidate(currentSection, provider.id);
    for (const task of SECTION_TASKS[currentSection]) if (draft.routes[task].provider_id === provider.id) draft.routes[task] = { ...draft.routes[task], provider_id: "", model: "" };
    draft.selectedId = draft.providers[0]?.id || ""; render();
    status("连接已从本模块草稿中移除。点击本模块的配置保存按钮后生效。");
  }
  function editProvider(event) {
    const provider = selected(); if (!provider || busy || locked) return;
    const map = { "provider-name": "name", "provider-vendor": "vendor", "provider-base-url": "base_url", "provider-api-key": "api_key", "provider-clear-key": "clear_api_key", "provider-text-api": "text_api", "provider-anthropic-auth": "anthropic_auth", "provider-speech-api": "speech_api", "provider-enabled": "enabled" };
    const key = map[event.target.id]; if (!key) return;
    if (currentSection === "asr" && ["text_api", "anthropic_auth"].includes(key) || currentSection === "text" && key === "speech_api") return;
    provider[key] = ["clear_api_key", "enabled"].includes(key) ? event.target.checked : event.target.value;
    if (key === "clear_api_key" && provider.clear_api_key) { provider.api_key = ""; $("provider-api-key").value = ""; }
    if (key === "vendor") {
      const preset = presetFor(provider);
      if (currentSection === "text") {
        if (TEXT_APIS.includes(preset?.text_api)) provider.text_api = preset.text_api;
        if (ANTHROPIC_AUTHS.includes(preset?.anthropic_auth)) provider.anthropic_auth = preset.anthropic_auth;
      } else if (["openai", "minimax"].includes(preset?.speech_api)) provider.speech_api = preset.speech_api;
      renderEditor();
    }
    if (["base_url", "api_key", "clear_api_key", "vendor", "text_api", "anthropic_auth", "speech_api"].includes(key)) invalidate(currentSection, provider.id);
    renderProviders(); renderEndpoints();
    if (["name", "vendor", "speech_api", "enabled"].includes(key)) renderWorkflow();
    $("provider-editor-heading").textContent = provider.name || "服务商设置"; renderState();
  }
  for (const id of editorFields) $(id).addEventListener(["provider-name", "provider-base-url", "provider-api-key"].includes(id) ? "input" : "change", editProvider);
  for (const task of TASKS) {
    const id = taskId(task);
    $(id + "-provider").addEventListener("change", () => {
      if (busy || locked || currentSection !== taskSection(task)) return;
      const route = draftFor().routes[task]; route.provider_id = $(id + "-provider").value; route.model = "";
      $(id + "-model").value = ""; $(id + "-model-search").value = ""; renderModels(task); renderState();
    });
    $(id + "-model").addEventListener("input", () => { if (!busy && !locked && currentSection === taskSection(task)) { draftFor().routes[task].model = $(id + "-model").value; renderState(); } });
    $(id + "-model-search").addEventListener("input", () => renderModels(task));
    $(id + "-model-list").addEventListener("change", () => { if (!busy && !locked && currentSection === taskSection(task) && $(id + "-model-list").value) { draftFor().routes[task].model = $(id + "-model-list").value; $(id + "-model").value = draftFor().routes[task].model; renderState(); } });
    $(id + "-fetch-models").addEventListener("click", () => fetchModels(task));
  }
  $("file-asr-format").addEventListener("change", () => { if (!busy && !locked && currentSection === "asr") { draftFor().routes.file_asr.response_format = $("file-asr-format").value; renderState(); } });
  $("use-provider-address").addEventListener("click", () => {
    const provider = selected(), address = addressFor(provider);
    if (!provider || !address || busy || locked) return;
    provider.base_url = address; $("provider-base-url").value = address; invalidate(currentSection, provider.id); renderEndpoints(); renderState();
    status("官方地址已填入当前草稿。请填写对应服务密钥；模型由你选择或手动填写。");
  });
  $("open-asr-settings").addEventListener("click", () => openSection("asr"));
  $("open-text-settings").addEventListener("click", () => openSection("text"));
  $("back-service-overview").addEventListener("click", () => openSection(""));
  $("add-provider").addEventListener("click", addProvider); $("remove-provider").addEventListener("click", removeProvider);
  $("save-provider").addEventListener("click", saveProvider); $("save-section-top").addEventListener("click", save);
  $("reload-service-config").addEventListener("click", load); $("service-config-form").addEventListener("submit", save);
  async function refreshLock() {
    const generation = configurationGeneration;
    const result = await request("/api/service-config");
    if (generation !== configurationGeneration) return;
    locked = Boolean(result.locked);
    $("service-lock-note").textContent = "正在录制、准备模型或处理音频。你可以查看配置，结束当前任务后再修改。";
    renderState();
  }
  async function pollHealth() {
    if (stopped) return;
    try {
      await refreshLock();
      if (connectionLost) { connectionLost = false; status("本机服务连接已恢复。当前草稿仍保留，可继续查看或保存配置。", "success"); renderState(); }
    }
    catch { connectionLost = true; locked = true; status("暂时无法连接本机服务，配置修改已暂停。连接恢复后可继续，当前草稿仍保留。", "error"); renderState(); }
    finally { if (!stopped) timer = setTimeout(pollHealth, 4000); }
  }
  render();
  return {
    load, save, saveProvider, fetchModels, addProvider, removeProvider, openSection, refreshLock,
    setLocked(value) { locked = Boolean(value); renderState(); },
    startPolling() { stopped = false; pollHealth(); }, stopPolling() { stopped = true; clearTimeout(timer); },
    get dirty() { return sectionDirty("asr") || sectionDirty("text"); }, get section() { return currentSection; },
  };
}

// Bootstrap the standalone settings page.
const serviceUI = createServiceConfigUI();
serviceUI.load(); serviceUI.startPolling();
window.addEventListener("beforeunload", (event) => { if (serviceUI.dirty) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener("pagehide", () => serviceUI.stopPolling());
window.addEventListener("pageshow", (event) => { if (event.persisted) serviceUI.startPolling(); });
window.addEventListener("popstate", () => serviceUI.openSection(sectionFromURL(), { historyMode: "none" }));
