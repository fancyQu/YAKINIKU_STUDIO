// Exercise the actual isolated cloud-ASR settings controller, including
// secret-preserving saves, provider drafts, model selection, and start readiness.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
class Element {
  constructor(id = "") { this.id = id; this.value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.dataset = {}; this.handlers = {}; this.children = []; this.textContent = ""; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  replaceChildren(...children) { this.children = children; }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ target: this, preventDefault() {} }))); }
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
let settings = {
  import_transcription_base_url: "", import_effective_base_url: "https://example.test/v1",
  import_transcription_model: "", import_transcription_response_format: "verbose_json",
  import_transcription_key_configured: false, import_transcription_has_own_key: false,
  import_configuration_ready: false, translation_key_configured: true,
};
const requests = [];
let pendingModels = null;
let confirmReload = true, failSave = false;
const confirmations = [];
const context = vm.createContext({
  document: { getElementById: element, createElement: () => new Element() },
  window: { confirm(message) { confirmations.push(message); return confirmReload; } },
  jsonRequest: async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, body });
    if (url === "/api/settings/models") return pendingModels ? await pendingModels : { models: ["text-only-model", "voice-model", "voice-model"] };
    if (options.method === "POST") {
      if (failSave) throw new Error("识别设置保存失败，请重试。");
      settings = { ...settings, ...body, import_transcription_key_configured: true, import_transcription_has_own_key: !body.clear_import_transcription_api_key, import_configuration_ready: true };
    }
    return { ...settings };
  },
});
const source = fs.readFileSync(path.join(__dirname, "..", "static", "import-settings.js"), "utf8").replace(/^import .*\n/gm, "").replace(/^export /gm, "");
vm.runInContext(source + "\nvar controller = createImportSettings();", context);
const evaluate = (code) => vm.runInContext(code, context);
(async () => {
  await evaluate("controller.load()");
  assert.equal(element("import-asr-model").value, "", "no guessed ASR model");
  assert.equal(element("import-api-key").value, "", "secret never echoed");
  assert.equal(element("import-api-base-url").value, "", "shared fallback keeps editable field blank");
  assert.match(element("import-effective-base").textContent, /继承共用地址/);
  assert.equal(evaluate("controller.ready"), false);
  element("import-api-base-url").value = "https://speech.example.test/v1"; await element("import-api-base-url").emit("input");
  element("import-api-key").value = "example-key"; await element("import-api-key").emit("input");
  await element("fetch-import-models").emit("click");
  const modelRequest = requests.find((request) => request.url === "/api/settings/models").body;
  assert.equal(modelRequest.role, "import_transcription");
  assert.equal(modelRequest.import_transcription_api_key, "example-key");
  assert.equal(modelRequest.import_transcription_base_url, "https://speech.example.test/v1");
  assert.equal(element("import-model-list").children.length, 3, "all models retained, duplicates removed");
  element("import-model-search").value = "voice"; await element("import-model-search").emit("input");
  assert.equal(element("import-model-list").children.length, 2);
  element("import-model-list").value = "voice-model"; await element("import-model-list").emit("change");
  assert.equal(element("import-asr-model").value, "voice-model");
  assert.equal(evaluate("controller.dirty"), true);
  assert.equal(evaluate("controller.ready"), false, "unsaved draft cannot start import");
  confirmReload = false;
  const requestCount = requests.length;
  await element("reload-import-settings").emit("click");
  assert.equal(requests.length, requestCount, "cancelled reload issues no request");
  assert.equal(element("import-api-key").value, "example-key", "cancelled reload preserves an unsaved key");
  assert.equal(element("import-asr-model").value, "voice-model");
  assert.match(confirmations.at(-1), /放弃.*未保存/);
  assert.match(element("reload-import-settings").textContent, /放弃修改/);
  confirmReload = true;
  await element("import-settings-form").emit("submit");
  assert.equal(evaluate("controller.ready"), true);
  assert.equal(element("import-api-key").value, "");
  element("import-response-format").value = "json"; await element("import-response-format").emit("change");
  assert.match(element("import-format-help").textContent, /按文本估算/);
  await element("import-settings-form").emit("submit");
  const saves = requests.filter((request) => request.url === "/api/import-settings" && request.body);
  assert.equal(Object.hasOwn(saves.at(-1).body, "import_transcription_api_key"), false, "blank key preserves saved key");
  assert.equal(saves.at(-1).body.import_transcription_response_format, "json");
  element("clear-import-api-key").checked = true; await element("clear-import-api-key").emit("change");
  assert.equal(element("import-api-key").disabled, true);
  await element("import-settings-form").emit("submit");
  assert.equal(requests.at(-1).body.clear_import_transcription_api_key, true);
  evaluate("controller.setLocked(true)");
  assert.equal(element("save-import-settings").disabled, true);
  assert.equal(element("fetch-import-models").disabled, true);
  evaluate("controller.setLocked(false)");
  assert.equal(element("save-import-settings").disabled, false);
  let resolveModels;
  pendingModels = new Promise((resolve) => { resolveModels = resolve; });
  const pending = element("fetch-import-models").emit("click");
  assert.equal(element("fetch-import-models").attributes["aria-busy"], "true");
  element("import-api-base-url").value = "https://new.example.test"; await element("import-api-base-url").emit("input");
  resolveModels({ models: ["stale-provider-model"] });
  await pending;
  assert.equal(element("import-model-list").children.length, 1, "stale provider result discarded");
  assert.equal(element("fetch-import-models").attributes["aria-busy"], "false");
  failSave = true; await element("import-settings-form").emit("submit"); failSave = false;
  assert.equal(evaluate("controller.dirty"), true);
  assert.equal(element("import-api-base-url").value, "https://new.example.test");
  assert.equal(element("import-settings-status").dataset.level, "error");
  assert.equal(element("save-import-settings").textContent, "保存识别 API 设置");
  assert.equal(element("save-import-settings").attributes["aria-busy"], "false");
  await element("reload-import-settings").emit("click");
  assert.equal(evaluate("controller.dirty"), false, "confirmed reload restores saved settings");
  assert.equal(element("import-api-base-url").value, settings.import_transcription_base_url);
  console.log("Cloud import settings tests passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
