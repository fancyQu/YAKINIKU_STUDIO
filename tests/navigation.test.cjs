const assert = require("node:assert/strict");
const { createNavigation } = require("../static/navigation.js");
const fs = require("node:fs");
const path = require("node:path");

function fixture(path = "/static/import.html", { referrer = "", mounted = true } = {}) {
  class Element {
    constructor(tag = "div") { this.tagName = tag; this.dataset = {}; this.attributes = {}; this.children = []; this.textContent = ""; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return name === "href" ? this.href || null : this.attributes[name] ?? null; }
    hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this.renders = (this.renders || 0) + 1; }
    closest() { return this.tagName === "a" ? this : null; }
  }
  const mount = new Element("nav"), returnLink = new Element("a");
  const listeners = {}, scrolls = [], storage = new Map();
  const host = {
    location: new URL(path, "http://127.0.0.1:8877"), scrollY: 247,
    sessionStorage: { setItem: (key, value) => storage.set(key, value), getItem: (key) => storage.get(key) ?? null },
    requestAnimationFrame: (callback) => callback(), scrollTo: (options) => scrolls.push(options),
    addEventListener: (name, callback) => { listeners[name] = callback; },
    document: {
      referrer, createElement: (tag) => new Element(tag),
      querySelector: () => mounted ? mount : null,
      querySelectorAll: () => [returnLink],
      addEventListener: (name, callback) => { listeners[name] = callback; },
    },
  };
  const navigation = createNavigation(host);
  return { navigation, host, mount, returnLink, listeners, scrolls, storage, Element };
}

{
  const { navigation } = fixture();
  for (const raw of [null, "", "https://external.example/", "//external.example/static/import.html", "javascript:alert(1)", "http://name:secret@127.0.0.1:8877/static/import.html", "/api/settings", "/static/preview.html", "/static/preview.html?id=<script>", "/static/../api/settings", "/static/\\evil.example", "/\n/static/import.html"]) assert.equal(navigation.safeURL(raw), null, String(raw));
  assert.equal(navigation.safeURL("/static/providers.html?section=asr&api_key=private&draft=secret#service-section"), "/static/providers.html?section=asr#service-section");
  assert.equal(navigation.withReturnTo("/api/sessions/id/files/ja.srt"), "/api/sessions/id/files/ja.srt");
  assert.equal(navigation.withReturnTo("#subtitle-settings"), "#subtitle-settings");
}
{
  const start = fixture("/#recordings").navigation;
  const previewURL = start.withReturnTo("/static/preview.html?id=project-1");
  const preview = fixture(previewURL).navigation;
  assert.equal(preview.sourceURL(), "/#recordings");
  const configURL = preview.withReturnTo("/static/providers.html?section=asr");
  const config = fixture(configURL).navigation;
  assert.equal(config.sourceURL(), previewURL);
  assert.equal(config.withReturnTo("/#workspace"), "/#workspace", "ancestor menu navigation must not create a return loop");
  assert.equal(new URL(config.withReturnTo("/static/live-settings.html"), "http://127.0.0.1:8877").searchParams.get("return_to"), configURL);
}
{
  const badSource = fixture("/static/providers.html?return_to=" + encodeURIComponent("https://external.example/static/import.html"));
  assert.equal(badSource.navigation.sourceURL(), "/static/import.html");
  const safeReferrer = fixture("/static/live-settings.html", { referrer: "http://127.0.0.1:8877/static/providers.html?section=text" });
  assert.equal(safeReferrer.navigation.sourceURL(), "/static/providers.html?section=text");
  const externalReferrer = fixture("/static/live-settings.html", { referrer: "https://external.example/" });
  assert.equal(externalReferrer.navigation.sourceURL(), "/#workspace");
}
{
  const ui = fixture("/static/providers.html?section=text&return_to=" + encodeURIComponent("/static/preview.html?id=p1"));
  ui.navigation.install();
  assert.equal(ui.mount.children[0].children[0].textContent, "← 返回项目预览");
  assert.equal(ui.mount.children[1].children.find((item) => item.textContent === "API 配置").attributes["aria-current"], "page");
  assert.equal(ui.returnLink.href, "/static/preview.html?id=p1");
  const item = new ui.Element("a"); item.href = "/static/live-settings.html";
  ui.listeners.click({ target: item, button: 0 });
  assert.equal(new URL(item.href, ui.host.location.origin).searchParams.get("return_to"), "/static/providers.html?section=text&return_to=%2Fstatic%2Fpreview.html%3Fid%3Dp1");
  const first = item.href;
  ui.listeners.click({ target: item, button: 0 });
  assert.equal(item.href, first, "repeated clicks do not nest another return parameter");
  const back = ui.mount.children[0].children[0], backURL = back.href;
  ui.listeners.click({ target: back, button: 0 });
  assert.equal(back.href, backURL, "back links must not add themselves to the trail");
  const download = new ui.Element("a"); download.href = "/static/import.html"; download.setAttribute("download", "file");
  ui.listeners.click({ target: download, button: 0 });
  assert.equal(download.href, "/static/import.html");
  ui.listeners.pageshow({ persisted: false });
  assert.equal(ui.scrolls.at(-1).top, 247);
}
{
  const ui = fixture("/static/overlay.html?return_to=" + encodeURIComponent("/#workspace"), { mounted: false });
  ui.navigation.install();
  assert.equal(ui.returnLink.href, "/#workspace", "the compact caption window needs no full navigation mount");
  const preview = fixture("/static/preview.html?id=live1");
  preview.navigation.install();
  preview.navigation.setFallback("/#recordings");
  assert.equal(preview.navigation.sourceURL(), "/#recordings");
  const renders = preview.mount.renders;
  preview.navigation.setFallback("/#recordings");
  assert.equal(preview.mount.renders, renders, "background project polling must preserve keyboard focus");
}
{
  const home = fixture("/"); home.navigation.install();
  const tabs = home.mount.children[1].children;
  assert.equal(tabs[0].attributes["aria-current"], "page");
  assert.ok(tabs.slice(1).every((item) => item.target === "_blank" && item.rel === "noopener"), "other modules must keep the capture workspace open");
  let raw = "/#workspace";
  for (let index = 0; index < 14; index++) raw = "/static/live-settings.html?return_to=" + encodeURIComponent(raw);
  assert.doesNotThrow(() => home.navigation.safeURL(raw));
}
{
  for (const name of ["index", "import", "providers", "live-settings", "space-downloads", "preview", "overlay"]) {
    const source = fs.readFileSync(path.join(__dirname, "..", "static", name + ".html"), "utf8");
    assert.match(source, /src="\/static\/navigation\.js\?v=ui-navigation-1" defer/, name + " loads the shared navigation");
    assert.match(source, /href="\/static\/navigation\.css\?v=ui-navigation-1"/, name + " uses the shared visual language");
    assert.match(source, name === "overlay" ? /data-return-link/ : /data-app-navigation/, name + " exposes a return control");
  }
}
console.log("Navigation tests passed: safe source trails, nested return, page selection, caption window, focus and live capture preservation.");
