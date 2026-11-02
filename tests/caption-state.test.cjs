// Exercise the actual frontend event handlers with a minimal DOM. No copied
// caption-ordering implementation, browser permission, or cloud API is needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor() {
    this.textContent = "";
    this.childNodes = [];
    this.dataset = {};
    this.classList = { toggle() {}, add() {} };
  }
  cloneNode() { return new Element(); }
  replaceChildren() {}
  append() {}
  addEventListener() {}
  getBoundingClientRect() { return { width: 100, height: 60 }; }
  getContext() {
    return { setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} };
  }
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElement() { return new Element(); },
  createDocumentFragment() { return new Element(); },
};
document.getElementById("recordings-list").firstElementChild = new Element();
const context = vm.createContext({
  document,
  window: { devicePixelRatio: 1, addEventListener() {} },
  location: { origin: "http://127.0.0.1:8765" },
  requestAnimationFrame() {},
  AbortController,
  setTimeout, clearTimeout,
  fetch: async (url) => ({ ok: true, json: async () => url === "/api/settings" ? { key_configured: false } : [] }),
});
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
vm.runInContext(source, context);
vm.runInContext("capture = {}; phase = 'recording';", context);
function emit(message) {
  context.testMessage = message;
  vm.runInContext("handleProviderMessage(capture, testMessage)", context);
}
function caption() { return document.getElementById("live-ja").textContent; }
function final(id, start, ja, zh = "") {
  emit({ type: zh ? "translation" : "transcript", segment: { id, start_ms: start, end_ms: start + 1000, ja, zh, translation_status: zh ? "done" : "pending" } });
}

emit({ type: "partial", id: "turn-1", start_ms: null, ja: "第一句试听" });
assert.equal(caption(), "第一句试听");
assert.equal(vm.runInContext("latestStart", context), -1, "unknown timestamps must not create a false time zero");
final("turn-1", 1000, "第一句原文", "第一句译文");
assert.equal(caption(), "第一句原文");

emit({ type: "partial", id: "turn-2", start_ms: null, ja: "第二句正在说" });
assert.equal(caption(), "第二句正在说", "second-turn precommit partial must remain visible after a nonzero first-turn timestamp");
assert.equal(vm.runInContext("latestStart", context), 1000, "precommit partial must retain the last real timestamp");
final("turn-1", 1000, "第一句原文", "晚到的第一句译文");
assert.equal(caption(), "第二句正在说", "older translation must not erase the next uncommitted turn");
final("turn-2", 3000, "第二句完整原文");
assert.equal(caption(), "第二句完整原文", "commit for the displayed partial must reconcile normally");

emit({ type: "partial", id: "turn-3", start_ms: 5000, ja: "第三句正在说" });
assert.equal(caption(), "第三句正在说");
emit({ type: "partial", id: "turn-2", start_ms: 3000, ja: "迟到的第二句片段" });
assert.equal(caption(), "第三句正在说", "old timed partial must not replace the newer turn");
final("turn-2", 3000, "第二句完整原文", "晚到的第二句译文");
assert.equal(caption(), "第三句正在说", "older final must not replace a newer uncommitted partial");
emit({ type: "partial", id: "turn-3", start_ms: undefined, ja: "第三句继续补全" });
assert.equal(caption(), "第三句继续补全", "missing timestamp must behave like an uncommitted null timestamp");
assert.equal(vm.runInContext("latestStart", context), 5000);
final("turn-3", 5000, "第三句完整原文", "第三句译文");
assert.equal(document.getElementById("live-zh").textContent, "第三句译文");

vm.runInContext("resetTranscript(); capture.recognitionMode = 'local';", context);
final("local-1", 0, "第一句原文");
final("local-2", 3000, "第二句原文");
final("local-1", 0, "第一句校正", "第一句中文");
assert.equal(caption(), "第一句校正", "a completed local translation must become visible even after newer recognition");
assert.equal(document.getElementById("live-zh").textContent, "第一句中文");
final("local-3", 6000, "第三句原文");
assert.equal(document.getElementById("live-zh").textContent, "第一句中文", "new pending recognition must not erase readable Chinese");
final("local-3", 6000, "第三句校正", "第三句中文");
final("local-2", 3000, "第二句校正", "第二句中文");
assert.equal(caption(), "第三句校正", "out-of-order completion must not rewind the local subtitle");
assert.equal(document.getElementById("live-zh").textContent, "第三句中文", "Japanese and Chinese must remain paired");
assert.equal(vm.runInContext("segments.get('local-2').zh", context), "第二句中文", "late translations must remain in the transcript");
vm.runInContext("resetTranscript();", context);
final("local-4", 0, "新场原文");
assert.equal(caption(), "新场原文", "new recording must reset translated caption timing");

console.log("Caption state tests passed: unknown-time partials, late translations, and timed ordering.");
