// Exercise production handlers. Every backend/browser operation is a local fake.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const tick = () => new Promise(setImmediate);
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
class Element {
  constructor(id = '') {
    this.id = id; this.textContent = ''; this.value = ''; this.children = [];
    this.dataset = {}; this.attributes = {}; this.listeners = {}; this.hidden = false;
    this.paused = true; this.currentTime = 0; this.readyState = 0;
    this.classes = new Set();
    this.classList = { add: value => this.classes.add(value), remove: value => this.classes.delete(value),
      toggle: (value, on) => on ? this.classes.add(value) : this.classes.delete(value) };
  }
  cloneNode() { return new Element(); }
  get childNodes() { return this.children; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, action) { this.listeners[type] = action; }
  async dispatch(type, event = {}) { return this.listeners[type]?.(event); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  getBoundingClientRect() { return { width: 100, height: 60 }; }
  getContext() { return { setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; }
  load() {} focus() {} play() { return Promise.resolve(); }
}
function makeDocument() {
  const elements = new Map(), events = {};
  const get = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  get('recordings-list').firstElementChild = new Element();
  const links = ['#workspace', '#recordings'].map(href => { const element = new Element(href); element.setAttribute('href', href); return element; });
  const doc = { getElementById: get, createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element('fragment'), querySelectorAll: () => links,
    addEventListener(type, action) { events[type] = action; },
    querySelector: () => ({ async requestFullscreen() { doc.fullscreenElement = {}; events.fullscreenchange?.(); } }),
    async exitFullscreen() { doc.fullscreenElement = null; events.fullscreenchange?.(); } };
  doc.documentElement = { async requestFullscreen() { doc.fullscreenElement = {}; events.fullscreenchange?.(); } };
  return { doc, get, events, links };
}
const response = body => ({ ok: true, json: async () => structuredClone(body) });
async function workbenchActions() {
  const { doc, get, events, links } = makeDocument();
  const opened = [], windowEvents = {}, requests = [];
  let popupAllowed = true, sessionsGate = null, sessionsFailure = false;
  class Channel { postMessage() {} }
  const context = vm.createContext({ document: doc,
    window: { devicePixelRatio: 1, BroadcastChannel: Channel,
      addEventListener(type, action) { windowEvents[type] = action; },
      open(url, target) { opened.push({ url, target }); return popupAllowed ? {} : null; } },
    YakinikuNavigation: { withReturnTo: url => `${url}?return_to=%2F%23recordings` },
    BroadcastChannel: Channel, location: { origin: 'http://127.0.0.1:8877', search: '', hash: '#recordings' },
    requestAnimationFrame() {}, AbortController, URL, URLSearchParams, ArrayBuffer, Uint8Array,
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}, performance,
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === '/api/sessions') {
        if (sessionsGate) await sessionsGate.promise;
        if (sessionsFailure) throw new TypeError('fixture unavailable');
        return response([]);
      }
      if (url === '/api/health') return response({});
      if (url === '/api/live-settings') return response({ backend: 'mlx', model: 'mlx-whisper-small', translation_ready: true });
      if (url === '/api/live-model/status') return response({ state: 'ready', model: 'mlx-whisper-small' });
      return response({ transcription_mode: 'local' });
    },
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'static/app.js'), 'utf8'), context);
  await tick();
  assert.equal(links[1].getAttribute('aria-current'), 'location');
  assert.equal(links[0].getAttribute('aria-current'), null);
  context.location.hash = '#workspace'; windowEvents.hashchange();
  assert.equal(links[0].classes.has('active'), true);
  assert.equal(links[1].getAttribute('aria-current'), null);
  await get('open-settings').dispatch('click'); await get('open-settings').dispatch('click');
  assert.equal(opened.length, 2);
  assert.ok(opened.every(item => item.target === '_blank'), 'settings never replace an existing draft window');
  assert.ok(opened[0].url.includes('return_to='));
  popupAllowed = false; await get('open-settings').dispatch('click');
  assert.equal(get('notice').dataset.level, 'error'); assert.match(get('notice').textContent, /拦截/);
  popupAllowed = true; await get('open-model-download').dispatch('click');
  assert.ok(opened.at(-1).url.includes('/static/live-settings.html#live-model-download'));
  assert.ok(opened.at(-1).url.includes('return_to='));assert.equal(opened.at(-1).target,'_blank');
  assert.equal(requests.filter(item=>item.url==='/api/live-model/download').length,0,'opening the download page does not start a download');
  assert.match(get('notice').textContent,/点击「下载模型」才会开始/);
  popupAllowed = false; await get('open-model-download').dispatch('click');
  assert.equal(get('notice').dataset.level,'error');assert.match(get('notice').textContent,/拦截/);
  popupAllowed = true; await get('overlay-button').dispatch('click');
  assert.ok(opened.at(-1).url.startsWith('/static/overlay.html?return_to='));
  const before = requests.filter(item => item.url === '/api/sessions').length;
  sessionsGate = deferred();
  const refreshing = get('refresh-recordings').dispatch('click'); await tick();
  await get('refresh-recordings').dispatch('click');
  assert.equal(requests.filter(item => item.url === '/api/sessions').length, before + 1);
  assert.equal(get('refresh-recordings').disabled, true);
  assert.equal(get('refresh-recordings').getAttribute('aria-busy'), 'true');
  sessionsGate.resolve(); await refreshing; sessionsGate = null;
  assert.equal(get('notice').dataset.level, 'success'); assert.match(get('notice').textContent, /共 0 个/);
  assert.equal(get('refresh-recordings').disabled, false);
  assert.equal(get('refresh-recordings').getAttribute('aria-busy'), 'false');
  sessionsFailure = true; await get('refresh-recordings').dispatch('click');
  assert.equal(get('notice').dataset.level, 'error'); assert.equal(get('refresh-recordings').disabled, false);
  await get('fullscreen-button').dispatch('click');
  assert.equal(get('fullscreen-button').getAttribute('aria-pressed'), 'true');
  assert.equal(get('fullscreen-button').getAttribute('aria-label'), '退出字幕全屏');
  doc.fullscreenElement = null; events.fullscreenchange();
  assert.equal(get('fullscreen-button').getAttribute('aria-label'), '全屏显示字幕');
}
async function previewActions() {
  const { doc, get } = makeDocument(); doc.querySelectorAll = () => [];
  const id = 'a'.repeat(32), requests = [], fallbacks = [];
  let project = { id, title: 'Fixture', source_type: 'live', status: 'partial', duration_ms: 1000,
    segments: [{ id: 'one', ja: 'fixture', zh: '', start_ms: 0, end_ms: 1000 }], files: [] };
  let projectGate = null, postGate = null, postFailure = false, refinementFailure = false;
  const context = vm.createContext({ document: doc, window: {}, location: { search: `?id=${id}` },
    YakinikuNavigation: { setFallback: url => fallbacks.push(url) },
    URLSearchParams, AbortController, setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (options.method === 'POST') {
        if (postGate) await postGate.promise;
        if (postFailure) return { ok: false, status: 502, json: async () => ({ detail: 'fixture retry failed' }) };
        project.status = 'processing'; return response({});
      }
      if (url.endsWith('/refinement')) {
        if (refinementFailure) return { ok: false, status: 502, json: async () => ({ detail: 'fixture check failed' }) };
        return response({ eligible: true, ready: false, message: 'fixture configuration required' });
      }
      if (projectGate) await projectGate.promise;
      return response(project);
    },
  });
  const source = fs.readFileSync(path.join(root, 'static/project-utils.js'), 'utf8').replace(/^export /gm, '') + '\n'
    + fs.readFileSync(path.join(root, 'static/preview.js'), 'utf8').replace(/^import .*\n/, '');
  vm.runInContext(source, context); await tick();
  assert.equal(fallbacks.at(-1), '/#recordings', 'a direct live preview returns to the live recordings list');
  projectGate = deferred();
  const checking = get('refinement-refresh').dispatch('click'); await tick();
  const readCount = requests.length;
  await get('refinement-refresh').dispatch('click');
  assert.equal(requests.length, readCount, 'manual recheck cannot duplicate a pending request');
  assert.equal(get('refinement-refresh').disabled, true);
  assert.equal(get('refinement-refresh').getAttribute('aria-busy'), 'true');
  assert.equal(get('refinement-refresh').textContent, '正在检查…');
  projectGate.resolve(); await checking; projectGate = null;
  assert.equal(get('preview-notice').dataset.level, 'success');
  assert.equal(get('refinement-refresh').disabled, false);
  refinementFailure = true; await get('refinement-refresh').dispatch('click');
  assert.equal(get('preview-notice').dataset.level, 'error'); assert.match(get('preview-notice').textContent, /fixture check failed/);
  refinementFailure = false;
  postGate = deferred();
  const retry = get('preview-retry').dispatch('click'); await tick();
  await get('preview-retry').dispatch('click');
  assert.equal(requests.filter(item => item.options.method === 'POST').length, 1);
  assert.equal(get('preview-retry').textContent, '正在提交…');
  assert.equal(get('preview-retry').getAttribute('aria-busy'), 'true');
  postGate.resolve(); await retry; postGate = null;
  assert.equal(get('preview-retry').getAttribute('aria-busy'), 'false');
  assert.equal(get('preview-retry').hidden, true, 'accepted background work hides the restart action');
  project.status = 'partial'; await vm.runInContext('loadProject()', context);
  postFailure = true; await get('preview-retry').dispatch('click');
  assert.equal(get('preview-retry').disabled, false); assert.equal(get('preview-retry').textContent, '补全字幕');
  assert.match(get('preview-notice').textContent, /fixture retry failed/);
  project = { ...project, source_type: 'import' }; await vm.runInContext('loadProject()', context);
  assert.equal(fallbacks.at(-1), '/static/import.html', 'a direct file-import preview returns to audio projects');
}
async function overlayActions() {
  const { doc, get, events } = makeDocument();
  const context = vm.createContext({ document: doc, window: {} });
  vm.runInContext(fs.readFileSync(path.join(root, 'static/overlay.js'), 'utf8'), context);
  await get('overlay-fullscreen').dispatch('click');
  assert.equal(get('overlay-fullscreen').textContent, '退出全屏');
  assert.equal(get('overlay-fullscreen').getAttribute('aria-pressed'), 'true');
  doc.fullscreenElement = null; events.fullscreenchange();
  assert.equal(get('overlay-fullscreen').textContent, '全屏');
  doc.documentElement.requestFullscreen = async () => { throw new Error('fixture denied'); };
  await get('overlay-fullscreen').dispatch('click');
  assert.equal(get('overlay-fullscreen').disabled, false);
  assert.match(get('overlay-help').textContent, /无法全屏/);
}
(async () => {
  await workbenchActions(); await previewActions(); await overlayActions();
  console.log('Page action tests passed: navigation context, popup failure, busy/duplicate guards, success/error feedback and fullscreen state.');
})().catch(error => { console.error(error); process.exitCode = 1; });
