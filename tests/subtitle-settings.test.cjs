const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor() { this.value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.textContent = ""; this.dataset = {}; this.handlers = {}; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ preventDefault() {} }))); }
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let stored = { enabled: true, max_chars: 24, max_seconds: 6, locked: false };
let failSave = false;
let resolveSave;
let pendingSave = false;
const requests = [];
const timers = [];
let confirmReload = true;
const confirmations = [];
const context = vm.createContext({
  document: { getElementById: element },
  window: { confirm(message) { confirmations.push(message); return confirmReload; } },
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  jsonRequest: async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method: options.method || "GET", body });
    assert.equal(url, "/api/subtitle-settings");
    if (options.method === "PUT") {
      if (failSave) throw new Error("保存失败，请重试");
      if (pendingSave) await new Promise((resolve) => { resolveSave = resolve; });
      stored = { ...body, locked: false };
    }
    return { ...stored };
  },
});
const source = fs.readFileSync(path.join(__dirname, "..", "static", "subtitle-settings.js"), "utf8").replace(/^import .*\n/gm, "").replace(/^export /gm, "");
vm.runInContext(source + "\nvar controller = createSubtitleSettings();", context);
const evaluate = (code) => vm.runInContext(code, context);

(async () => {
  await evaluate("controller.load()");
  assert.equal(element("subtitle-max-chars").value, "24");
  assert.equal(element("subtitle-max-seconds").value, "6");
  assert.equal(element("subtitle-split-enabled").checked, true);
  assert.equal(evaluate("controller.ready"), true);
  assert.equal(element("save-subtitle-settings").disabled, true, "saved state has no unsaved change");

  element("subtitle-max-chars").value = "18"; await element("subtitle-max-chars").emit("input");
  element("subtitle-max-seconds").value = "4"; await element("subtitle-max-seconds").emit("input");
  assert.equal(evaluate("controller.dirty"), true);
  assert.equal(evaluate("controller.ready"), false);
  await element("subtitle-settings-form").emit("submit");
  assert.deepEqual(requests.at(-1), { url: "/api/subtitle-settings", method: "PUT", body: { enabled: true, max_chars: 18, max_seconds: 4 } });
  assert.equal(evaluate("controller.dirty"), false);
  assert.match(element("subtitle-settings-status").textContent, /18 字.*4 秒/);

  element("subtitle-max-chars").value = "20"; await element("subtitle-max-chars").emit("input");
  failSave = true;
  await element("subtitle-settings-form").emit("submit");
  assert.equal(element("subtitle-max-chars").value, "20", "save failure retains input");
  assert.equal(evaluate("controller.dirty"), true);
  assert.equal(element("subtitle-settings-status").dataset.level, "error");
  failSave = false;
  confirmReload = false;
  const beforeCancelReload = requests.length;
  await element("reload-subtitle-settings").emit("click");
  assert.equal(requests.length, beforeCancelReload);
  assert.equal(element("subtitle-max-chars").value, "20", "cancelled reload retains subtitle draft");
  assert.match(confirmations.at(-1), /放弃.*未保存/);
  assert.match(element("reload-subtitle-settings").textContent, /放弃修改/);
  confirmReload = true;
  await element("reload-subtitle-settings").emit("click");
  assert.equal(element("subtitle-max-chars").value, "18", "reload uses persisted value");

  for (const value of ["7", "81", "10.5", ""]) {
    element("subtitle-max-chars").value = value; await element("subtitle-max-chars").emit("input");
    const count = requests.length;
    await element("subtitle-settings-form").emit("submit");
    assert.equal(requests.length, count, "invalid char limit makes no request");
    assert.equal(element("subtitle-settings-status").dataset.level, "error");
  }
  element("subtitle-max-chars").value = "18"; await element("subtitle-max-chars").emit("input");
  for (const value of ["1", "16", "", "NaN"]) {
    element("subtitle-max-seconds").value = value; await element("subtitle-max-seconds").emit("input");
    const count = requests.length;
    await element("subtitle-settings-form").emit("submit");
    assert.equal(requests.length, count, "invalid time limit makes no request");
  }
  element("subtitle-max-seconds").value = "5"; await element("subtitle-max-seconds").emit("input");
  pendingSave = true;
  const pending = element("subtitle-settings-form").emit("submit");
  assert.equal(element("subtitle-max-chars").disabled, true, "busy request disables editing");
  assert.equal(element("save-subtitle-settings").disabled, true);
  assert.equal(element("save-subtitle-settings").attributes["aria-busy"], "true");
  assert.match(element("save-subtitle-settings").textContent, /正在保存/);
  resolveSave(); await pending; pendingSave = false;
  assert.equal(element("save-subtitle-settings").attributes["aria-busy"], "false");

  element("subtitle-split-enabled").checked = false; await element("subtitle-split-enabled").emit("change");
  assert.equal(element("subtitle-max-chars").disabled, true);
  await element("subtitle-settings-form").emit("submit");
  assert.equal(requests.at(-1).body.enabled, false);
  assert.match(element("subtitle-settings-status").textContent, /保留识别服务原始分句/);

  evaluate("controller.setLocked(true)");
  assert.equal(element("subtitle-split-enabled").disabled, true);
  assert.equal(element("reload-subtitle-settings").disabled, true);
  evaluate("controller.setLocked(false)");
  assert.equal(element("subtitle-split-enabled").disabled, false);
  element("subtitle-max-chars").value = "20"; await element("subtitle-max-chars").emit("input");
  for (const reason of ["active", "import_active", "subtitle_active", "model_preparing", "reservations"]) {
    stored = { enabled: true, max_chars: 30, max_seconds: 8, locked: true };
    await timers.shift()();
    assert.equal(evaluate("controller.locked"), true, `${reason} locks using the authoritative settings endpoint`);
    assert.equal(element("subtitle-split-enabled").disabled, true);
    assert.equal(element("subtitle-max-chars").value, "20", "lock polling preserves unsaved input");
    assert.equal(evaluate("controller.dirty"), true);
    stored.locked = false;
    await timers.shift()();
    assert.equal(evaluate("controller.locked"), false);
  }
  assert.equal(requests.some(({ url }) => url === "/api/health"), false, "health omits recording reservations and must not drive settings locks");

  const html = fs.readFileSync(path.join(__dirname, "..", "static", "import.html"), "utf8");
  assert.match(html, /<\/section>\s*<section id="subtitle-settings"/, "segmentation card is independent of hidden legacy API card");
  assert.match(html, /id="save-subtitle-settings"/);
  assert.equal(requests.some(({ url }) => url.includes("/api/imports") || url.includes("/retry") || url.includes("/refine")), false, "settings do not upload or reprocess audio");
  console.log("Subtitle settings UI tests passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
