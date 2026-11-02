// Verify the real import-page controls cannot upload with missing or unsaved
// cloud settings, and that a valid saved configuration sends the selected file.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor() { this.value = ""; this.hidden = false; this.disabled = false; this.dataset = {}; this.handlers = {}; this.children = []; this.textContent = ""; this.attributes = {}; this.classList = { add() {}, remove() {} }; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  async emit(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({ preventDefault() {} }))); }
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let onSettingsChange;
let onSubtitleChange;
const state = { ready: false, configured: false, loaded: true, dirty: false, busy: false, setLocked() {}, async load() { onSettingsChange(); } };
const subtitleState = { ready: true, loaded: true, dirty: false, busy: false, locked: false, setLocked() {}, async load() { onSubtitleChange(); } };
const uploads = [];
const listeners = {}, requests = [];
let projects = [], listGate = null, retryGate = null, listFail = false;
class FakeXHR {
  constructor() { this.upload = {}; this.headers = {}; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(name, value) { this.headers[name] = value; }
  send(file) { uploads.push({ file, headers: this.headers, url: this.url }); this.status = 202; this.responseText = '{"id":"new-project"}'; this.onload(); }
}
const context = vm.createContext({
  document: { getElementById: element, createElement: () => new Element(), createDocumentFragment: () => new Element() },
  window: { addEventListener(type, listener) { listeners[type] = listener; } },
  createImportSettings: ({ onChange }) => { onSettingsChange = onChange; return state; },
  createSubtitleSettings: ({ onChange }) => { onSubtitleChange = onChange; return subtitleState; },
  jsonRequest: async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/retry")) { if (retryGate) await retryGate; return {}; }
    if (listGate) await listGate;
    if (listFail) throw new Error("项目列表读取失败，请重试。");
    return projects;
  }, IMPORT_STAGES: {}, projectBusy: () => false,
  clockTime: () => "00:00:00", projectFileURL: () => "",
  XMLHttpRequest: FakeXHR, setTimeout, clearTimeout,
});
const source = fs.readFileSync(path.join(__dirname, "..", "static", "imports.js"), "utf8").replace(/^import .*\n/gm, "");
vm.runInContext(source, context);
(async () => {
  await new Promise(setImmediate);
  context.testFile = { name: "日语音频.wav", size: 1024 };
  vm.runInContext("selectFile(testFile)", context);
  assert.equal(element("start-import").disabled, true);
  assert.match(element("start-import-help").textContent, /完成上方语音识别 API 设置/);
  await element("import-form").emit("submit");
  assert.equal(uploads.length, 0, "missing configuration cannot upload");
  state.ready = true; state.configured = true; onSettingsChange();
  assert.equal(element("start-import").disabled, false);
  state.dirty = true; state.ready = false; onSettingsChange();
  assert.equal(element("start-import").disabled, true);
  assert.match(element("start-import-help").textContent, /先保存/);
  await element("import-form").emit("submit");
  assert.equal(uploads.length, 0, "unsaved configuration cannot upload");
  state.dirty = false; state.ready = true; onSettingsChange();
  subtitleState.dirty = true; subtitleState.ready = false; onSubtitleChange();
  assert.equal(element("start-import").disabled, true);
  assert.match(element("start-import-help").textContent, /保存分句设置/);
  await element("import-form").emit("submit");
  assert.equal(uploads.length, 0, "unsaved segmentation cannot upload");
  subtitleState.dirty = false; subtitleState.ready = true; onSubtitleChange();
  element("import-title").value = "项目名称";
  await element("import-form").emit("submit");
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].url, "/api/imports");
  assert.equal(uploads[0].headers["X-Filename"], encodeURIComponent("日语音频.wav"));
  assert.equal(uploads[0].headers["X-Title"], encodeURIComponent("项目名称"));
  assert.equal(uploads[0].file, context.testFile);
  assert.equal(element("start-import").disabled, true, "successful upload clears file selection");
  vm.runInContext("selectFile(testFile)", context);
  context.badFile = { name: "bad.txt", size: 10 };
  vm.runInContext("selectFile(badFile)", context);
  assert.equal(element("start-import").disabled, true, "invalid file removes previous valid selection");

  const unloadPrevented = () => { let prevented = false; const event = { preventDefault() { prevented = true; } }; listeners.beforeunload(event); return prevented; };
  assert.equal(unloadPrevented(), false, "a clean import page can be left without a warning");
  state.dirty = true; assert.equal(unloadPrevented(), true, "legacy ASR drafts are protected"); state.dirty = false;
  subtitleState.dirty = true; assert.equal(unloadPrevented(), true, "subtitle drafts are protected"); subtitleState.dirty = false;
  element("import-title").value = "尚未提交的项目"; assert.equal(unloadPrevented(), true, "an unsubmitted title is protected"); element("import-title").value = "";
  vm.runInContext("selectFile(testFile)", context); assert.equal(unloadPrevented(), true, "a selected file is protected");
  vm.runInContext("selectFile(badFile)", context);

  let releaseList;
  listGate = new Promise((resolve) => { releaseList = resolve; });
  const refreshing = element("refresh-imports").emit("click");
  assert.equal(element("refresh-imports").disabled, true);
  assert.equal(element("refresh-imports").attributes["aria-busy"], "true");
  assert.match(element("refresh-imports").textContent, /刷新中/);
  const beforeDuplicate = requests.length;
  await element("refresh-imports").emit("click");
  assert.equal(requests.length, beforeDuplicate, "duplicate refresh does not issue another request");
  releaseList(); await refreshing; listGate = null;
  assert.equal(element("refresh-imports").disabled, false);
  assert.equal(element("refresh-imports").attributes["aria-busy"], "false");
  assert.match(element("import-notice").textContent, /列表已更新/);
  listFail = true; await element("refresh-imports").emit("click"); listFail = false;
  vm.runInContext("clearTimeout(pollTimer)", context);
  assert.equal(element("import-notice").dataset.level, "error");
  assert.equal(element("refresh-imports").disabled, false, "a failed refresh can be retried");

  // Resuming a completed ASR checkpoint must not require configuring ASR again.
  state.ready = false; state.configured = false;
  projects = [{ id: "translation-only", status: "completed", import_stage: "failed", source_type: "import", failed_translation_count: 1 }];
  context.testProjects = projects; vm.runInContext("render(testProjects)", context);
  const walk = (node) => [node, ...node.children.flatMap(walk)];
  const retry = walk(element("import-projects")).find((node) => node.textContent === "继续处理");
  assert.ok(retry); assert.equal(retry.disabled, false);
  let releaseRetry; retryGate = new Promise((resolve) => { releaseRetry = resolve; });
  const resuming = retry.emit("click");
  assert.equal(retry.disabled, true); assert.match(retry.textContent, /正在继续/);
  await retry.emit("click");
  assert.equal(requests.filter(({ url }) => url.endsWith("/retry")).length, 1, "repeat clicks cannot duplicate resume");
  releaseRetry(); await resuming; retryGate = null;
  assert.match(element("import-notice").textContent, /已继续处理/);
  subtitleState.locked = true; onSubtitleChange();
  const lockedRetry = walk(element("import-projects")).find((node) => node.textContent === "继续处理");
  assert.equal(lockedRetry.disabled, true, "another active task locks resume");
  await lockedRetry.emit("click");
  assert.equal(requests.filter(({ url }) => url.endsWith("/retry")).length, 1);
  subtitleState.locked = false; onSubtitleChange();
  listFail = true; await lockedRetry.emit("click"); listFail = false;
  vm.runInContext("clearTimeout(pollTimer)", context);
  assert.match(element("import-notice").textContent, /已继续处理.*列表暂时无法读取/, "a post-success list failure never says the resume itself failed");
  assert.equal(element("import-notice").dataset.level, "success");
  console.log("Cloud import upload controls tests passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
