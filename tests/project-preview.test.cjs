// Test the production timeline rules used by audio preview, including
// silence gaps, exact subtitle boundaries, and multi-part seek offsets.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "..", "static", "project-utils.js"), "utf8").replace(/^export /gm, "");
const context = vm.createContext({ fetch: async () => ({ ok: true, json: async () => ({}) }) });
vm.runInContext(source, context);
function evaluate(expression) { return vm.runInContext(expression, context); }
context.testSegments = [
  { id: "second", start_ms: 2000, end_ms: 3000, ja: "次", zh: "下一句" },
  { id: "first", start_ms: 0, end_ms: 1000, ja: "初め", zh: "开始" },
  { id: "bad", start_ms: 7000, end_ms: 6000 },
];
evaluate("var cues = normalizedSegments(testSegments)");
assert.equal(evaluate("cues.length"), 2);
assert.equal(evaluate("cues[0].id"), "first");
assert.equal(evaluate("cueAt(cues, -1)"), -1);
assert.equal(evaluate("cueAt(cues, 0)"), 0);
assert.equal(evaluate("cueAt(cues, 999.9)"), 0);
assert.equal(evaluate("cueAt(cues, 1000)"), -1);
assert.equal(evaluate("cueAt(cues, 1500)"), -1);
assert.equal(evaluate("cueAt(cues, 2000)"), 1);
assert.equal(evaluate("cueAt(cues, 3000)"), -1);
assert.deepEqual(Array.from(evaluate("subtitleLines(cues[0], 'ja')")), ["初め"]);
assert.deepEqual(Array.from(evaluate("subtitleLines(cues[0], 'zh')")), ["开始"]);
assert.deepEqual(Array.from(evaluate("subtitleLines(cues[0], 'bilingual')")), ["开始", "初め"]);
assert.deepEqual(Array.from(evaluate("subtitleLines({ja:'原文'}, 'zh')")), ["中文翻译尚未完成"]);
context.testProject = { duration_ms: 90000, files: [{ name: "audio.wav" }, { name: "audio-002.wav" }], audio_parts: [
  { name: "audio.wav", start_ms: 0, end_ms: 60000 },
  { name: "audio-002.wav", start_ms: 60000, end_ms: 90000 },
  { name: "../../escape.wav", start_ms: 90000, end_ms: 100000 },
] };
evaluate("var parts = audioParts(testProject)");
assert.equal(evaluate("parts.length"), 2);
assert.equal(evaluate("partForTime(parts, 59999)"), 0);
assert.equal(evaluate("partForTime(parts, 60000)"), 1);
assert.equal(evaluate("partForTime(parts, 75000)"), 1);
assert.equal(evaluate("(75000 - parts[partForTime(parts, 75000)].start_ms) / 1000"), 15);
assert.equal(evaluate("partForTime(parts, 90000)"), -1);
assert.equal(evaluate("projectBusy({import_stage:'completed'})"), false);
assert.equal(evaluate("projectBusy({import_stage:'transcribing'})"), true);
assert.equal(evaluate("projectFileURL('a', 'source.mp3')"), "/api/sessions/a/files/source.mp3");
assert.equal(evaluate("clockTime(25*3600*1000)"), "25:00:00");
console.log("Audio project preview timeline tests passed.");
