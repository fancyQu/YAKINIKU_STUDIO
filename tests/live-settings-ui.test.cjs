const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "..", "static", "live-settings.html"), "utf8");
const localSource = fs.readFileSync(path.join(__dirname, "..", "static", "local-translation-settings.js"), "utf8").replace(/^export /gm, "");
const source = localSource + "\n" + fs.readFileSync(path.join(__dirname, "..", "static", "live-settings.js"), "utf8")
  .replace(/^import .*\n/gm, "").replace(/^export /gm, "").replace(/\/\/ Bootstrap the live settings page\.[\s\S]*/, "");
const clone = (value) => JSON.parse(JSON.stringify(value));
class Element {
  constructor(id = "", tagName = "div") { Object.assign(this, { id, tagName: tagName.toUpperCase(), value: "", hidden: false, disabled: false, textContent: "", dataset: {}, children: [], handlers: {}, attributes: {} }); }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ target: this, preventDefault() {} }))); }
}
function fixture() {
  return { backend: "mlx", model: "mlx-whisper-small", translation_route: { provider_id: "saved", model: "chosen-live" }, glossary: "live glossary", segment_seconds: 5, silence_ms: 600, configured: true, locked: false, translation_ready: true,
    providers: [{ id: "saved", name: "Saved text", base_url: "https://text.example/v1", text_api: "responses", enabled: true, key_configured: true }, { id: "alt", name: "Alternate text", base_url: "https://alt.example/anthropic", text_api: "anthropic", anthropic_auth: "bearer", enabled: true, key_configured: true }] };
}
function harness(initial = fixture()) {
  const elements = new Map([...html.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)].map((match) => [match[2], new Element(match[2], match[1])]));
  const element = (id) => { assert.ok(elements.has(id), id); return elements.get(id); };
  let config = clone(initial), status = { state: "not_prepared", backend: "mlx", model: "mlx-whisper-small", path: "/models/small", version: "fixed", prepared_files: true }, failing = "";
  let confirmReply = true, hold;
  const confirmations = [];
  const requests = [];
  async function request(url, options = {}) {
    const body = options.body ? JSON.parse(options.body) : null;
    const statusResponse = url.startsWith("/api/live-model/status") ? clone(status) : null;
    requests.push({ url, method: options.method || "GET", body });
    if (failing && url === failing) { failing = ""; throw new Error("模拟失败"); }
    if (hold?.url === url) { const gate = hold; hold = null; await gate.promise; if (gate.failAfter) throw new Error("旧模型状态请求失败"); }
    if (url === "/api/live-settings" && options.method === "PUT") { config = { ...config, ...clone(body), configured: true }; return clone(config); }
    if (url === "/api/live-settings") return clone(config);
    if (url.startsWith("/api/live-model/status")) return statusResponse;
    if (url === "/api/live-model/download") { status.download = { backend: body.backend, model: body.model, state: "downloading", downloaded_bytes: 0, total_bytes: 500000000, repo_id: "mlx-community/test", revision: "fixture" }; status.active_download = clone(status.download); config.locked = true; return clone(status.download); }
    if (url === "/api/live-model/download/cancel") { const cancelled = { ...(status.active_download || status.download), state: "cancelled" }; if (!status.model || status.model === body.model) status.download = clone(cancelled); status.active_download = null; config.locked = false; return clone(cancelled); }
    if (url === "/api/live-model/prepare") { status = { state: "ready", ...body, path: "/models/selected", version: "fixed" }; return clone(status); }
    if (url === "/api/service-config/models") return { models: ["embed", "alternate-text", "chosen-live", "alternate-text"] };
    throw new Error("unexpected request " + url);
  }
  const context = vm.createContext({ document: { getElementById: element, createElement: (tag) => new Element("", tag) }, jsonRequest: request, URLSearchParams, encodeURIComponent, console, confirm(message) { confirmations.push(message); return confirmReply; } });
  vm.runInContext(source + "\nvar controller = createLiveSettingsUI();", context);
  const evaluate = (code) => vm.runInContext(code, context);
  async function input(id, value, event = "input") { element(id).value = value; await element(id).emit(event); }
  return { element, evaluate, input, requests, confirmations, get config() { return clone(config); }, fail(url) { failing = url; }, setStatus(value) { status = value; },
    allowReload(value) { confirmReply = value; }, setServerLocked(value) { config.locked = value; },
    holdNext(url, failAfter = false) { let resolve; const promise = new Promise((accept) => { resolve = accept; }); hold = { url, promise, failAfter }; return { resolve }; } };
}
async function initialSelectionDoesNotClaimToBeSaved() {
  const initial = fixture(); initial.configured = false;
  const t = harness(initial); await t.evaluate("controller.load()");
  assert.equal(t.element("live-save-state").textContent, "当前显示初始选择，尚未独立保存");
  assert.equal(t.evaluate("controller.dirty"), false);
  await t.input("live-translation-model", "draft-live-model");
  assert.equal(t.element("live-save-state").textContent, "直播配置有未保存的修改");
  await t.evaluate("controller.load()");
  assert.equal(t.element("live-save-state").textContent, "当前显示初始选择，尚未独立保存");
  await t.evaluate("controller.save()");
  assert.equal(t.element("live-save-state").textContent, "直播配置已保存");
}
async function modelSelectionPersistsWithoutReset() {
  const t = harness(); await t.evaluate("controller.load()");
  assert.equal(t.element("live-asr-model").value, "mlx:mlx-whisper-small");
  await t.input("live-asr-model", "tiny:tiny", "change"); await t.evaluate("controller.save()");
  assert.equal(t.config.backend, "tiny"); assert.equal(t.config.model, "tiny");
  await t.evaluate("controller.load()"); assert.equal(t.element("live-asr-model").value, "tiny:tiny");
  assert.match(t.element("live-model-status").textContent, /加载/);
}
async function independentSavePayloadAndFailurePreservesDraft() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-model", "new-live-model");
  t.fail("/api/live-settings"); await t.evaluate("controller.save()");
  assert.equal(t.element("live-translation-model").value, "new-live-model"); assert.equal(t.evaluate("controller.dirty"), true);
  assert.match(t.element("live-settings-status").textContent, /模拟失败/);
  await t.evaluate("controller.save()");
  const body = t.requests.find((r) => r.method === "PUT").body;
  assert.deepEqual(Object.keys(body).sort(), ["backend", "glossary", "model", "segment_seconds", "silence_ms", "translation_route", "translation_backend", "local_translation_model"].sort());
  assert.equal(Object.hasOwn(body, "correction"), false); assert.equal(Object.hasOwn(body, "api_key"), false);
  assert.equal(t.evaluate("controller.dirty"), false);
}
async function prepareSavesSelectedModelBeforeWarmup() {
  const t = harness(); await t.evaluate("controller.load()");
  await t.input("live-asr-model", "mlx:mlx-whisper-large-v3-turbo", "change");
  await t.evaluate("controller.prepare()");
  const actions = t.requests.filter((r) => r.method !== "GET");
  assert.deepEqual(actions.map((r) => r.url), ["/api/live-settings", "/api/live-model/prepare"]);
  assert.equal(actions[0].body.model, "mlx-whisper-large-v3-turbo"); assert.equal(actions[1].body.model, "mlx-whisper-large-v3-turbo");
  assert.equal(t.element("live-model-badge").textContent, "已就绪");
}
async function modelListingUsesSavedConnectionAndAllowsEveryReturnedModel() {
  const t = harness(); await t.evaluate("controller.load()");
  await t.input("live-translation-provider", "alt", "change");
  assert.equal(t.element("live-translation-model").value, "");
  await t.evaluate("controller.fetchModels()");
  const body = t.requests.find((r) => r.url === "/api/service-config/models").body;
  assert.equal(body.provider_id, "alt"); assert.equal(body.text_api, "anthropic"); assert.equal(body.anthropic_auth, "bearer"); assert.equal(Object.hasOwn(body, "api_key"), false);
  assert.deepEqual(t.element("live-translation-model-list").children.map((o) => o.value), ["", "alternate-text", "chosen-live", "embed"]);
  await t.input("live-translation-model-list", "alternate-text", "change"); assert.equal(t.element("live-translation-model").value, "alternate-text");
}
async function activeTaskLocksAllMutationsAndModelFailureDoesNotFallback() {
  const config = fixture(); config.locked = true;
  const t = harness(config); await t.evaluate("controller.load()");
  assert.equal(t.element("save-live-settings").disabled, true); assert.equal(t.element("prepare-live-model").disabled, true);
  await t.evaluate("controller.save()"); await t.evaluate("controller.prepare()"); assert.equal(t.requests.filter((r) => r.method !== "GET").length, 0);
  const f = harness(); f.setStatus({ state: "failed", error: "GPU 暂不可用" }); await f.evaluate("controller.load()");
  assert.equal(f.element("live-asr-model").value, "mlx:mlx-whisper-small"); assert.match(f.element("live-model-status").textContent, /GPU/); assert.equal(f.element("live-model-badge").textContent, "加载失败");
}
async function reloadCancellationAndLockRefreshPreserveDrafts() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-model", "用户的待保存模型");
  t.allowReload(false); const count = t.requests.length;
  await t.evaluate("controller.load()");
  assert.equal(t.requests.length, count); assert.equal(t.element("live-translation-model").value, "用户的待保存模型");
  assert.match(t.confirmations.at(-1), /放弃尚未保存/);
  t.setServerLocked(true); await t.evaluate("controller.refreshLock()");
  assert.equal(t.element("save-live-settings").disabled, true);
  assert.equal(t.element("live-translation-model").value, "用户的待保存模型");
  t.setServerLocked(false); await t.evaluate("controller.refreshLock()");
  assert.equal(t.element("save-live-settings").disabled, false); assert.equal(t.evaluate("controller.dirty"), true);
}
async function successfulSaveSurvivesModelStatusFailureAndShowsBusyFeedback() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-model", "新直播模型");
  const gate = t.holdNext("/api/live-settings"), pending = t.evaluate("controller.save()");
  assert.equal(t.element("save-live-settings").getAttribute("aria-busy"), "true");
  assert.match(t.element("save-live-settings").textContent, /正在保存/);
  t.fail("/api/live-model/status?backend=mlx&model=mlx-whisper-small");
  gate.resolve(); await pending;
  assert.equal(t.config.translation_route.model, "新直播模型"); assert.equal(t.evaluate("controller.dirty"), false);
  assert.match(t.element("live-settings-status").textContent, /直播配置已保存/);
  assert.match(t.element("live-save-state").textContent, /模型状态暂时无法读取/);
  assert.equal(t.element("save-live-settings").getAttribute("aria-busy"), "false");
}
async function preparationFailureDistinguishesSavedConfiguration() {
  const t = harness(); await t.evaluate("controller.load()"); await t.input("live-translation-model", "已保存但模型未就绪");
  t.fail("/api/live-model/prepare"); await t.evaluate("controller.prepare()");
  assert.equal(t.config.translation_route.model, "已保存但模型未就绪"); assert.equal(t.evaluate("controller.dirty"), false);
  assert.match(t.element("live-settings-status").textContent, /配置已保存；本机模型准备未完成/);
  assert.equal(t.element("prepare-live-model").getAttribute("aria-busy"), "false");
}
async function openingAndSelectingNeverDownloadAutomatically() {
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "missing", total_bytes: 500000000, repo_id: "mlx-community/fixture", revision: "fixed" } });
  await t.evaluate("controller.load()");
  assert.equal(t.element("prepare-live-model").disabled, true);
  assert.equal(t.element("download-live-model").disabled, false);
  assert.equal(t.element("live-model-download").hidden, false);
  assert.match(t.element("live-download-source").textContent, /mlx-community\/fixture/);
  assert.match(t.element("live-download-size").textContent, /MB/);
  await t.input("live-asr-model", "mlx:mlx-whisper-large-v3-turbo", "change");
  await t.evaluate("controller.prepare()");
  assert.equal(t.requests.filter(r => r.method !== "GET").length, 0, "missing files cannot save or prepare, and GET never downloads");
  assert.match(t.element("live-settings-status").textContent, /先下载/);
  await t.evaluate("controller.save()");
  assert.equal(t.requests.filter(r => r.url === "/api/live-model/download").length, 0, "saving cannot download");
}
async function explicitDownloadPreservesDraftAndNeverPrepares() {
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "missing" } });
  await t.evaluate("controller.load()"); await t.input("live-translation-model", "未保存的翻译模型");
  await t.input("live-asr-model", "mlx:mlx-whisper-large-v3-turbo", "change");
  await t.element("download-live-model").emit("click");
  const actions = t.requests.filter(r => r.method !== "GET");
  assert.deepEqual(actions.map(r => r.url), ["/api/live-model/download"]);
  assert.deepEqual(actions[0].body, { backend: "mlx", model: "mlx-whisper-large-v3-turbo", consent: true });
  assert.equal(t.config.model, "mlx-whisper-small");
  assert.equal(t.element("live-translation-model").value, "未保存的翻译模型");
  assert.equal(t.evaluate("controller.dirty"), true);
  assert.equal(t.element("download-live-model").disabled, true);
  assert.equal(t.element("cancel-live-model-download").hidden, false);
  assert.equal(t.element("cancel-live-model-download").disabled, false, "cancel remains enabled while download locks settings");
  assert.equal(t.element("save-live-settings").disabled, true);
  assert.equal(t.element("prepare-live-model").disabled, true);
  await t.evaluate("controller.downloadModel()");
  assert.equal(t.requests.filter(r => r.url === "/api/live-model/download").length, 1);
}
async function downloadProgressCompletionAndCancellationNeedNoSave() {
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "downloading", downloaded_bytes: 250000000, total_bytes: 500000000 } });
  t.setServerLocked(true); await t.evaluate("controller.load()");
  assert.equal(t.element("live-download-progress").hidden, false); assert.equal(t.element("live-download-progress").value, 50);
  assert.match(t.element("live-download-status").textContent, /250\.0 MB \/ 500\.0 MB/);
  await t.element("cancel-live-model-download").emit("click");
  assert.deepEqual(t.requests.find(r => r.url.endsWith("/download/cancel")).body, { backend: "mlx", model: "mlx-whisper-small" });
  assert.equal(t.element("download-live-model").disabled, false);
  assert.equal(t.element("download-live-model").textContent, "重新下载模型");
  assert.equal(t.element("cancel-live-model-download").hidden, true);
  t.setStatus({ state: "not_prepared", download: { state: "verifying", total_bytes: 500000000, downloaded_bytes: 500000000 } });
  await t.evaluate("controller.updateModelStatus()");
  assert.equal(t.element("prepare-live-model").disabled, true); assert.match(t.element("live-download-badge").textContent, /校验/);
  t.setStatus({ state: "not_prepared", prepared_files: true, download: { state: "downloaded", total_bytes: 500000000 } });
  await t.evaluate("controller.updateModelStatus()");
  assert.equal(t.element("download-live-model").disabled, true); assert.equal(t.element("prepare-live-model").disabled, false);
  assert.match(t.element("live-download-status").textContent, /才会加载/);
  assert.equal(t.requests.filter(r => r.method === "PUT" || r.url === "/api/live-model/prepare").length, 0, "completion and cancellation do not save or prepare");
}
async function tinyHidesDownloadsAndFailureAllowsUserRetry() {
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "failed", error: "下载连接断开" } });
  await t.evaluate("controller.load()");
  assert.equal(t.element("download-live-model").disabled, false); assert.equal(t.element("download-live-model").textContent, "重新下载模型");
  assert.match(t.element("live-download-status").textContent, /下载连接断开/);
  await t.input("live-asr-model", "tiny:tiny", "change");
  assert.equal(t.element("live-model-download").hidden, true);
  await t.evaluate("controller.downloadModel()");
  assert.equal(t.requests.filter(r => r.url === "/api/live-model/download").length, 0);
}
async function switchingModelsIgnoresStaleResultsAndErrors() {
  for (const fails of [false, true]) {
    const t = harness(); await t.evaluate("controller.load()");
    t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "missing" } });
    const gate = t.holdNext("/api/live-model/status?backend=mlx&model=mlx-whisper-large-v3-turbo", fails);
    const olderSelection = t.input("live-asr-model", "mlx:mlx-whisper-large-v3-turbo", "change");
    assert.equal(t.element("prepare-live-model").disabled, true); assert.equal(t.element("download-live-model").disabled, true, "unknown availability requires a successful read");
    t.setStatus({ state: "ready", prepared_files: true, download: { state: "downloaded" } });
    await t.input("live-asr-model", "mlx:mlx-whisper-small", "change");
    gate.resolve(); await olderSelection;
    assert.equal(t.element("prepare-live-model").disabled, false); assert.equal(t.element("live-download-badge").textContent, "已下载");
    assert.match(t.element("live-model-status").textContent, /Whisper Small 已加载/);
    assert.equal(t.requests.filter(r => r.method !== "GET").length, 0);
  }
}
async function failedDownloadAndCancelRequestsKeepDraftsAndAllowRetry() {
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "missing" } });
  await t.evaluate("controller.load()"); await t.input("live-translation-model", "保留草稿");
  t.fail("/api/live-model/download"); await t.evaluate("controller.downloadModel()");
  assert.equal(t.element("download-live-model").disabled, false); assert.match(t.element("live-settings-status").textContent, /下载未能开始/);
  assert.equal(t.element("live-translation-model").value, "保留草稿");assert.equal(t.evaluate("controller.dirty"), true);
  await t.evaluate("controller.downloadModel()");
  t.fail("/api/live-model/download/cancel");await t.evaluate("controller.cancelDownload()");
  assert.equal(t.element("cancel-live-model-download").disabled, false);assert.equal(t.element("save-live-settings").disabled, true);
  assert.match(t.element("live-settings-status").textContent, /取消下载未完成/);
  assert.equal(t.requests.filter(r => r.method === "PUT" || r.url === "/api/live-model/prepare").length, 0);
}
async function reloadAndRefreshCanCancelAnotherModelsDownload() {
  const active = { backend: "mlx", model: "mlx-whisper-large-v3-turbo", state: "downloading", total_bytes: 1610000000, downloaded_bytes: 805000000, repo_id: "mlx-community/turbo-fixture", revision: "turbo-revision" };
  const savedSmallStatus = { state: "ready", backend: "mlx", model: "mlx-whisper-small", prepared_files: true, path: "/models/small", download: { state: "downloaded", total_bytes: 481000000, repo_id: "mlx-community/small-fixture" }, active_download: active };
  const t = harness(); t.setStatus({ state: "not_prepared", prepared_files: false, download: { state: "missing" } });
  await t.evaluate("controller.load()");
  await t.input("live-asr-model", "mlx:mlx-whisper-large-v3-turbo", "change");
  await t.evaluate("controller.downloadModel()");
  t.setStatus(clone(savedSmallStatus));
  await t.evaluate("controller.load()");
  assert.equal(t.config.model,"mlx-whisper-small");assert.equal(t.element("live-asr-model").value,"mlx:mlx-whisper-small");
  assert.equal(t.element("live-asr-model").disabled,true);assert.equal(t.element("live-model-badge").textContent,"已就绪");
  assert.match(t.element("live-model-status").textContent,/Whisper Small 已加载/);
  assert.match(t.element("live-download-heading").textContent,/large-v3-turbo/);assert.match(t.element("live-download-source").textContent,/turbo-fixture/);
  assert.match(t.element("live-download-size").textContent,/1\.61 GB/);assert.equal(t.element("live-download-progress").value,50);
  assert.match(t.element("live-download-status").textContent,/保存选择保持不变/);
  assert.equal(t.element("cancel-live-model-download").hidden,false);assert.equal(t.element("cancel-live-model-download").disabled,false);
  await t.evaluate("controller.cancelDownload()");
  assert.deepEqual(t.requests.find(r=>r.url.endsWith("/download/cancel")).body,{backend:"mlx",model:"mlx-whisper-large-v3-turbo"});
  assert.equal(t.config.model,"mlx-whisper-small");assert.equal(t.element("live-model-badge").textContent,"已就绪");
  assert.equal(t.element("live-download-badge").textContent,"已下载");assert.match(t.element("live-download-size").textContent,/481\.0 MB/);
  assert.equal(t.element("prepare-live-model").disabled,false);
  assert.equal(t.requests.filter(r=>r.method==="PUT"||r.url==="/api/live-model/prepare").length,0);

  // A new page has no unsaved Turbo selection, but still exposes the global task.
  const initial=fixture();initial.locked=true;
  const fresh=harness(initial);fresh.setStatus(clone(savedSmallStatus));await fresh.evaluate("controller.load()");
  assert.equal(fresh.element("live-asr-model").value,"mlx:mlx-whisper-small");assert.equal(fresh.element("cancel-live-model-download").disabled,false);
  await fresh.element("cancel-live-model-download").emit("click");
  assert.deepEqual(fresh.requests.find(r=>r.url.endsWith("/download/cancel")).body,{backend:"mlx",model:"mlx-whisper-large-v3-turbo"});
  assert.equal(fresh.config.model,"mlx-whisper-small");

  // Saved Tiny still exposes a separate MLX task, rather than hiding its cancel UI.
  const tiny=fixture();tiny.backend="tiny";tiny.model="tiny";tiny.locked=true;
  const tinyPage=harness(tiny);tinyPage.setStatus({state:"ready",backend:"tiny",model:"tiny",active_download:active});await tinyPage.evaluate("controller.load()");
  assert.equal(tinyPage.element("live-model-download").hidden,false);assert.equal(tinyPage.element("cancel-live-model-download").disabled,false);
  await tinyPage.evaluate("controller.cancelDownload()");
  assert.deepEqual(tinyPage.requests.find(r=>r.url.endsWith("/download/cancel")).body,{backend:"mlx",model:"mlx-whisper-large-v3-turbo"});
  assert.equal(tinyPage.element("live-model-download").hidden,true);
}
(async () => {
  const tests = [initialSelectionDoesNotClaimToBeSaved, modelSelectionPersistsWithoutReset, independentSavePayloadAndFailurePreservesDraft, prepareSavesSelectedModelBeforeWarmup, modelListingUsesSavedConnectionAndAllowsEveryReturnedModel, activeTaskLocksAllMutationsAndModelFailureDoesNotFallback, reloadCancellationAndLockRefreshPreserveDrafts, successfulSaveSurvivesModelStatusFailureAndShowsBusyFeedback, preparationFailureDistinguishesSavedConfiguration, openingAndSelectingNeverDownloadAutomatically, explicitDownloadPreservesDraftAndNeverPrepares, downloadProgressCompletionAndCancellationNeedNoSave, tinyHidesDownloadsAndFailureAllowsUserRetry, switchingModelsIgnoresStaleResultsAndErrors, failedDownloadAndCancelRequestsKeepDraftsAndAllowRetry, reloadAndRefreshCanCancelAnotherModelsDownload];
  for (const test of tests) await test();
  console.log(`${tests.length} live settings UI scenarios passed`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
