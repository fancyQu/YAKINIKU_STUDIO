const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "..", "static", "live-settings.html"), "utf8");
const source = ["local-translation-settings.js", "live-settings.js"].map((name) => fs.readFileSync(path.join(__dirname, "..", "static", name), "utf8").replace(/^import .*\n/gm, "").replace(/^export /gm, "").replace(/\/\/ Bootstrap the live settings page\.[\s\S]*/, "")).join("\n");
const clone = (value) => JSON.parse(JSON.stringify(value));
const IDS = ["qwen2.5-1.5b-instruct-4bit", "qwen2.5-3b-instruct-4bit", "qwen3-4b-instruct-2507-4bit", "qwen2.5-7b-instruct-4bit"];
class Element {
  constructor(id = "", tagName = "div") { Object.assign(this, { id, tagName, value: "", hidden: false, disabled: false, textContent: "", dataset: {}, children: [], handlers: {}, attributes: {} }); }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ target: this, preventDefault() {} }))); }
}
function fixture() { return { backend: "mlx", model: "mlx-whisper-small", translation_backend: "cloud", local_translation_model: IDS[2], translation_route: { provider_id: "cloud", model: "saved-cloud-model" }, glossary: "人名", segment_seconds: 5, silence_ms: 600, configured: true, locked: false, providers: [{ id: "cloud", name: "云端服务", enabled: true, base_url: "https://text.example/v1", key_configured: true }] }; }
function harness(initial = fixture(), origin = "http://127.0.0.1:8765") {
  const elements = new Map([...html.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)].map((match) => [match[2], new Element(match[2], match[1])]));
  const element = (id) => { assert.ok(elements.has(id), id); return elements.get(id); };
  let config = clone(initial), active = null, runtimeAvailable = true, asrActive = null, failing = "", failingStatus = 0, gate;
  const requests = [], statuses = new Map(IDS.map((id) => [id, { model: id, state: "not_prepared", prepared_files: false, download: { model: id, state: "missing" } }]));
  const catalog = IDS.map((id, index) => ({ id, label: ["Qwen 2.5 · 1.5B", "Qwen 2.5 · 3B", "Qwen3 · 4B", "Qwen 2.5 · 7B"][index], recommended: index === 2, description: index === 1 ? "内存较紧张时的选择" : "本机文字翻译", disk_bytes: [1e9, 2e9, 2.5e9, 4.3e9][index], estimated_runtime_memory_gb: [[2, 3], [3, 5], [4, 7], [6, 10]][index], memory_estimate_note: "内存范围仅供估计。", repo_id: "mlx-community/fixture", download_source: "modelscope", source_label: "阿里 ModelScope（国内源）", source_url: "https://modelscope.cn/models/mlx-community/fixture" }));
  async function request(url, options = {}) {
    const body = options.body ? JSON.parse(options.body) : null;
    const selected = url.startsWith("/api/live-translation/status?") ? new URL(url, "http://local").searchParams.get("model") : null;
    const response = selected ? clone({ ...(statuses.get(selected) || {}), active_download: active, runtime_available: runtimeAvailable }) : null;
    requests.push({ url, method: options.method || "GET", body });
    if (failing === url) { failing = ""; throw Object.assign(new Error("模拟连接失败"), { status: failingStatus }); }
    if (gate?.url === url) { const held = gate; gate = null; await held.promise; if (held.failAfter) throw new Error("旧请求失败"); }
    if (url === "/api/live-settings" && options.method === "PUT") { config = { ...config, ...clone(body), configured: true }; return clone(config); }
    if (url === "/api/live-settings") return clone(config);
    if (url.startsWith("/api/live-model/status?")) return { state: "ready", prepared_files: true, download: { state: "downloaded" }, active_download: clone(asrActive) };
    if (url === "/api/live-translation/models") return { models: catalog.map((item) => ({ ...item, ...clone(statuses.get(item.id)) })), active_download: clone(active), runtime_available: runtimeAvailable, hardware: { total_memory_gb: 16 }, locked: config.locked };
    if (selected) return response;
    if (url === "/api/live-translation/download") { active = { model: body.model, state: "downloading", total_bytes: 2e9, downloaded_bytes: 0 }; statuses.get(body.model).download = clone(active); config.locked = true; return clone(active); }
    if (url === "/api/live-translation/download/cancel") { const cancelled = { ...active, model: body.model, state: "cancelled" }; statuses.get(body.model).download = clone(cancelled); active = null; config.locked = false; return cancelled; }
    if (url === "/api/live-translation/prepare") { statuses.set(body.model, { ...statuses.get(body.model), state: "ready", prepared_files: true }); return clone(statuses.get(body.model)); }
    if (url === "/api/service-config/models") return { models: ["cloud-a", "cloud-b"] };
    throw new Error("Unexpected request " + url);
  }
  const context = vm.createContext({ document: { getElementById: element, createElement: (tag) => new Element("", tag), defaultView: { location: { origin } } }, jsonRequest: request, URL, encodeURIComponent, console, confirm: () => true, setTimeout: () => 1, clearTimeout() {} });
  vm.runInContext(source + "\nvar controller = createLiveSettingsUI();", context);
  const evaluate = (code) => vm.runInContext(code, context);
  async function input(id, value, type = "change") { element(id).value = value; await element(id).emit(type); }
  return { element, requests, evaluate, input, get config() { return clone(config); }, setStatus(model, value) { statuses.set(model, { ...statuses.get(model), ...value }); }, setRuntime(value) { runtimeAvailable = value; }, setLocked(value) { config.locked = value; }, fail(url, status = 0) { failing = url; failingStatus = status; }, setActive(value) { active = value; }, setAsrActive(value) { asrActive = value; }, holdNext(url, failAfter = false) { let resolve; const promise = new Promise((accept) => { resolve = accept; }); gate = { url, promise, failAfter }; return { resolve }; } };
}
const mutations = (t) => t.requests.filter((item) => item.method !== "GET");
const localStatusURL = (model) => `/api/live-translation/status?model=${encodeURIComponent(model)}`;
async function catalogLoadsWithoutDownloadingAndExplainsMemory() {
  const t = harness(); await t.evaluate("controller.load()");
  assert.equal(mutations(t).length, 0);
  assert.deepEqual(t.element("live-local-translation-model").children.map((item) => item.value), IDS);
  assert.equal(t.element("live-cloud-translation-settings").hidden, false);
  assert.equal(t.element("live-local-translation-settings").hidden, true);
  assert.match(t.element("local-translation-hardware").textContent, /16 GB/);
  assert.match(t.element("local-translation-memory-note").textContent, /估计.*不包含.*语音识别.*macOS/);
  assert.match(t.element("live-local-translation-model").children[2].textContent, /推荐.*2\.50 GB.*4–7 GB/);
  assert.equal(t.element("local-translation-catalog").children.length, 4);
  assert.equal(t.evaluate("controller.dirty"), false);
}
async function switchingAndSavingRetainsBothIndependentSelections() {
  const t = harness(); await t.evaluate("controller.load()");
  await t.input("live-translation-model", "cloud-draft", "input");
  await t.input("live-translation-backend", "local"); await t.input("live-local-translation-model", IDS[1]);
  assert.equal(t.element("live-cloud-translation-settings").hidden, true);
  assert.equal(t.element("live-local-translation-settings").hidden, false);
  assert.equal(t.element("live-translation-model").value, "cloud-draft");
  assert.equal(t.evaluate("controller.dirty"), true); await t.evaluate("controller.save()");
  assert.equal(t.config.translation_backend, "local"); assert.equal(t.config.local_translation_model, IDS[1]);
  assert.deepEqual(t.config.translation_route, { provider_id: "cloud", model: "cloud-draft" });
  assert.equal(t.evaluate("controller.dirty"), false);
  await t.input("live-translation-backend", "cloud"); assert.equal(t.element("live-local-translation-model").value, IDS[1]);
  assert.equal(t.element("live-translation-model").value, "cloud-draft"); await t.evaluate("controller.save()"); await t.evaluate("controller.load()");
  assert.equal(t.config.translation_backend, "cloud"); assert.equal(t.config.local_translation_model, IDS[1]);
}
async function localSaveDoesNotRequireCloudConnectionOrDownload() {
  const config = fixture(); config.translation_route = {}; config.providers = [];
  const t = harness(config); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local");
  assert.equal(t.element("save-live-settings").disabled, false); await t.evaluate("controller.save()");
  assert.equal(t.config.translation_backend, "local"); assert.deepEqual(t.config.translation_route, { provider_id: "", model: "" });
  assert.equal(mutations(t).length, 1); assert.equal(mutations(t)[0].url, "/api/live-settings");
  assert.equal(Object.hasOwn(mutations(t)[0].body, "api_key"), false);
}
async function explicitDownloadPreservesUnsavedDraftAndNeverLoads() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local"); await t.input("live-local-translation-model", IDS[1]); await t.input("live-glossary", "未保存术语", "input");
  await t.element("download-local-translation-model").emit("click");
  assert.deepEqual(mutations(t).map((item) => item.url), ["/api/live-translation/download"]);
  assert.deepEqual(mutations(t)[0].body, { model: IDS[1], consent: true });
  assert.equal(t.config.translation_backend, "cloud"); assert.equal(t.config.local_translation_model, IDS[2]);
  assert.equal(t.element("live-glossary").value, "未保存术语"); assert.equal(t.evaluate("controller.dirty"), true);
  assert.equal(t.element("save-live-settings").disabled, true); assert.equal(t.element("download-live-model").disabled, true);
  assert.equal(t.element("cancel-local-translation-download").disabled, false);
  await t.evaluate("controller.localTranslation.download()"); assert.equal(mutations(t).length, 1);
}
async function completionAndPrepareDoNotSaveTheDraft() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local"); await t.input("live-local-translation-model", IDS[1]);
  t.setStatus(IDS[1], { prepared_files: true, download: { state: "downloaded" } }); await t.evaluate("controller.localTranslation.refresh()");
  assert.equal(t.element("prepare-local-translation-model").disabled, false);
  assert.equal(t.element("download-local-translation-model").disabled, true);
  assert.equal(t.element("local-translation-runtime-badge").textContent, "已下载");
  await t.element("prepare-local-translation-model").emit("click");
  assert.deepEqual(mutations(t).map((item) => item.url), ["/api/live-translation/prepare"]);
  assert.equal(t.element("local-translation-runtime-badge").textContent, "已就绪");
  assert.equal(t.element("live-translation-badge").textContent, "已就绪");
  assert.equal(t.config.translation_backend, "cloud"); assert.equal(t.evaluate("controller.dirty"), true);
  assert.match(t.element("live-settings-status").textContent, /当前配置修改仍保留/);
}
async function reloadFindsAndCancelsAnotherModelsDownloadEvenInCloudMode() {
  const config = fixture(); config.locked = true;
  const t = harness(config); t.setActive({ model: IDS[3], state: "downloading", downloaded_bytes: 2e9, total_bytes: 4e9 }); await t.evaluate("controller.load()");
  assert.equal(t.element("live-local-translation-settings").hidden, true);
  assert.equal(t.element("local-translation-active-download").hidden, false);
  assert.equal(t.element("local-translation-download-progress").value, 50);
  assert.match(t.element("local-translation-active-heading").textContent, /7B/);
  assert.match(t.element("local-translation-download-status").textContent, /与上方模型选择不同/);
  assert.equal(t.element("live-local-translation-model").value, IDS[2]);
  assert.equal(t.element("cancel-local-translation-download").disabled, false);
  await t.element("cancel-local-translation-download").emit("click");
  assert.deepEqual(mutations(t)[0].body, { model: IDS[3] });
  assert.equal(t.element("local-translation-active-download").hidden, true);
  assert.equal(t.element("save-live-settings").disabled, false);
  assert.equal(t.config.translation_backend, "cloud"); assert.equal(t.config.local_translation_model, IDS[2]);
}
async function loadingVerifyingAndDownloadFailureAreDistinct() {
  const config = fixture(); config.translation_backend = "local";
  const t = harness(config); t.setStatus(IDS[2], { download: { state: "verifying" } }); t.setActive({ model: IDS[2], state: "verifying", total_bytes: 100, downloaded_bytes: 100 }); await t.evaluate("controller.load()");
  assert.equal(t.element("local-translation-runtime-badge").textContent, "校验中"); assert.equal(t.element("local-translation-download-badge").textContent, "校验中"); assert.equal(t.element("prepare-local-translation-model").disabled, true);
  t.setActive(null); t.setStatus(IDS[2], { state: "loading", prepared_files: true, download: { state: "downloaded" } }); await t.evaluate("controller.localTranslation.refresh()");
  assert.equal(t.element("local-translation-runtime-badge").textContent, "加载中"); assert.equal(t.element("prepare-local-translation-model").disabled, true);
  t.setStatus(IDS[2], { state: "not_prepared", prepared_files: false, download: { state: "failed", error: "权重下载中断" } }); await t.evaluate("controller.localTranslation.refresh()");
  assert.equal(t.element("local-translation-runtime-badge").textContent, "下载失败"); assert.equal(t.element("download-local-translation-model").disabled, false); assert.match(t.element("local-translation-runtime-status").textContent, /权重下载中断/);
}
async function statusFailuresRecoverAndNeverDiscardDraftOrForceCloud() {
  const config = fixture(); config.translation_backend = "local";
  const t = harness(config); await t.evaluate("controller.load()"); await t.input("live-glossary", "保留当前修改", "input");
  t.fail(localStatusURL(IDS[2])); await t.evaluate("controller.localTranslation.updateStatus()");
  assert.equal(t.element("download-local-translation-model").disabled, true); assert.equal(t.element("prepare-local-translation-model").disabled, true);
  assert.match(t.element("local-translation-runtime-status").textContent, /状态暂时无法读取/); assert.equal(t.element("live-translation-backend").value, "local");
  await t.evaluate("controller.localTranslation.refresh()");
  assert.equal(t.element("download-local-translation-model").disabled, false); assert.equal(t.element("live-glossary").value, "保留当前修改"); assert.equal(t.evaluate("controller.dirty"), true);
}
async function unavailableRuntimeExplainsLimitsWithoutBlockingCloud() {
  const t = harness(); t.setRuntime(false); await t.evaluate("controller.load()");
  assert.equal(t.element("save-live-settings").disabled, false); assert.equal(t.element("live-fetch-models").disabled, false);
  await t.input("live-translation-backend", "local");
  assert.equal(t.element("live-translation-badge").textContent, "环境未就绪"); assert.match(t.element("local-translation-runtime-status").textContent, /安装 MLX.*不会自动切换云端/);
  assert.equal(t.element("prepare-local-translation-model").disabled, true);
  assert.equal(mutations(t).length, 0);
}
async function catalogFailureDoesNotBlockLegacyCloudSettings() {
  const config = fixture(); delete config.translation_backend; delete config.local_translation_model;
  const t = harness(config); t.fail("/api/live-translation/models"); await t.evaluate("controller.load()");
  assert.equal(t.element("live-translation-backend").value, "cloud"); assert.equal(t.element("save-live-settings").disabled, false); assert.equal(t.element("live-fetch-models").disabled, false);
  await t.input("live-translation-model", "继续使用云端", "input"); await t.evaluate("controller.save()");
  assert.equal(t.config.translation_route.model, "继续使用云端"); assert.equal(t.config.translation_backend, "cloud"); assert.equal(t.evaluate("controller.dirty"), false);
}
async function catalog404ExplainsOldServiceAndPreservesCloudDraft() {
  const t = harness(fixture(), "http://127.0.0.1:8877"); await t.evaluate("controller.load()"); await t.input("live-translation-model", "待保存云端模型", "input");
  t.fail("/api/live-translation/models", 404); await t.evaluate("controller.localTranslation.refresh()");
  const message = t.element("local-translation-runtime-status").textContent;
  assert.match(message, /本机服务.*http:\/\/127\.0\.0\.1:8877.*404.*旧版本.*重启该地址对应的本机服务.*重新读取/);
  assert.doesNotMatch(message, /8765/);
  assert.match(message, /与模型下载源无关/); assert.doesNotMatch(message, /自动重试|Hugging Face/);
  assert.equal(t.element("live-translation-model").value, "待保存云端模型"); assert.equal(t.evaluate("controller.dirty"), true);
  assert.equal(t.element("save-live-settings").disabled, false); assert.equal(t.element("live-fetch-models").disabled, false);
  await t.evaluate("controller.save()"); assert.equal(t.config.translation_route.model, "待保存云端模型");
  await t.evaluate("controller.localTranslation.refresh()");
  assert.doesNotMatch(t.element("local-translation-runtime-status").textContent, /旧版本/);
  assert.ok(t.requests.every((item) => item.url.startsWith("/api/")), "requests remain relative to the current service without port fallback");
}
async function status404RequiresRestartWithoutDiscardingLocalSelection() {
  const t = harness(fixture(), "http://127.0.0.1:8877"); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local"); await t.input("live-local-translation-model", IDS[1]);
  t.fail(localStatusURL(IDS[1]), 404); await t.evaluate("controller.localTranslation.updateStatus()");
  const message = t.element("local-translation-runtime-status").textContent;
  assert.match(message, /http:\/\/127\.0\.0\.1:8877.*旧版本.*重启该地址对应的本机服务.*重新读取/); assert.doesNotMatch(message, /自动重试|8765/);
  assert.match(message, /与模型下载源无关/);
  assert.equal(t.element("live-local-translation-model").value, IDS[1]); assert.equal(t.element("live-translation-backend").value, "local"); assert.equal(t.evaluate("controller.dirty"), true);
  assert.equal(t.element("download-local-translation-model").disabled, true); assert.equal(t.element("prepare-local-translation-model").disabled, true);
  await t.input("live-translation-backend", "cloud"); assert.equal(t.element("save-live-settings").disabled, false); assert.equal(t.element("live-fetch-models").disabled, false);
  await t.evaluate("controller.localTranslation.updateStatus()"); assert.doesNotMatch(t.element("local-translation-runtime-status").textContent, /旧版本/);
  assert.ok(t.requests.every((item) => item.url.startsWith("/api/")), "status requests do not change the configured service origin");
}
async function staleResponsesAndErrorsCannotReplaceCurrentModel() {
  for (const failAfter of [false, true]) {
    const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local");
    const gate = t.holdNext(localStatusURL(IDS[1]), failAfter), pending = t.input("live-local-translation-model", IDS[1]);
    t.setStatus(IDS[2], { state: "ready", prepared_files: true }); await t.input("live-local-translation-model", IDS[2]); gate.resolve(); await pending;
    assert.equal(t.element("live-local-translation-model").value, IDS[2]); assert.equal(t.element("local-translation-runtime-badge").textContent, "已就绪");
    assert.doesNotMatch(t.element("local-translation-runtime-status").textContent, /旧请求/); assert.equal(mutations(t).length, 0);
  }
}
async function lockedAndBusyActionsCannotStartDuplicateWork() {
  const config = fixture(); config.translation_backend = "local"; config.locked = true;
  const t = harness(config); await t.evaluate("controller.load()");
  await t.evaluate("controller.localTranslation.download()"); await t.evaluate("controller.localTranslation.prepare()"); assert.equal(mutations(t).length, 0);
  t.setLocked(false); await t.evaluate("controller.refreshLock()");
  const gate = t.holdNext("/api/live-translation/download"), pending = t.evaluate("controller.localTranslation.download()");
  assert.equal(t.element("download-local-translation-model").attributes["aria-busy"], "true"); assert.equal(t.element("save-live-settings").disabled, true);
  await t.evaluate("controller.localTranslation.download()"); await t.evaluate("controller.save()"); assert.equal(mutations(t).length, 1); gate.resolve(); await pending;
}
async function failedMutationsKeepDraftAndCancellationAvailable() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-backend", "local");
  t.fail("/api/live-translation/download"); await t.evaluate("controller.localTranslation.download()");
  assert.match(t.element("live-settings-status").textContent, /下载未能开始/); assert.equal(t.evaluate("controller.dirty"), true); assert.equal(t.element("download-local-translation-model").disabled, false);
  await t.evaluate("controller.localTranslation.download()"); t.fail("/api/live-translation/download/cancel"); await t.evaluate("controller.localTranslation.cancelDownload()");
  assert.match(t.element("live-settings-status").textContent, /取消本机翻译下载未完成/); assert.equal(t.element("cancel-local-translation-download").disabled, false); assert.equal(t.element("save-live-settings").disabled, true); assert.equal(t.config.translation_backend, "cloud");
}
async function asrDownloadsLockTranslationWithoutOfferingWrongCancellation() {
  const t = harness(); t.setAsrActive({ backend: "mlx", model: "mlx-whisper-large-v3-turbo", state: "downloading" }); await t.evaluate("controller.load()");
  assert.equal(t.element("local-translation-active-download").hidden, true); assert.equal(t.element("cancel-live-model-download").hidden, false);
  assert.equal(t.element("download-local-translation-model").disabled, true); await t.evaluate("controller.localTranslation.download()"); await t.evaluate("controller.localTranslation.cancelDownload()");
  assert.equal(mutations(t).length, 0);
}
async function sourcesShowThePlatformAndRepositoryForSelectedAndActiveModels() {
  const t = harness(); await t.evaluate("controller.load()");
  assert.match(t.element("local-translation-source").textContent, /阿里 ModelScope（国内源）.*mlx-community\/fixture/);
  assert.doesNotMatch(t.element("local-translation-source").textContent, /Hugging Face/);
  t.setStatus(IDS[2], { download: { state: "missing", download_source: "modelscope", repo_id: "aliyun/custom-qwen" } });
  t.setActive({ model: IDS[3], state: "downloading", download_source: "modelscope", source_label: "阿里 ModelScope（国内源）", source_url: "https://modelscope.cn/models/aliyun/active-qwen", repo_id: "aliyun/active-qwen", total_bytes: 100, downloaded_bytes: 50 });
  await t.evaluate("controller.localTranslation.refresh()");
  assert.match(t.element("local-translation-source").textContent, /阿里 ModelScope（国内源）.*aliyun\/custom-qwen/);
  assert.match(t.element("local-translation-download-status").textContent, /阿里 ModelScope（国内源）.*aliyun\/active-qwen/);
  assert.match(t.element("local-translation-download-status").textContent, /与上方模型选择不同/);
  assert.equal(t.element("live-local-translation-settings").hidden, true, "a background download identifies its platform even in cloud mode");
}
(async () => {
  const tests = [catalogLoadsWithoutDownloadingAndExplainsMemory, switchingAndSavingRetainsBothIndependentSelections, localSaveDoesNotRequireCloudConnectionOrDownload, explicitDownloadPreservesUnsavedDraftAndNeverLoads, completionAndPrepareDoNotSaveTheDraft, reloadFindsAndCancelsAnotherModelsDownloadEvenInCloudMode, loadingVerifyingAndDownloadFailureAreDistinct, statusFailuresRecoverAndNeverDiscardDraftOrForceCloud, unavailableRuntimeExplainsLimitsWithoutBlockingCloud, catalogFailureDoesNotBlockLegacyCloudSettings, catalog404ExplainsOldServiceAndPreservesCloudDraft, status404RequiresRestartWithoutDiscardingLocalSelection, staleResponsesAndErrorsCannotReplaceCurrentModel, lockedAndBusyActionsCannotStartDuplicateWork, failedMutationsKeepDraftAndCancellationAvailable, asrDownloadsLockTranslationWithoutOfferingWrongCancellation, sourcesShowThePlatformAndRepositoryForSelectedAndActiveModels];
  for (const test of tests) await test();
  console.log(`${tests.length} local translation UI scenarios passed`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
