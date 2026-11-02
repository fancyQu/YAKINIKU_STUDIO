const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Exercise the production page's playback and click handlers with a list
// positioned below the audio player. The window must never be scrolled.
const pageWindow = { scrollY: 137 };
const classList = () => {
  const values = new Set();
  return { add: (value) => values.add(value), remove: (value) => values.delete(value),
    contains: (value) => values.has(value), toggle(value, enabled) { enabled ? values.add(value) : values.delete(value); } };
};
class Element {
  constructor(name = "") {
    this.name = name; this.children = []; this.attributes = {}; this.events = new Map();
    this.classList = classList(); this.dataset = {}; this.hidden = false;
    this.scrollTop = 0; this.clientTop = 1; this.clientHeight = 200; this.scrollHeight = 600;
    this.checked = true; this.paused = false; this.currentTime = 0; this.readyState = 1;
    this.scrollCalls = []; this.focusCalls = [];
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) { for (const child of children) { this.children.push(child); child.parent = this; } }
  replaceChildren(...children) {
    this.children = [];
    for (const child of children) this.append(...(child.name === "fragment" ? child.children : [child]));
    this.children.forEach((child, index) => { child.rowOffset = index * 150; });
  }
  contains(row) { return this.children.includes(row); }
  getBoundingClientRect() {
    const top = this.parent?.name === "preview-timeline" ? 501 + this.rowOffset - this.parent.scrollTop : 500;
    const height = this.parent?.name === "preview-timeline" ? 150 : this.clientHeight + 2;
    return { top, bottom: top + height, height };
  }
  scrollTo(options) { this.scrollCalls.push(options); this.scrollTop = options.top; }
  scrollIntoView() { pageWindow.scrollY = 999; throw new Error("Page scroll regression: scrollIntoView called"); }
  focus(options) { this.focusCalls.push(options); if (!options?.preventScroll) pageWindow.scrollY = 999; }
  addEventListener(type, handler) { if (!this.events.has(type)) this.events.set(type, []); this.events.get(type).push(handler); }
  dispatch(type, event = {}) { for (const handler of this.events.get(type) || []) handler(event); }
  play() { this.paused = false; return Promise.resolve(); }
  load() {}
}
const ids = new Map();
function element(id) { if (!ids.has(id)) ids.set(id, new Element(id)); return ids.get(id); }
const languageButtons = ["ja", "zh", "bilingual"].map((language) => { const button = new Element(); button.dataset.language = language; return button; });
const document = {
  getElementById: element,
  createElement: (tag) => new Element(tag),
  createDocumentFragment: () => new Element("fragment"),
  querySelectorAll: () => languageButtons,
};
const context = vm.createContext({ document, window: pageWindow, location: { search: "" }, URLSearchParams,
  setTimeout, clearTimeout, AbortController, fetch: () => { throw new Error("Unexpected network request"); } });
const root = path.join(__dirname, "..");
const utilitySource = fs.readFileSync(path.join(root, "static/project-utils.js"), "utf8").replace(/^export /gm, "");
const previewSource = fs.readFileSync(path.join(root, "static/preview.js"), "utf8").replace(/^import .*\n/, "");
vm.runInContext(utilitySource + "\n" + previewSource, context);
vm.runInContext(`
  project = { status: 'completed' };
  parts = [{ name: 'audio.wav', start_ms: 0, end_ms: 8000 }];
  segments = Array.from({length: 4}, (_, i) => ({id: String(i), start_ms: i*2000, end_ms: i*2000+1800, ja:'原文',zh:'译文'}));
  audio.setAttribute('src', projectFileURL(identifier, 'audio.wav'));
  renderTimeline();
`, context);
const timeline = element("preview-timeline");
const audio = element("preview-audio");
audio.currentTime = 4.5;
audio.dispatch("timeupdate");
assert.equal(timeline.scrollTop, 262, "follow the third cue inside the list, keeping a 12 px lower margin");
assert.equal(timeline.scrollCalls.length, 1);
assert.equal(pageWindow.scrollY, 137, "playback cannot move the outer page");
assert.equal(timeline.children[2].attributes["aria-current"], "true");
audio.dispatch("timeupdate");
assert.equal(timeline.scrollCalls.length, 1, "unchanged cue does not keep scrolling");

// A cue already fully visible causes no scroll. Language changes use the
// same containment path and do not move a page positioned above the list.
languageButtons[0].dispatch("click");
assert.equal(timeline.scrollCalls.length, 1);
assert.equal(element("preview-zh").hidden, true);
assert.equal(pageWindow.scrollY, 137);

// Pointer focus and seek preserve the page position. Seeking backwards
// scrolls just the list so the requested first cue is visible again.
let prevented = false;
const firstRow = timeline.children[0];
firstRow.dispatch("pointerdown", { button: 0, preventDefault() { prevented = true; } });
firstRow.dispatch("click");
assert.equal(prevented, true);
assert.ok(firstRow.focusCalls.every((options) => options.preventScroll === true));
assert.equal(audio.currentTime, 0);
assert.equal(timeline.scrollTop, 0);
assert.equal(pageWindow.scrollY, 137, "click-to-seek cannot move the outer page");

// Manual scrolling pauses follow; enabling it explicitly resumes local
// following, while oversized rows align at their beginning and clamp safely.
timeline.dispatch("wheel");
assert.equal(element("follow-playback").checked, false);
audio.currentTime = 6.5;
audio.dispatch("timeupdate");
assert.equal(timeline.scrollTop, 0);
element("follow-playback").checked = true;
element("follow-playback").dispatch("change");
assert.equal(timeline.scrollTop, 400, "last cue is clamped to the list's maximum scroll position");
assert.equal(pageWindow.scrollY, 137);

audio.paused = true;
timeline.scrollTop = 0;
audio.currentTime = 4.5;
audio.dispatch("seeked");
assert.equal(timeline.scrollTop, 0, "paused seek highlights without following");
assert.equal(pageWindow.scrollY, 137);
console.log("Preview scroll tests passed: playback, language switch, seek and manual follow stay inside the subtitle list.");
