// Run the real settings controller against a minimal DOM and a strict local API double.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "..", "static", "providers.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "providers.js"), "utf8")
  .replace(/^import .*\n/gm, "").replace(/^export /gm, "").replace(/\/\/ Bootstrap the standalone settings page\.[\s\S]*/, "");
const clone = (value) => JSON.parse(JSON.stringify(value));
const TASKS = ["file_asr", "correction", "translation"];
const SECTION_TASKS = { asr: ["file_asr"], text: ["correction", "translation"] };

class Element {
  constructor(id = "", tagName = "DIV") {
    Object.assign(this, { id, tagName: tagName.toUpperCase(), value: "", checked: false, disabled: false,
      hidden: false, dataset: {}, handlers: {}, children: [], attributes: {}, textContent: "", className: "" });
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].join(" ").trim(); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter((name) => !names.includes(name)).join(" "); },
      toggle: (name, force) => { const active = force === undefined ? !this.className.split(/\s+/).includes(name) : force;
        this.classList[active ? "add" : "remove"](name); return active; },
    };
  }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ target: this, preventDefault() {} }))); }
}

function fixture() {
  return {
    providers: [
      { id: "saved-asr", scope: "asr", name: "已保存 MiniMax 语音", vendor: "minimax", base_url: "https://speech.example/v1", text_api: "chat_completions", speech_api: "minimax", enabled: true, key_configured: true },
      { id: "saved-gpt", scope: "text", name: "已保存 GPT", vendor: "custom", base_url: "https://text.example/v1", text_api: "responses", speech_api: "none", enabled: true, key_configured: true },
      { id: "saved-anthropic", scope: "text", name: "已保存 Anthropic", vendor: "anthropic", base_url: "https://api.anthropic.com", text_api: "anthropic", anthropic_auth: "api_key", speech_api: "none", enabled: true, key_configured: true },
      { id: "legacy-shared", scope: "text", name: "旧版共用服务", vendor: "custom", base_url: "https://legacy.example/v1", text_api: "chat_completions", speech_api: "openai", enabled: true, key_configured: true },
      { id: "legacy-shared-asr", scope: "asr", name: "旧版共用服务 · 音频识别", vendor: "custom", base_url: "https://legacy.example/v1", text_api: "chat_completions", speech_api: "openai", enabled: true, key_configured: true },
    ],
    routes: { file_asr: { provider_id: "saved-asr", model: "", response_format: "verbose_json" }, correction: { provider_id: "saved-gpt", model: "" }, translation: { provider_id: "saved-gpt", model: "chosen-text-model" } },
    ready: { file_asr: false, correction: false, translation: true }, live: { mode: "local", model: "tiny" }, locked: false,
    presets: [
      { id: "custom", name: "自定义", text_api: "chat_completions", speech_api: "openai" },
      { id: "minimax", name: "MiniMax", suggested_base_url: "https://api.minimax.io/v1", text_api: "chat_completions", speech_api: "minimax", asr_model_hint: "asr-1.0", docs_url: "https://platform.minimax.io/docs/api-reference/speech-to-text" },
      { id: "minimax_anthropic", name: "MiniMax · Anthropic 文字", suggested_base_url: "https://api.minimax.io/anthropic", text_api: "anthropic", anthropic_auth: "bearer", speech_api: "none" },
      { id: "anthropic", name: "Anthropic", suggested_base_url: "https://api.anthropic.com", text_api: "anthropic", anthropic_auth: "api_key", speech_api: "none" },
    ],
  };
}
function deferred() { let resolve; const promise = new Promise((accept) => { resolve = accept; }); return { promise, resolve }; }

function harness(initial = fixture()) {
  const elements = new Map([...html.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)].map((match) => {
    const item = new Element(match[2], match[1]); item.hidden = /\bhidden(?:\s|>|=)/.test(match[0]); return [item.id, item];
  }));
  const element = (id) => { assert.ok(elements.has(id), `settings HTML must include #${id}`); return elements.get(id); };
  const requests = []; let config = clone(initial), failure, hold;
  const confirmations = [], historyChanges = [], navigationEvents = [];
  let confirmReply = true;
  const location = { href: "http://127.0.0.1:8877/static/providers.html?return_to=%2Fstatic%2Fimport.html", search: "?return_to=%2Fstatic%2Fimport.html" };
  const history = { pushState(_state, _title, value) { historyChanges.push(value); const url = new URL(value, location.href); location.href = url.href; location.search = url.search; } };
  let modelReply = () => ({ models: ["text-small", "asr-1.0", "embedding", "text-small"] });
  function savedProvider(value) {
    const previous = config.providers.find((item) => item.id === value.id) || {};
    const { api_key, clear_api_key, ...provider } = value;
    return { ...previous, ...provider, key_configured: Boolean(!clear_api_key && (api_key || previous.key_configured)) };
  }
  function updateReady() {
    config.ready = Object.fromEntries(TASKS.map((task) => {
      const route = config.routes[task], provider = config.providers.find((item) => item.id === route.provider_id);
      return [task, Boolean(provider?.enabled !== false && provider?.base_url && provider?.key_configured && route.model)];
    }));
  }
  async function request(url, options = {}) {
    const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method, body });
    if (failure?.url === url) { const message = failure.message; failure = null; throw new Error(message); }
    if (hold?.url === url) { const waiting = hold; hold = null; await waiting.promise; }
    if (url === "/api/service-config/models") return await modelReply(body);
    if (url === "/api/health") return { active: false, import_active: false };
    if (method === "PUT" && url === "/api/service-config/provider") {
      assert.ok(body.id && ["asr", "text", "shared"].includes(body.scope));
      assert.equal(Object.hasOwn(body, "providers"), false, "provider save must not submit a full configuration");
      assert.equal(Object.hasOwn(body, "routes"), false, "provider save must not submit task drafts");
      const saved = savedProvider(body), index = config.providers.findIndex((item) => item.id === body.id);
      if (index === -1) config.providers.push(saved); else config.providers[index] = saved;
      updateReady();
    } else if (method === "PUT" && url.startsWith("/api/service-config/section/")) {
      const scope = url.split("/").at(-1); assert.ok(SECTION_TASKS[scope]);
      assert.deepEqual(Object.keys(body.routes).sort(), [...SECTION_TASKS[scope]].sort(), "section saves only its own routes");
      assert.ok(body.providers.every((item) => item.scope === scope || item.scope === "shared"), "section cannot submit another module's providers");
      const next = body.providers.map(savedProvider);
      config.providers = config.providers.filter((item) => item.scope !== scope && !next.some((value) => value.id === item.id));
      config.providers.push(...next); config.routes = { ...config.routes, ...body.routes }; updateReady();
    } else { assert.equal(method, "GET", `unexpected mutation ${method} ${url}`); assert.equal(url, "/api/service-config"); }
    return clone(config);
  }
  const context = vm.createContext({ document: { getElementById: element, createElement: (tag) => new Element("", tag) }, Math, Set, Map, jsonRequest: request, URL, URLSearchParams, location, history, Event, window: { dispatchEvent(event) { navigationEvents.push(event.type); } }, confirm(message) { confirmations.push(message); return confirmReply; } });
  vm.runInContext(source + "\nvar controller = createServiceConfigUI();", context);
  const evaluate = (code) => vm.runInContext(code, context);
  const setInput = async (id, value) => { element(id).value = value; await element(id).emit("input"); };
  const setSelect = async (id, value) => { element(id).value = value; await element(id).emit("change"); };
  const text = (item) => [item.textContent, ...item.children.map(text)].join(" ");
  const selectProvider = async (name) => {
    const button = element("provider-list").children.find((item) => text(item).includes(name));
    assert.ok(button, `visible provider list must include ${name}`); await button.emit("click");
  };
  return { element, requests, evaluate, setInput, setSelect, text, selectProvider, confirmations, historyChanges, navigationEvents, get config() { return clone(config); },
    allowReload(value) { confirmReply = value; }, setServerLocked(value) { config.locked = value; },
    failNext(url) { failure = { url, message: "模拟保存失败，请重试" }; },
    holdNext(url) { const gate = deferred(); hold = { url, ...gate }; return gate; }, models(reply) { modelReply = reply; } };
}
const mutations = (test) => test.requests.filter((item) => item.method === "PUT" || (item.method === "POST" && item.url !== "/api/service-config/models"));
const values = (item) => item.children.map((child) => child.value);

async function navigationAndScope() {
  const t = harness(); await t.evaluate("controller.load()");
  assert.equal(t.evaluate("controller.section"), ""); assert.equal(t.element("service-overview").hidden, false);
  assert.equal(t.element("service-section").hidden, true); assert.match(t.element("service-live-note").textContent, /独立本机模型设置/);
  await t.element("open-asr-settings").emit("click"); assert.equal(t.evaluate("controller.section"), "asr");
  assert.equal(t.element("service-overview").hidden, true); assert.equal(t.element("service-section").hidden, false);
  for (const id of ["file-asr-step", "provider-speech-api-field", "provider-speech-endpoint-label"]) assert.equal(t.element(id).hidden, false);
  for (const id of ["correction-step", "translation-step", "provider-text-api-field", "provider-text-endpoint-label"]) assert.equal(t.element(id).hidden, true);
  const asrList = t.text(t.element("provider-list"));
  assert.match(asrList, /已保存 MiniMax 语音/); assert.match(asrList, /旧版共用服务/); assert.doesNotMatch(asrList, /已保存 GPT|已保存 Anthropic/);
  assert.deepEqual(values(t.element("file-asr-provider")).sort(), ["", "saved-asr", "legacy-shared-asr"].sort());
  assert.equal(values(t.element("provider-vendor")).includes("anthropic"), false);
  assert.equal(values(t.element("provider-vendor")).includes("minimax_anthropic"), false);
  await t.element("back-service-overview").emit("click"); assert.equal(t.evaluate("controller.section"), "");
  await t.element("open-text-settings").emit("click"); assert.equal(t.evaluate("controller.section"), "text");
  for (const id of ["correction-step", "translation-step", "provider-text-api-field", "provider-text-endpoint-label"]) assert.equal(t.element(id).hidden, false);
  for (const id of ["file-asr-step", "provider-speech-api-field", "provider-speech-endpoint-label"]) assert.equal(t.element(id).hidden, true);
  assert.doesNotMatch(t.text(t.element("provider-list")), /已保存 MiniMax 语音/);
  assert.deepEqual(values(t.element("correction-provider")).sort(), ["", "saved-gpt", "saved-anthropic", "legacy-shared"].sort());
  assert.equal(t.element("provider-api-key").value, "", "saved secrets are never echoed");
  assert.equal(t.element("correction-model").value, "", "translation model cannot fill unconfigured correction");
  assert.equal(t.element("correction-ready").dataset.ready, "false"); assert.equal(t.element("translation-ready").dataset.ready, "true");
}

async function providerSavePreservesAllOtherDrafts() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.setInput("provider-name", "ASR 未保存名称"); await t.setInput("provider-api-key", "private-asr-draft"); await t.setInput("file-asr-model", "asr-draft-model");
  await t.evaluate("controller.openSection('text')"); await t.selectProvider("已保存 GPT");
  await t.setInput("provider-name", "GPT 单连接保存"); await t.setInput("provider-api-key", "private-one-provider");
  await t.setInput("correction-model", "proofread-draft"); await t.setInput("translation-model", "translation-draft");
  await t.element("add-provider").emit("click"); await t.setInput("provider-name", "另一个文字连接草稿");
  await t.setInput("provider-base-url", "https://draft.example/v1"); await t.setInput("provider-api-key", "private-other-provider");
  await t.selectProvider("GPT 单连接保存"); await t.element("save-provider").emit("click");
  const saves = mutations(t); assert.equal(saves.length, 1); assert.equal(saves[0].url, "/api/service-config/provider");
  assert.equal(saves[0].body.id, "saved-gpt"); assert.equal(saves[0].body.scope, "text"); assert.equal(saves[0].body.api_key, "private-one-provider");
  assert.equal(t.config.routes.correction.model, ""); assert.equal(t.config.routes.translation.model, "chosen-text-model");
  assert.equal(t.element("provider-api-key").value, "", "only the successfully saved connection clears its secret input");
  assert.equal(t.element("correction-model").value, "proofread-draft"); assert.equal(t.element("translation-model").value, "translation-draft");
  await t.selectProvider("另一个文字连接草稿"); assert.equal(t.element("provider-api-key").value, "private-other-provider");
  assert.equal(t.element("provider-base-url").value, "https://draft.example/v1");
  await t.evaluate("controller.openSection('asr')");
  assert.equal(t.element("provider-name").value, "ASR 未保存名称"); assert.equal(t.element("provider-api-key").value, "private-asr-draft");
  assert.equal(t.element("file-asr-model").value, "asr-draft-model");
  assert.equal(t.config.providers.find((item) => item.id === "saved-asr").name, "已保存 MiniMax 语音"); assert.equal(t.evaluate("controller.dirty"), true);
}

async function sectionSavePreservesOtherModule() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.setInput("provider-name", "ASR 模块草稿"); await t.setInput("provider-api-key", "private-asr-pending"); await t.setInput("file-asr-model", "pending-asr-model");
  await t.evaluate("controller.openSection('text')"); await t.selectProvider("已保存 GPT");
  await t.setInput("provider-name", "文字模块已保存"); await t.setInput("provider-api-key", "private-text-save");
  await t.setInput("correction-model", "selected-proofreader"); await t.setInput("translation-model", "selected-translator");
  await t.element("service-config-form").emit("submit");
  const saved = mutations(t).at(-1); assert.equal(saved.url, "/api/service-config/section/text");
  assert.equal(saved.body.providers.some((item) => item.id === "saved-asr"), false); assert.deepEqual(Object.keys(saved.body.routes).sort(), ["correction", "translation"]);
  assert.equal(t.config.routes.file_asr.model, ""); assert.equal(t.config.routes.correction.model, "selected-proofreader");
  assert.equal(t.element("provider-api-key").value, ""); assert.equal(t.element("correction-ready").dataset.ready, "true");
  await t.evaluate("controller.openSection('asr')");
  assert.equal(t.element("provider-name").value, "ASR 模块草稿"); assert.equal(t.element("provider-api-key").value, "private-asr-pending");
  assert.equal(t.element("file-asr-model").value, "pending-asr-model"); await t.element("service-config-form").emit("submit");
  const asrSave = mutations(t).at(-1); assert.equal(asrSave.url, "/api/service-config/section/asr");
  assert.deepEqual(Object.keys(asrSave.body.routes), ["file_asr"]); assert.equal(asrSave.body.providers.some((item) => item.scope === "text"), false);
  assert.equal(t.config.routes.translation.model, "selected-translator");
}

async function failuresRetainDraftInputs() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.setInput("provider-name", "失败仍保留的名称"); await t.setInput("provider-base-url", "https://changed.example/v1");
  await t.setInput("provider-api-key", "private-failed-save"); await t.setInput("file-asr-model", "unsaved-asr-model");
  for (const [url, id, event] of [["/api/service-config/provider", "save-provider", "click"], ["/api/service-config/section/asr", "service-config-form", "submit"]]) {
    t.failNext(url); await t.element(id).emit(event);
    assert.equal(t.element("provider-name").value, "失败仍保留的名称"); assert.equal(t.element("provider-base-url").value, "https://changed.example/v1");
    assert.equal(t.element("provider-api-key").value, "private-failed-save"); assert.equal(t.element("file-asr-model").value, "unsaved-asr-model");
    assert.equal(t.element("service-status").dataset.level, "error"); assert.match(t.element("service-status").textContent, /模拟保存失败/);
    assert.equal(t.evaluate("controller.dirty"), true);
  }
  assert.equal(t.config.providers.find((item) => item.id === "saved-asr").name, "已保存 MiniMax 语音"); assert.equal(t.config.routes.file_asr.model, "");
}

async function reloadIsScopedAndClearingSecretsIsExplicit() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.setInput("provider-name", "重新读取时保留的 ASR 草稿"); await t.setInput("provider-api-key", "private-kept-asr-key");
  await t.setInput("file-asr-model", "kept-asr-model");
  await t.evaluate("controller.openSection('text')"); await t.selectProvider("已保存 GPT");
  await t.setInput("provider-name", "将被重新读取的文字草稿"); await t.setInput("provider-api-key", "private-reset-text-key");
  await t.setInput("correction-model", "reset-proofread-model");
  await t.element("reload-service-config").emit("click");
  assert.equal(t.element("provider-name").value, "已保存 GPT"); assert.equal(t.element("provider-api-key").value, "");
  assert.equal(t.element("correction-model").value, "");
  await t.evaluate("controller.openSection('asr')");
  assert.equal(t.element("provider-name").value, "重新读取时保留的 ASR 草稿");
  assert.equal(t.element("provider-api-key").value, "private-kept-asr-key"); assert.equal(t.element("file-asr-model").value, "kept-asr-model");
  t.element("provider-clear-key").checked = true; await t.element("provider-clear-key").emit("change");
  assert.equal(t.element("provider-api-key").disabled, true); assert.equal(t.element("provider-api-key").value, "");
  await t.element("save-provider").emit("click"); const saved = mutations(t).at(-1);
  assert.equal(saved.url, "/api/service-config/provider"); assert.equal(saved.body.clear_api_key, true);
  assert.equal(Object.hasOwn(saved.body, "api_key"), false, "explicit clear cannot submit a stale secret");
  assert.equal(t.config.providers.find((item) => item.id === "saved-asr").key_configured, false);
  assert.equal(t.element("file-asr-model").value, "kept-asr-model", "saving a connection cannot submit/reset task drafts");
}

async function allSavesLockWhileBusyOrRecording() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('text'))"); await t.setInput("provider-name", "保存中名称");
  for (const [url, id, event] of [["/api/service-config/provider", "save-provider", "click"], ["/api/service-config/section/text", "service-config-form", "submit"]]) {
    const gate = t.holdNext(url), pending = t.element(id).emit(event);
    for (const lockedId of ["save-provider", "save-section-top", "save-service-config", "add-provider", "reload-service-config"]) assert.equal(t.element(lockedId).disabled, true);
    const busyButton = t.element(id === "save-provider" ? id : "save-service-config");
    assert.equal(busyButton.getAttribute("aria-busy"), "true"); assert.match(busyButton.textContent, /正在保存/);
    const before = mutations(t).length; await t.evaluate("controller.saveProvider(); controller.save()");
    assert.equal(mutations(t).length, before, "busy guards block synthetic duplicate submissions too"); gate.resolve(); await pending;
  }
  t.evaluate("controller.setLocked(true)");
  for (const id of ["save-provider", "save-section-top", "save-service-config", "correction-fetch-models"]) assert.equal(t.element(id).disabled, true);
  const before = mutations(t).length; await t.element("save-provider").emit("click"); await t.element("service-config-form").emit("submit");
  assert.equal(mutations(t).length, before, "active tasks prevent every mutation endpoint"); t.evaluate("controller.setLocked(false)");
}

async function reloadCancellationAndHistoryPreserveDrafts() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.setInput("provider-name", "保留的语音草稿"); await t.setInput("provider-api-key", "fixture-unsaved-key");
  t.allowReload(false); const requestsBefore = t.requests.length;
  await t.element("reload-service-config").emit("click");
  assert.equal(t.requests.length, requestsBefore, "cancelled reload must not read or overwrite a draft");
  assert.equal(t.element("provider-name").value, "保留的语音草稿"); assert.equal(t.element("provider-api-key").value, "fixture-unsaved-key");
  assert.match(t.confirmations.at(-1), /ASR 音频识别/); assert.match(t.confirmations.at(-1), /密钥/);
  assert.match(t.historyChanges.at(-1), /section=asr/); assert.match(t.historyChanges.at(-1), /return_to=/);
  assert.deepEqual(t.navigationEvents, ["yakiniku:navigation-change"]);
  const entriesBefore = t.historyChanges.length;
  await t.evaluate("controller.openSection('', {historyMode:'none'})");
  assert.equal(t.element("service-overview").hidden, false); assert.equal(t.historyChanges.length, entriesBefore);
  await t.evaluate("controller.openSection('asr', {historyMode:'none'})");
  assert.equal(t.element("provider-name").value, "保留的语音草稿"); assert.equal(t.element("provider-api-key").value, "fixture-unsaved-key");
  assert.equal(t.historyChanges.length, entriesBefore, "restoring browser history does not create a new entry");
}

async function lockRefreshNeverOverwritesDraftAndErrorsReachSavebar() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('text'))");
  await t.setInput("provider-name", "保留的文字草稿");
  t.setServerLocked(true); await t.evaluate("controller.refreshLock()");
  assert.equal(t.element("save-service-config").disabled, true);
  assert.equal(t.element("provider-name").value, "保留的文字草稿");
  t.setServerLocked(false); await t.evaluate("controller.refreshLock()");
  assert.equal(t.element("save-service-config").disabled, false); assert.equal(t.evaluate("controller.dirty"), true);
  t.failNext("/api/service-config/section/text"); await t.evaluate("controller.save()");
  assert.match(t.element("service-save-state").textContent, /修改仍保留/);
  assert.match(t.element("service-save-help").textContent, /模拟保存失败/);
  assert.equal(t.element("save-service-config").getAttribute("aria-busy"), "false");
}

async function legacyConnectionsAreIndependent() {
  const initial = fixture(); for (const task of TASKS) initial.routes[task].provider_id = task === "file_asr" ? "legacy-shared-asr" : "legacy-shared";
  const t = harness(initial); await t.evaluate("controller.load().then(() => controller.openSection('asr'))"); await t.selectProvider("旧版共用服务");
  assert.equal(t.element("provider-shared-note").hidden, true);
  assert.equal(t.element("remove-provider").disabled, false, "ASR no longer edits a shared text connection");
  await t.setInput("provider-name", "独立音频连接"); await t.element("save-provider").emit("click"); assert.equal(mutations(t).at(-1).body.scope, "asr");
  assert.equal(t.config.routes.file_asr.provider_id, "legacy-shared-asr");
  for (const task of ["correction", "translation"]) assert.equal(t.config.routes[task].provider_id, "legacy-shared");
  await t.element("service-config-form").emit("submit"); assert.equal(mutations(t).at(-1).body.providers.find((item) => item.id === "legacy-shared-asr").scope, "asr");
  await t.evaluate("controller.openSection('text')"); assert.equal(values(t.element("correction-provider")).includes("legacy-shared"), true);
  assert.equal(values(t.element("correction-provider")).includes("legacy-shared-asr"), false);
  await t.selectProvider("旧版共用服务"); assert.equal(t.element("remove-provider").disabled, false);
  assert.equal(t.element("provider-speech-api").value, "openai", "a scoped editor must not rewrite a legacy shared protocol");
  assert.equal(t.element("provider-name").value, "旧版共用服务", "ASR names never overwrite the text copy");
}

async function oldSharedResponsesAreSplitWithoutSelectingModels() {
  const initial = fixture(); initial.providers = initial.providers.filter((p) => p.id !== "legacy-shared-asr");
  const legacy = initial.providers.find((p) => p.id === "legacy-shared"); legacy.scope = "shared";
  for (const task of TASKS) initial.routes[task].provider_id = legacy.id;
  const t = harness(initial); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  assert.equal(values(t.element("file-asr-provider")).includes("legacy-shared"), false);
  assert.equal(t.element("file-asr-provider").value, "legacy-shared-asr");
  assert.equal(t.element("file-asr-model").value, "");
  await t.evaluate("controller.openSection('text')");
  assert.equal(t.element("translation-provider").value, "legacy-shared");
  assert.equal(values(t.element("translation-provider")).includes("legacy-shared-asr"), false);
  assert.equal(t.element("translation-model").value, "chosen-text-model");
  assert.equal(mutations(t).length, 0, "defensive read normalization performs no save");
}

async function legacySplitUsesStableDistinctIdsWhenCloneNameIsTaken() {
  const initial = fixture(), legacy = initial.providers.find((p) => p.id === "legacy-shared");
  legacy.scope = "shared";
  initial.routes.file_asr.provider_id = legacy.id;
  const t = harness(initial); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  const choices = values(t.element("file-asr-provider"));
  assert.equal(new Set(choices).size, choices.length, "a pre-existing clone ID must never be reused");
  assert.equal(t.element("file-asr-provider").value, "legacy-shared-asr-2");
  assert.ok(choices.includes("legacy-shared-asr"), "unrelated existing connections survive migration");
  await t.evaluate("controller.load()");
  assert.equal(t.element("file-asr-provider").value, "legacy-shared-asr-2", "read-only normalization is deterministic");
}

async function modelsUseDraftConnectionsWithoutGuessingModels() {
  const t = harness(); await t.evaluate("controller.load().then(() => controller.openSection('asr'))"); await t.element("add-provider").emit("click");
  const newId = values(t.element("file-asr-provider")).find((id) => id.startsWith("provider-")); assert.match(newId, /^provider-[a-z0-9]+$/);
  await t.setSelect("provider-vendor", "minimax"); assert.equal(t.element("provider-base-url").value, ""); assert.equal(t.element("provider-api-key").value, "");
  assert.equal(t.element("provider-speech-api").value, "minimax"); await t.element("use-provider-address").emit("click");
  assert.equal(t.element("provider-speech-endpoint").textContent, "https://api.minimax.io/v1/speech_to_text");
  await t.setInput("provider-name", "新 MiniMax 识别"); await t.setInput("provider-api-key", "private-new-asr"); await t.setSelect("file-asr-provider", newId);
  assert.match(t.element("file-asr-model-hint").textContent, /asr-1.0/); await t.element("file-asr-fetch-models").emit("click");
  const nativeCall = t.requests.filter((item) => item.url === "/api/service-config/models").at(-1);
  assert.equal(nativeCall.body.base_url, "https://api.minimax.io/v1"); assert.equal(nativeCall.body.api_key, "private-new-asr"); assert.equal(Object.hasOwn(nativeCall.body, "provider_id"), false);
  assert.equal(t.element("file-asr-model").value, "", "fetching a list never auto-selects the ASR model");
  assert.equal(t.element("file-asr-model-list").children.length, 4, "all model types retained, duplicates removed");
  await t.setInput("file-asr-model-search", "asr"); assert.equal(t.element("file-asr-model-list").children.length, 2);
  await t.setSelect("file-asr-model-list", "asr-1.0"); assert.equal(t.element("file-asr-model").value, "asr-1.0");
  t.models(() => ({ models: [] })); await t.element("file-asr-fetch-models").emit("click");
  assert.match(t.element("file-asr-model-status").textContent, /手动填写/);
  assert.equal(t.element("file-asr-model").value, "asr-1.0", "empty discovery cannot erase the explicitly selected ASR model");
  const stale = deferred(); t.models(() => stale.promise); const pending = t.element("file-asr-fetch-models").emit("click");
  for (const id of ["save-provider", "save-section-top", "save-service-config"]) assert.equal(t.element(id).disabled, true);
  await t.setInput("provider-base-url", "https://different.example/v1"); stale.resolve({ models: ["stale-model"] }); await pending;
  assert.equal(t.element("file-asr-model-list").hidden, true, "stale model responses cannot populate changed connections");
  t.models(() => ({ models: ["fixture-text-model"] })); await t.evaluate("controller.openSection('text')"); await t.selectProvider("已保存 Anthropic");
  assert.equal(t.element("provider-anthropic-auth-field").hidden, false); assert.equal(t.element("provider-text-endpoint").textContent, "https://api.anthropic.com/v1/messages");
  await t.setInput("provider-api-key", "private-anthropic-draft"); await t.setSelect("provider-anthropic-auth", "bearer");
  await t.setSelect("correction-provider", "saved-anthropic"); await t.element("correction-fetch-models").emit("click");
  const anthropicCall = t.requests.filter((item) => item.url === "/api/service-config/models").at(-1);
  assert.equal(anthropicCall.body.provider_id, "saved-anthropic"); assert.equal(anthropicCall.body.text_api, "anthropic");
  assert.equal(anthropicCall.body.anthropic_auth, "bearer"); assert.equal(anthropicCall.body.api_key, "private-anthropic-draft");
  assert.equal(t.element("correction-model").value, "", "model discovery must not replace task model input");
  await t.setInput("correction-model", "manual-proofreader"); t.models(() => { throw new Error("模拟模型接口不可用"); });
  await t.element("correction-fetch-models").emit("click");
  assert.equal(t.element("correction-model").value, "manual-proofreader"); assert.equal(t.element("correction-model-status").dataset.level, "error");
  assert.match(t.element("correction-model-status").textContent, /手动填写/);
  await t.element("add-provider").emit("click"); await t.setSelect("provider-vendor", "minimax_anthropic");
  assert.equal(t.element("provider-text-api").value, "anthropic"); assert.equal(t.element("provider-anthropic-auth").value, "bearer");
  assert.equal(t.element("provider-base-url").value, ""); await t.element("use-provider-address").emit("click");
  assert.equal(t.element("provider-text-endpoint").textContent, "https://api.minimax.io/anthropic/v1/messages");
  await t.setSelect("provider-vendor", "anthropic"); assert.equal(t.element("provider-anthropic-auth").value, "api_key");
  assert.equal(t.element("provider-base-url").value, "https://api.minimax.io/anthropic", "presets cannot silently overwrite configured addresses");
}

async function legacyAsrDiscoveryIgnoresHiddenAnthropicProtocol() {
  const initial = fixture(), legacy = initial.providers.find((item) => item.id === "legacy-shared-asr");
  Object.assign(legacy, { vendor: "minimax_anthropic", text_api: "anthropic", anthropic_auth: "bearer", speech_api: "minimax" });
  initial.routes.file_asr.provider_id = legacy.id;
  const t = harness(initial); await t.evaluate("controller.load().then(() => controller.openSection('asr'))");
  await t.selectProvider("旧版共用服务");
  assert.equal(t.element("provider-text-api-field").hidden, true);
  assert.equal(t.element("provider-text-api").value, "anthropic", "opening ASR cannot rewrite a hidden legacy text protocol");
  assert.equal(t.element("provider-speech-api").value, "minimax");
  assert.equal(t.element("use-provider-address").disabled, true, "an old text preset must not offer its /anthropic URL in ASR");
  await t.element("use-provider-address").emit("click");
  assert.equal(t.element("provider-base-url").value, "https://legacy.example/v1");
  const oldOption = t.element("provider-vendor").children.find((item) => item.value === "minimax_anthropic");
  if (oldOption) assert.match(oldOption.textContent, /旧|文字|不适用|重新/, "retained old text presets need an explicit warning");
  await t.element("file-asr-fetch-models").emit("click");
  const discovery = t.requests.filter((item) => item.url === "/api/service-config/models").at(-1);
  assert.equal(discovery.body.provider_id, "legacy-shared-asr");
  assert.equal(discovery.body.text_api, "chat_completions", "ASR uses /models + Bearer regardless of the hidden text protocol");
  await t.element("save-provider").emit("click");
  const saved = mutations(t).at(-1);
  assert.equal(saved.body.text_api, "anthropic"); assert.equal(saved.body.anthropic_auth, "bearer");
  assert.equal(saved.body.vendor, "minimax_anthropic"); assert.equal(saved.body.speech_api, "minimax");
}

(async () => {
  for (const test of [navigationAndScope, providerSavePreservesAllOtherDrafts, sectionSavePreservesOtherModule,
    failuresRetainDraftInputs, reloadIsScopedAndClearingSecretsIsExplicit, allSavesLockWhileBusyOrRecording,
    reloadCancellationAndHistoryPreserveDrafts, lockRefreshNeverOverwritesDraftAndErrorsReachSavebar,
    legacyConnectionsAreIndependent, oldSharedResponsesAreSplitWithoutSelectingModels,
    legacySplitUsesStableDistinctIdsWhenCloneNameIsTaken, modelsUseDraftConnectionsWithoutGuessingModels,
    legacyAsrDiscoveryIgnoresHiddenAnthropicProtocol]) {
    await test(); console.log(`${test.name}: passed`);
  }
  console.log("Provider UI tests passed: separate modules, scoped saves, draft isolation, errors, locks, legacy connections and model discovery.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
