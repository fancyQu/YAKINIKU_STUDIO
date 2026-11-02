// Verify routing form behavior and the actual settings payload without a browser
// or sending any credential, audio, or text to a third-party service.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor() {
    this.textContent = ""; this.value = ""; this.checked = false;
    this.childNodes = []; this.dataset = {}; this.disabled = false;
    this.listeners = new Map();
    this.classList = { toggle() {}, add() {} };
  }
  cloneNode() { return new Element(); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  append(...nodes) { for (const node of nodes) this.childNodes.push(...(node.isFragment ? node.childNodes : [node])); }
  addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(listener); }
  dispatch(type, event = {}) { for (const listener of this.listeners.get(type) || []) listener({ target: this, stopPropagation() {}, ...event }); }
  getBoundingClientRect() { return { width: 100, height: 60 }; }
  getContext() { return { setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; }
}
const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) { const element = new Element(); element.id = id; elements.set(id, element); } return elements.get(id); },
  createElement() { return new Element(); },
  createDocumentFragment() { const fragment = new Element(); fragment.isFragment = true; return fragment; },
};
document.getElementById("recordings-list").firstElementChild = new Element();
const baseSettings = {
  api_base_url: "", key_configured: false,
  shared_key_configured: false, key_from_environment: false,
  transcription_mode: "realtime", translation_api: "responses",
  transcription_model: "", translation_model: "gpt-4.1-mini",
  segment_seconds: 5, glossary: "",
  transcription_base_url: "", translation_base_url: "",
  transcription_has_own_key: false, translation_has_own_key: false,
};
const requests = [];
let serverSettings = { ...baseSettings };
let modelHandler = async () => ({ ok: true, json: async () => ({ models: ["text-model-a", "text-model-b", "whisper-1", "image-model", "<img src=x onerror=alert(1)>"] }) });
const context = vm.createContext({
  document, window: { devicePixelRatio: 1, addEventListener() {} },
  location: { origin: "http://127.0.0.1:8765" },
  requestAnimationFrame() {}, AbortController, URL, setTimeout, clearTimeout,
  fetch: async (url, options) => {
    requests.push({ url, options });
    if (url === "/api/settings/models") return modelHandler(JSON.parse(options.body));
    let result;
    if (url === "/api/settings/test") result = { transcription: { ok: true, message: "鉴权通过，模型列表可读取" }, translation: { ok: true, message: "鉴权通过，模型列表可读取" } };
    else if (url === "/api/settings") {
      if (options?.method === "POST") {
        const payload = JSON.parse(options.body);
        serverSettings = { ...serverSettings, ...payload };
        // The server never returns plaintext credentials to the browser.
        for (const key of ["api_key", "transcription_api_key", "translation_api_key"]) delete serverSettings[key];
      }
      result = { ...serverSettings };
    } else result = [];
    return { ok: true, json: async () => result };
  },
});
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function run(source) { return vm.runInContext(source, context); }
const get = (id) => document.getElementById(id);

(async () => {
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  assert.equal(get("provider-preset").value, "official", "interface preset describes the selected protocols without filling a service address");
  assert.equal(get("api-base-url").value, "", "new installations must not prefill any service address");
  assert.equal(get("api-key").value, "");
  assert.equal(get("transcription-model").value, "", "new installations must not assume a speech model is available");
  assert.equal(get("test-connection").disabled, false);

  get("api-key").value = "entered-but-never-sent";
  get("provider-preset").value = "official";
  run("applyProviderPreset()");
  assert.equal(get("api-base-url").value, "", "realtime preset must not inject an official address");
  get("provider-preset").value = "custom";
  run("applyProviderPreset()");
  assert.equal(get("api-base-url").value, "", "compatible preset must not inject a service address");
  assert.equal(get("api-key").value, "entered-but-never-sent", "interface-only presets must preserve the user's entered credential");
  assert.equal(get("transcription-mode").value, "segmented");
  assert.equal(get("translation-api").value, "chat_completions");
  assert.equal(get("transcription-model").value, "", "presets must not inject an unverified speech model");
  assert.equal(get("translation-model").value, "gpt-4.1-mini", "interface presets preserve the user model instead of replacing it");
  assert.equal(get("test-connection").disabled, true, "diagnostics must not test stale saved settings after editing");
  assert.match(get("test-connection-help").textContent, /先点击下方「保存设置」/);
  const before = requests.length;
  await run("saveSettings({preventDefault(){}})");
  assert.equal(requests.length, before, "blank custom base must not silently fall back to official");
  assert.match(get("settings-status").textContent, /填写共用 API 地址/);

  get("api-base-url").value = "https://chosen.example/v1";
  get("provider-preset").value = "official";
  run("applyProviderPreset()");
  assert.equal(get("api-base-url").value, "https://chosen.example/v1", "realtime preset must preserve the user's address");
  get("provider-preset").value = "custom";
  run("applyProviderPreset()");
  assert.equal(get("api-base-url").value, "https://chosen.example/v1", "compatible preset must preserve the user's address");

  get("transcription-model").value = "manually-chosen-speech-model";
  get("translation-model").value = "manually-chosen-text-model";
  get("provider-preset").value = "official";
  run("applyProviderPreset()");
  assert.equal(get("transcription-model").value, "manually-chosen-speech-model");
  assert.equal(get("translation-model").value, "manually-chosen-text-model");
  get("provider-preset").value = "custom";
  run("applyProviderPreset()");

  get("api-base-url").value = "https://relay.example/v1";
  get("api-key").value = "test-only-shared-key";
  get("translation-model").value = "platform/model-alias";
  get("transcription-base-url").value = "https://speech.example/v1";
  get("transcription-api-key").value = "test-only-speech-key";
  get("translation-base-url").value = "https://text.example/v1";
  get("translation-api-key").value = "";
  get("clear-translation-api-key").checked = true;
  get("segment-seconds").value = "4";
  await run("saveSettings({preventDefault(){}})");
  const save = requests.findLast((item) => item.url === "/api/settings" && item.options.method === "POST");
  const payload = JSON.parse(save.options.body);
  assert.equal(payload.api_base_url, "https://relay.example/v1");
  assert.equal(payload.transcription_base_url, "https://speech.example/v1");
  assert.equal(payload.translation_base_url, "https://text.example/v1");
  assert.equal(payload.transcription_mode, "segmented");
  assert.equal(payload.translation_api, "chat_completions");
  assert.equal(payload.segment_seconds, 4);
  assert.equal(payload.translation_model, "platform/model-alias");
  assert.equal(payload.clear_translation_api_key, true);
  assert.equal(payload.translation_api_key, "", "blank role secret must remain blank so the server can preserve or clear it explicitly");
  assert.equal(get("api-key").value, "");
  assert.equal(get("transcription-api-key").value, "");
  assert.equal(get("translation-api-key").value, "");
  assert.equal(get("test-connection").disabled, false, "saving restores the configuration check button");
  assert.match(get("test-connection-help").textContent, /检查已保存/);
  assert.match(get("api-destinations").textContent, /直播音频 → https:\/\/speech.example\/v1/);
  assert.match(get("api-destinations").textContent, /日语文本 → https:\/\/text.example\/v1/);

  await run("testConnection()");
  const diagnostic = requests.findLast((item) => item.url === "/api/settings/test");
  assert.equal(diagnostic.options.body, "{}", "configuration test must use saved settings without resending credentials");
  assert.match(get("settings-status").textContent, /不代替实际语音测试/);

  context.testSettings = { ...baseSettings, api_base_url: "https://saved.example/v1", key_from_environment: true, transcription_has_own_key: true, translation_has_own_key: true };
  run("settings = testSettings; fillSettings()");
  assert.equal(get("api-base-url").value, "https://saved.example/v1", "explicitly saved user address must still be displayed");
  assert.match(get("api-destinations").textContent, /直播音频 → https:\/\/saved.example\/v1/);
  assert.match(get("api-destinations").textContent, /日语文本 → https:\/\/saved.example\/v1/);
  assert.match(get("transcription-api-key").placeholder, /已配置独立密钥/);
  assert.equal(get("clear-transcription-api-key").disabled, false);
  assert.match(get("settings-status").textContent, /独立密钥/);
  assert.equal(get("api-key").value, "");

  context.testSettings = { ...baseSettings };
  run("settings = testSettings; fillSettings()");
  get("api-base-url").value = "https://draft.example/v1";
  get("api-key").value = "draft-test-only-key";
  await run("saveSettings({preventDefault(){}})");
  assert.equal(JSON.parse(requests.findLast((item) => item.url === "/api/settings" && item.options.method === "POST").options.body).transcription_model, "", "connection settings may be saved before choosing a speech model");
  assert.match(get("settings-status").textContent, /尚未配置语音识别模型/);
  get("api-base-url").value = "https://draft.example/v1";
  get("api-key").value = "draft-test-only-key";
  get("translation-base-url").value = "https://text-draft.example/v1";
  get("translation-api-key").value = "draft-test-only-role-key";
  get("clear-translation-api-key").checked = true;
  get("translation-model").value = "previously-chosen-model";
  run("markSettingsDirty()");
  const savedBeforeModels = requests.filter((item) => item.url === "/api/settings" && item.options.method === "POST").length;
  await run("fetchTranslationModels()");
  const modelRequest = requests.findLast((item) => item.url === "/api/settings/models");
  assert.deepEqual(JSON.parse(modelRequest.options.body), {
    api_base_url: "https://draft.example/v1", api_key: "draft-test-only-key",
    translation_base_url: "https://text-draft.example/v1", translation_api_key: "draft-test-only-role-key", clear_translation_api_key: true,
  }, "fetch uses current draft routing, not stale saved settings");
  assert.equal(requests.filter((item) => item.url === "/api/settings" && item.options.method === "POST").length, savedBeforeModels, "fetch must not save draft settings");
  assert.equal(get("api-key").value, "draft-test-only-key", "fetch must preserve entered credentials");
  assert.equal(get("translation-model").value, "previously-chosen-model", "fetch must not silently select the first model");
  assert.equal(get("translation-model-list").childNodes.length, 6, "every returned model must be selectable, including non-text IDs");
  assert.equal(get("translation-model-list").childNodes.at(-1).textContent, "<img src=x onerror=alert(1)>", "upstream IDs must be rendered as text");
  assert.equal(get("translation-model-list").disabled, false);
  assert.match(get("translation-model-status").textContent, /5 个模型/);

  get("translation-model-search").value = "MODEL-B";
  get("translation-model-search").dispatch("input");
  assert.equal(get("translation-model-list").childNodes.length, 2);
  get("translation-model-list").value = "text-model-b";
  get("translation-model-list").dispatch("change");
  assert.equal(get("translation-model").value, "text-model-b");
  await run("saveSettings({preventDefault(){}})");
  assert.equal(JSON.parse(requests.findLast((item) => item.url === "/api/settings" && item.options.method === "POST").options.body).translation_model, "text-model-b", "chosen model must be included in saved configuration");
  assert.equal(get("translation-model-list").disabled, true, "saving invalidates the old draft list");

  modelHandler = async () => ({ ok: false, status: 401, json: async () => ({ detail: "鉴权失败，请检查密钥。" }) });
  await run("fetchTranslationModels()");
  assert.match(get("translation-model-status").textContent, /鉴权失败/);
  assert.equal(get("translation-model").value, "text-model-b", "failure must preserve the selected model and manual fallback");
  assert.equal(get("fetch-translation-models").disabled, false);

  modelHandler = async () => ({ ok: true, json: async () => ({ models: [] }) });
  await run("fetchTranslationModels()");
  assert.match(get("translation-model-status").textContent, /列表为空/);
  assert.equal(get("translation-model-list").disabled, true);

  // Speech discovery uses its own endpoint configuration and cannot overwrite
  // a translation model or borrow the translation role's draft credentials.
  get("transcription-base-url").value = "https://speech-draft.example/v1";
  get("transcription-api-key").value = "draft-speech-only-key";
  get("clear-transcription-api-key").checked = false;
  get("transcription-model").value = "saved-but-unlisted-speech-model";
  modelHandler = async (payload) => ({ ok: true, json: async () => ({ models: payload.role === "transcription" ? ["speech-model-a", "speech-model-b", "text-only-model"] : ["text-model-b"] }) });
  await run("fetchTranscriptionModels()");
  const speechRequest = requests.findLast((item) => item.url === "/api/settings/models");
  const speechPayload = JSON.parse(speechRequest.options.body);
  assert.equal(speechPayload.role, "transcription");
  assert.equal(speechPayload.transcription_base_url, "https://speech-draft.example/v1");
  assert.equal(speechPayload.transcription_api_key, "draft-speech-only-key");
  assert.equal(speechPayload.translation_api_key, undefined);
  assert.equal(get("transcription-model").value, "saved-but-unlisted-speech-model", "speech discovery must not silently select a returned model");
  assert.match(get("transcription-model-status").textContent, /未在列表中/);
  assert.equal(get("transcription-model-list").childNodes.length, 4, "show the full provider list without guessing speech capability from IDs");
  get("transcription-model-search").value = "MODEL-B";
  get("transcription-model-search").dispatch("input");
  assert.equal(get("transcription-model-list").childNodes.length, 2);
  get("transcription-model-list").value = "speech-model-b";
  get("transcription-model-list").dispatch("change");
  assert.equal(get("transcription-model").value, "speech-model-b");
  assert.equal(get("translation-model").value, "text-model-b");
  await run("fetchTranslationModels()");
  assert.equal(get("translation-model-list").childNodes.length, 2);
  get("transcription-base-url").value = "https://another-speech.example/v1";
  run("markSettingsDirty({target: {id: 'transcription-base-url'}})");
  assert.equal(get("transcription-model-list").childNodes.length, 1, "speech source change invalidates only the speech list");
  assert.equal(get("translation-model-list").childNodes.length, 2, "translation discovery is independent of speech source changes");
  await run("fetchTranscriptionModels()");
  run("markSettingsDirty({target: {id: 'api-key'}})");
  assert.equal(get("transcription-model-list").childNodes.length, 1);
  assert.equal(get("translation-model-list").childNodes.length, 1, "shared credential change invalidates both lists");
  await run("saveSettings({preventDefault(){}})");
  assert.equal(JSON.parse(requests.findLast((item) => item.url === "/api/settings" && item.options.method === "POST").options.body).transcription_model, "speech-model-b");

  let completeModels;
  modelHandler = () => new Promise((resolve) => { completeModels = resolve; });
  const staleRequest = run("fetchTranslationModels()");
  assert.equal(get("fetch-translation-models").disabled, true, "pending fetch must disable duplicate requests");
  assert.match(get("test-connection-help").textContent, /正在获取模型/);
  get("translation-base-url").value = "https://new-service.example/v1";
  run("markSettingsDirty({target: {id: 'translation-base-url'}})");
  completeModels({ ok: true, json: async () => ({ models: ["stale-model"] }) });
  await staleRequest;
  assert.equal(get("translation-model-list").childNodes.length, 1, "a previous service's late response must be ignored");
  assert.match(get("translation-model-status").textContent, /重新获取/);

  const closedRequest = run("fetchTranslationModels()");
  get("settings-dialog").dispatch("close");
  run("fillSettings()");
  completeModels({ ok: true, json: async () => ({ models: ["closed-dialog-model"] }) });
  await closedRequest;
  assert.equal(get("translation-model-list").disabled, true, "reopened settings must not receive a previous dialog's response");
  assert.equal(get("translation-model").value, "text-model-b");

  context.testSettings = { ...baseSettings, transcription_mode: "local", transcription_model: "tiny", translation_model: "chosen-gpt", api_base_url: "https://text.example/v1", translation_key_configured: true, configuration_ready: true };
  run("settings = testSettings; fillSettings()");
  assert.equal(get("provider-preset").value, "local");
  assert.equal(get("transcription-mode").value, "local");
  assert.equal(get("transcription-model").value, "tiny");
  assert.equal(get("local-model-settings").hidden, false);
  assert.equal(get("transcription-model-picker").hidden, true);
  assert.equal(get("transcription-api-settings").hidden, true);
  assert.match(get("api-destinations").textContent, /直播音频 → 本机识别（不上传）/);
  assert.match(get("api-destinations").textContent, /GPT 校正与中文翻译/);
  const beforeLocalSpeech = requests.length;
  await run("fetchTranscriptionModels()");
  assert.equal(requests.length, beforeLocalSpeech, "local speech must not query the cloud speech service");
  get("provider-preset").value = "custom";
  run("applyProviderPreset()");
  assert.equal(get("transcription-model").value, "", "switching to cloud must not send the local tiny identifier as a cloud model");
  get("provider-preset").value = "local";
  run("applyProviderPreset()");
  assert.equal(get("transcription-model").value, "tiny");
  assert.equal(get("translation-model").value, "chosen-gpt", "local preset preserves the chosen GPT model");
  await run("saveSettings({preventDefault(){}})");
  const localSave = JSON.parse(requests.findLast((item) => item.url === "/api/settings" && item.options.method === "POST").options.body);
  assert.equal(localSave.transcription_mode, "local");
  assert.equal(localSave.transcription_model, "tiny");

  const requestCount = requests.length;
  run("phase = 'recording'; updateControls()");
  await run("fetchTranslationModels()");
  await run("fetchTranscriptionModels()");
  assert.equal(requests.length, requestCount, "recording must disable both roles of model discovery");
  assert.equal(get("fetch-translation-models").disabled, true);
  assert.equal(get("fetch-transcription-models").disabled, true);
  assert.match(get("test-connection-help").textContent, /结束当前录制/);

  console.log("Settings UI tests passed: settings routing, safe model discovery, filtering, selection, failures, and stale-request handling.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
