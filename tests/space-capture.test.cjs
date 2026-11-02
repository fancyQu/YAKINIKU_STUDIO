/* Observable browser-capture behavior with real Response streams and Chrome mocks. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const directory = path.join(__dirname, "..", "browser-extension", "space-capture");
const rules = require(path.join(directory, "rules.js"));
const SPACE = "1dKrPrwgXpXJX", OTHER = "1wxWjjYejaRJQ";
const PAGE = `https://x.com/i/spaces/${SPACE}`;
const ROOT = "https://prod-fastly-ap-northeast-1.video.pscp.tv/Transcoding/v1/hls/test/audio-space/";
const KEY = `https://x.com/i/live_video_stream/key?broadcast_id=${SPACE}&stream_name=test`;
const KEY_B64 = Buffer.from(Array.from({length: 16}, (_, i) => i)).toString("base64");
const flush = () => new Promise(resolve => setImmediate(resolve));
function run(context, file) { return vm.runInContext(fs.readFileSync(path.join(directory, file), "utf8"), context, {filename: file}); }

function pageEnvironment(routes = {}) {
  const messages = [], listeners = [], requests = [], observers = [];
  let clones = 0;
  class MockXHR {
    constructor() { this.listeners = {}; this.status = 200; this.responseType = ""; }
    open(...args) { this.openArgs = args; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    load() { for (const callback of this.listeners.load || []) callback(); }
  }
  const originalOpen = MockXHR.prototype.open;
  const originalFetch = async value => {
    const url = String(value?.url || value);
    requests.push(url);
    const source = await (typeof routes[url] === "function" ? routes[url]() : routes[url] || "unrelated");
    const response = new Response(source, {status: 200});
    const clone = response.clone.bind(response);
    response.clone = () => { clones++; return clone(); };
    return response;
  };
  const window = {fetch: originalFetch,
    addEventListener(type, callback) { if (type === "message") listeners.push(callback); },
    postMessage(data) { messages.push(data); }};
  const location = new URL(PAGE);
  const context = vm.createContext({window, location, XMLHttpRequest: MockXHR, URL, Response, Uint8Array,
    ArrayBuffer, WeakMap, WeakSet, Date, JSON, btoa, atob, SpaceCaptureRules: rules,
    MutationObserver: class { constructor(callback) { observers.push(callback); } observe() {} disconnect() {} },
    setTimeout: () => 1, clearTimeout() {}});
  const dispatch = data => { for (const callback of listeners) callback({source: window, origin: location.origin, data}); };
  return {context, window, location, messages, requests, observers, MockXHR, originalFetch, originalOpen, dispatch,
    clones: () => clones,
    activate() { dispatch({channel: "yakiniku-space-control", type: "activate", space_id: SPACE, nonce: "task-nonce", expires: Date.now() + 600000}); }};
}

test("URL rules reject another Space, credentials, non-loopback commands and mixed-key playlists", () => {
  assert.equal(rules.keyURL(KEY, SPACE), KEY);
  assert.equal(rules.keyURL(KEY, OTHER), null);
  assert.equal(rules.keyURL(KEY.replace("x.com", "x.com.evil.invalid"), SPACE), null);
  assert.equal(rules.localPage("http://127.0.0.1.evil.invalid/static/space-downloads.html"), null);
  assert.equal(rules.localPage("http://localhost:8765/static/providers.html"), null);
  assert.equal(rules.canonicalSpace(`https://user:pass@x.com/i/spaces/${SPACE}`), null);
  const playlist = `#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="${KEY}"\n#EXTINF:3,\nsegment.aac\n#EXT-X-ENDLIST\n`;
  assert.equal(rules.playlist(ROOT + "replay.m3u8", playlist, SPACE), true);
  assert.equal(rules.playlist(ROOT + "replay.m3u8", playlist.replace(SPACE, OTHER), SPACE, [ROOT]), false);
  assert.equal(rules.playlist(ROOT + "replay.m3u8", "#EXTM3U\n#EXTINF:3,\na.aac", SPACE), false);
  assert.equal(rules.playlist(ROOT + "master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=96000\naudio.m3u8", SPACE, [ROOT]), false);
});

test("fetch stays transparent and inactive/unrelated requests never have their bodies cloned", async () => {
  const e = pageEnvironment({[KEY]: Buffer.from(Array.from({length: 16}, (_, i) => i))});
  run(e.context, "page-capture.js");
  const inactive = await e.window.fetch(KEY);
  assert.equal(e.clones(), 0);
  assert.equal((await inactive.arrayBuffer()).byteLength, 16);
  e.activate();
  await e.window.fetch(KEY.replace(SPACE, OTHER));
  assert.equal(e.clones(), 0);
  const response = await e.window.fetch(KEY);
  assert.equal(response.bodyUsed, false);
  assert.equal((await response.arrayBuffer()).byteLength, 16);
  await flush();
  assert.equal(e.clones(), 1);
  const captured = e.messages.find(m => m.payload?.kind === "key");
  assert.equal(captured.payload.key_base64, KEY_B64);
  e.dispatch({channel: "yakiniku-space-control", type: "deactivate", nonce: "task-nonce"});
  assert.equal(e.window.fetch, e.originalFetch);
  assert.equal(e.MockXHR.prototype.open, e.originalOpen);
});

test("only the chosen Space metadata associates its status response and complete original playlist", async () => {
  const graph = `https://x.com/i/api/graphql/query/AudioSpaceById?variables=${encodeURIComponent(JSON.stringify({id: SPACE}))}`;
  const status = "https://x.com/i/api/1.1/live_video_stream/status/28_target";
  const url = ROOT + "replay.m3u8";
  const text = `#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="${KEY}",IV=0x01\n#EXTINF:3,\na.aac\n#EXT-X-KEY:METHOD=AES-128,URI="${KEY}",IV=0x02\n#EXTINF:3,\nb.aac\n#EXT-X-ENDLIST\n`;
  const e = pageEnvironment({[graph]: JSON.stringify({data: {audioSpace: {metadata: {rest_id: SPACE, media_key: "28_target"}}}}),
    [status]: JSON.stringify({source: {noRedirectPlaybackUrl: url}}), [url]: text});
  run(e.context, "page-capture.js"); e.activate();
  await e.window.fetch(url); await flush();
  assert.equal(e.clones(), 0, "unassociated CDN requests are not read");
  await e.window.fetch(graph); await flush();
  await e.window.fetch(status); await flush();
  await e.window.fetch(url); await flush();
  assert.equal(e.messages.find(m => m.payload?.kind === "playlist").payload.text, text);
  assert.equal(e.messages.filter(m => m.payload?.kind === "scope").length, 1);
});

test("XHR inspection leaves binary response and original open arguments unchanged", () => {
  const e = pageEnvironment(); run(e.context, "page-capture.js"); e.activate();
  const xhr = new e.MockXHR();
  xhr.open("GET", KEY, false);
  const bytes = Uint8Array.from(Array.from({length: 16}, (_, i) => i)).buffer;
  xhr.responseType = "arraybuffer"; xhr.response = bytes; xhr.load();
  assert.deepEqual(xhr.openArgs, ["GET", KEY, false]);
  assert.equal(xhr.response, bytes);
  assert.equal(e.messages.find(m => m.payload?.kind === "key").payload.key_base64, KEY_B64);
});

test("automatic playback clicks exactly one recording button and never a social control", () => {
  const clicked = {play: 0, like: 0, post: 0};
  const buttons = [
    {textContent: "点赞", click() { clicked.like++; }},
    {textContent: "发帖", click() { clicked.post++; }},
    {textContent: "播放录音", click() { clicked.play++; }}
  ];
  const e = pageEnvironment();
  e.context.document = {querySelectorAll(selector) { assert.equal(selector, '[role="menu"] button, [role="dialog"] button'); return buttons; }};
  e.context.chrome = {runtime: {onMessage: {addListener() {}}, sendMessage(message, callback) {
    if (message.type === "space:config") callback({active: true, space_id: SPACE, nonce: "task-nonce", expires: Date.now() + 600000});
  }}};
  run(e.context, "x-bridge.js");
  for (const callback of e.observers) { callback(); callback(); }
  assert.deepEqual(clicked, {play: 1, like: 0, post: 0});
  assert.equal(rules.playButton(e.context.document, "https://x.com/home", SPACE), null);
  assert.equal(rules.playButton({querySelectorAll: () => [buttons[2], buttons[2]]}, PAGE, SPACE), null);
});

function backgroundEnvironment({createTab, captureResult = () => ({stage: "downloading"})} = {}) {
  const writes = [], created = [], updates = [], sends = [], uploads = [], runtimeListeners = [];
  let nextTabId = 90, nextNonce = 0;
  const chrome = {
    storage: {session: {async get() { return {}; }, async set(value) { writes.push(JSON.stringify(value)); }}},
    runtime: {onMessage: {addListener(cb) { runtimeListeners.push(cb); }}},
    tabs: {async create(value) { created.push(value); return createTab ? await createTab(value) : {id: nextTabId++}; }, async update(id, value) { updates.push({id, ...value}); },
      async sendMessage(id, value) { sends.push({id, ...value}); }, onRemoved: {addListener() {}}, onUpdated: {addListener() {}}}
  };
  const context = vm.createContext({chrome, URL, Date, Map, JSON, atob, btoa, crypto: {randomUUID: () => `random-nonce-${nextNonce++}`},
    setTimeout: () => 1, AbortSignal,
    fetch: async (url, options) => { uploads.push({url, options}); return new Response(JSON.stringify(await captureResult(url, options))); }});
  context.importScripts = file => run(context, file);
  run(context, "background.js");
  return {context, writes, created, updates, sends, uploads, handle: (message, sender) => context.handle(message, sender)};
}

test("background rejects other origins/tabs/Space keys and keeps tokens out of page configuration", async () => {
  const e = backgroundEnvironment();
  const job = {id: "a".repeat(32), space_id: SPACE, source_url: PAGE + "?s=20", capture_token: "a".repeat(43)};
  const local = {url: "http://127.0.0.1:8765/static/space-downloads.html", tab: {id: 5}};
  const denied = await e.handle({type: "space:start", job}, {url: "https://x.com/home", tab: {id: 6}});
  assert.equal(denied.type, "error"); assert.equal(e.created.length, 0);
  const started = await e.handle({type: "space:start", job}, local);
  assert.equal(started.type, "started");
  assert.equal(e.updates[0].url, PAGE);
  assert.equal((await e.handle({type: "space:config"}, {url: PAGE, tab: {id: 91}})).active, false);
  const sender = {url: PAGE, tab: {id: 90}};
  const config = await e.handle({type: "space:config"}, sender);
  assert.equal(config.active, true); assert.equal(config.capture_token, undefined);
  await e.handle({type: "space:capture", nonce: config.nonce, payload: {kind: "key", url: KEY.replace(SPACE, OTHER), key_base64: KEY_B64}}, sender);
  assert.equal(e.uploads.length, 0);
  await e.handle({type: "space:capture", nonce: config.nonce, payload: {kind: "key", url: KEY, key_base64: KEY_B64}}, sender);
  assert.equal(e.uploads.length, 1);
  assert.equal(e.uploads[0].url, `http://127.0.0.1:8765/api/space-downloads/${job.id}/capture`);
  assert.equal(e.uploads[0].options.headers["X-Space-Capture-Token"], job.capture_token);
  assert.equal(e.writes.some(text => text.includes(KEY_B64)), false);
  assert.deepEqual(JSON.parse(e.writes.at(-1)).spaceJobs, []);
  assert.equal((await e.handle({type: "space:config"}, sender)).active, false);
});

test("different Space jobs from one local page keep capture, completion and cancellation independent", async () => {
  const e = backgroundEnvironment();
  const local = {url: "http://127.0.0.1:8765/static/space-downloads.html", tab: {id: 5}};
  const first = {id: "a".repeat(32), space_id: SPACE, source_url: PAGE, capture_token: "a".repeat(43)};
  const second = {id: "b".repeat(32), space_id: OTHER, source_url: `https://x.com/i/spaces/${OTHER}`, capture_token: "b".repeat(43)};
  const third = {...first, id: "c".repeat(32), capture_token: "c".repeat(43)};
  assert.equal((await e.handle({type: "space:start", job: first}, local)).type, "started");
  assert.equal((await e.handle({type: "space:start", job: second}, local)).type, "started");
  const firstSender = {url: first.source_url, tab: {id: 90}};
  const secondSender = {url: second.source_url, tab: {id: 91}};
  const firstConfig = await e.handle({type: "space:config"}, firstSender);
  const secondConfig = await e.handle({type: "space:config"}, secondSender);
  assert.equal(firstConfig.active, true);
  assert.equal(secondConfig.active, true);
  assert.equal(firstConfig.space_id, SPACE);
  assert.equal(secondConfig.space_id, OTHER);
  assert.notEqual(firstConfig.nonce, secondConfig.nonce);
  assert.equal(e.sends.some(message => message.type === "space:stop"), false);
  assert.equal(JSON.parse(e.writes.at(-1)).spaceJobs.length, 2);

  await e.handle({type: "space:capture", nonce: firstConfig.nonce,
    payload: {kind: "key", url: KEY.replace(SPACE, OTHER), key_base64: KEY_B64}}, firstSender);
  await e.handle({type: "space:capture", nonce: firstConfig.nonce,
    payload: {kind: "key", url: KEY, key_base64: KEY_B64}}, secondSender);
  assert.equal(e.uploads.length, 0, "Space association and nonce stay isolated between task tabs");
  const completed = await e.handle({type: "space:capture", nonce: firstConfig.nonce,
    payload: {kind: "key", url: KEY, key_base64: KEY_B64}}, firstSender);
  assert.equal(completed.active, false);
  assert.equal(e.uploads[0].url, `http://127.0.0.1:8765/api/space-downloads/${first.id}/capture`);
  assert.equal(e.uploads[0].options.headers["X-Space-Capture-Token"], first.capture_token);
  assert.equal((await e.handle({type: "space:config"}, secondSender)).active, true, "completing the first job keeps the second armed");
  assert.deepEqual(e.sends.filter(message => message.type === "space:stop").map(message => message.id), [90]);

  await e.handle({type: "space:start", job: third}, local);
  const thirdSender = {url: third.source_url, tab: {id: 92}};
  assert.equal((await e.handle({type: "space:config"}, thirdSender)).active, true);
  await e.handle({type: "space:cancel", job_id: third.id}, local);
  assert.equal((await e.handle({type: "space:config"}, thirdSender)).active, false);
  assert.equal((await e.handle({type: "space:config"}, secondSender)).active, true, "cancelling another job keeps the second armed");
  await e.handle({type: "space:capture", nonce: secondConfig.nonce,
    payload: {kind: "key", url: KEY.replace(SPACE, OTHER), key_base64: KEY_B64}}, secondSender);
  assert.equal(e.uploads.length, 2);
  assert.equal(e.uploads[1].url, `http://127.0.0.1:8765/api/space-downloads/${second.id}/capture`);
  assert.equal(e.uploads[1].options.headers["X-Space-Capture-Token"], second.capture_token);
  assert.deepEqual(JSON.parse(e.writes.at(-1)).spaceJobs, []);
});

test("duplicate job IDs do not open another tab or interrupt existing jobs", async () => {
  const e = backgroundEnvironment();
  const local = {url: "http://127.0.0.1:8765/static/space-downloads.html", tab: {id: 5}};
  const job = {id: "a".repeat(32), space_id: SPACE, source_url: PAGE, capture_token: "a".repeat(43)};
  await e.handle({type: "space:start", job}, local);
  assert.equal((await e.handle({type: "space:start", job}, local)).type, "started");
  assert.equal((await e.handle({type: "space:start", job: {...job, capture_token: "b".repeat(43)}}, local)).type, "error");
  assert.equal((await e.handle({type: "space:start", job}, {...local, tab: {id: 6}})).type, "error");
  assert.equal(e.created.length, 1);
  assert.equal(e.sends.some(message => message.type === "space:stop"), false);
  assert.equal((await e.handle({type: "space:config"}, {url: PAGE, tab: {id: 90}})).active, true);
});

test("concurrent starts reserve distinct job slots and reject duplicate IDs before tabs finish opening", async () => {
  const releases = [];
  const e = backgroundEnvironment({createTab: () => new Promise(resolve => { releases.push(resolve); })});
  const local = {url: "http://127.0.0.1:8765/static/space-downloads.html", tab: {id: 5}};
  const job = {id: "a".repeat(32), space_id: SPACE, source_url: PAGE, capture_token: "a".repeat(43)};
  const starts = ["a", "b", "c", "d"].map(id => e.handle({type: "space:start", job: {...job, id: id.repeat(32)}}, local));
  await flush();
  assert.equal(e.created.length, 4);
  assert.equal((await e.handle({type: "space:start", job}, local)).type, "error");
  assert.equal((await e.handle({type: "space:start", job: {...job, id: "e".repeat(32)}}, local)).type, "error");
  assert.equal(e.created.length, 4);
  releases.forEach((release, index) => release({id: 90 + index}));
  assert.equal((await Promise.all(starts)).every(result => result.type === "started"), true);
  assert.equal(JSON.parse(e.writes.at(-1)).spaceJobs.length, 4);
  assert.equal(e.sends.some(message => message.type === "space:stop"), false);
});

test("manifest requests no cookie, debugger, request-header, all-site or social permissions", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, "manifest.json")));
  assert.deepEqual(manifest.permissions, ["storage"]);
  assert.equal(manifest.host_permissions.some(p => p.includes("<all_urls>") || p === "*://*/*"), false);
  assert.equal(manifest.content_scripts.every(s => !s.all_frames), true);
  assert.equal(manifest.content_scripts.filter(s => s.matches.some(p => p.startsWith("https:"))).every(s => s.matches.every(p => p.endsWith("/i/spaces/*"))), true);
});

test("fetch and XHR recheck the Space association when a delayed response arrives", async () => {
  const graph = `https://x.com/i/api/graphql/query/AudioSpaceById?variables=${encodeURIComponent(JSON.stringify({id: SPACE}))}`;
  const status = "https://x.com/i/api/1.1/live_video_stream/status/28_target";
  const url = ROOT + "replay.m3u8";
  const text = `#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="${KEY}",IV=0x01\n#EXTINF:3,\na.aac\n#EXT-X-ENDLIST\n`;
  let release;
  const e = pageEnvironment({[graph]: JSON.stringify({data: {audioSpace: {metadata: {rest_id: SPACE, media_key: "28_target"}}}}),
    [status]: () => new Promise(resolve => { release = () => resolve(JSON.stringify({source: {location: url}})); })});
  run(e.context, "page-capture.js"); e.activate();
  const pending = e.window.fetch(status);
  const xhr = new e.MockXHR(); xhr.open("GET", url);
  await e.window.fetch(graph); await flush();
  release(); await pending; await flush();
  xhr.responseType = "text"; xhr.responseText = text; xhr.load();
  assert.equal(e.messages.find(m => m.payload?.kind === "playlist").payload.text, text);
});

test("queued captures stop after download starts without a late unauthorized error", async () => {
  const e = backgroundEnvironment();
  const job = {id: "b".repeat(32), space_id: SPACE, source_url: PAGE, capture_token: "t".repeat(43)};
  await e.handle({type: "space:start", job}, {url: "http://localhost:8765/static/space-downloads.html", tab: {id: 5}});
  const sender = {url: PAGE, tab: {id: 90}};
  const config = await e.handle({type: "space:config"}, sender);
  const event = {type: "space:capture", nonce: config.nonce, payload: {kind: "key", url: KEY, key_base64: KEY_B64}};
  const responses = await Promise.all([e.handle(event, sender), e.handle(event, sender), e.handle(event, sender)]);
  assert.equal(e.uploads.length, 1);
  assert.equal(responses.every(r => r.active === false), true);
  assert.equal(e.sends.some(m => m.level === "error"), false);
});
