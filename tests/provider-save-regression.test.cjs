// Separate settings tabs share one saved configuration, but keep their own drafts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/providers.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'static/providers.js'), 'utf8')
  .replace(/^import .*\n/gm, '').replace(/^export /gm, '')
  .replace(/\/\/ Bootstrap the standalone settings page\.[\s\S]*/, '');
const clone = value => JSON.parse(JSON.stringify(value));
const tasks = { asr: ['file_asr'], text: ['correction', 'translation'] };
class Element {
  constructor(id = '') { Object.assign(this, { id, value: '', checked: false, disabled: false,
    hidden: false, dataset: {}, handlers: {}, children: [], attributes: {}, textContent: '' }); }
  addEventListener(name, callback) { (this.handlers[name] ||= []).push(callback); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  async emit(type) { for (const callback of this.handlers[type] || []) await callback({ target: this, preventDefault() {} }); }
}
function server() {
  return { requests: [], versions: { asr: 0, text: 0 }, config: {
    providers: [
      { id: 'speech', name: 'MiniMax saved', scope: 'asr', vendor: 'minimax', base_url: 'https://speech.example/v1', speech_api: 'minimax', text_api: 'chat_completions', enabled: true, key_configured: true },
      { id: 'gpt', name: 'GPT saved', scope: 'text', vendor: 'custom', base_url: 'https://text.example/v1', speech_api: 'none', text_api: 'chat_completions', enabled: true, key_configured: true },
      { id: 'shared', name: 'Legacy shared', scope: 'text', vendor: 'custom', base_url: 'https://legacy.example/v1', speech_api: 'openai', text_api: 'chat_completions', enabled: true, key_configured: true },
      { id: 'shared-asr', name: 'Legacy shared · 音频识别', scope: 'asr', vendor: 'custom', base_url: 'https://legacy.example/v1', speech_api: 'openai', text_api: 'chat_completions', enabled: true, key_configured: true },
    ],
    routes: { file_asr: { provider_id: 'shared-asr', model: 'asr-1.0', response_format: 'verbose_json' }, correction: { provider_id: 'shared', model: 'gpt-correct' }, translation: { provider_id: 'shared', model: 'gpt-translate' } },
    presets: [{ id: 'custom', name: 'Custom', speech_api: 'openai', text_api: 'chat_completions' }, { id: 'minimax', name: 'MiniMax', speech_api: 'minimax', text_api: 'chat_completions' }],
    ready: { file_asr: true, correction: true, translation: true }, locked: false,
    section_revisions: { asr: 'asr-0', text: 'text-0' },
  } };
}
function tab(backend, section) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element(m[1])]));
  const get = id => { assert.ok(elements.has(id), id); return elements.get(id); };
  async function request(url, options = {}) {
    const method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : null;
    backend.requests.push({ method, url, body });
    if (url === '/api/health') return { active: false, import_active: false };
    if (method === 'GET') { assert.equal(url, '/api/service-config'); return clone(backend.config); }
    const scope = url === '/api/service-config/provider' ? body.section : url.split('/').at(-1);
    assert.ok(tasks[scope]);
    assert.equal(typeof body.expected_revision, 'string', 'every UI save includes the loaded module revision');
    if (body.expected_revision !== backend.config.section_revisions[scope]) {
      const error = new Error('此模块已在其他页面更新，请重新读取后再保存。'); error.status = 409; throw error;
    }
    if (url === '/api/service-config/provider') {
      assert.equal(body.scope, scope);
      const previous = backend.config.providers.find(p => p.id === body.id) || {};
      const saved = { ...previous, ...body, key_configured: true }; delete saved.api_key;
      backend.config.providers = backend.config.providers.filter(p => p.id !== body.id).concat(saved);
    } else {
      const current = scope;
      assert.deepEqual(Object.keys(body.routes).sort(), [...tasks[current]].sort());
      assert.ok(body.providers.every(provider => provider.scope === current), 'module saves cannot contain connections from another module');
      const saved = body.providers.map(p => ({ ...backend.config.providers.find(old => old.id === p.id), ...p, key_configured: true }));
      backend.config.providers = backend.config.providers.filter(p => p.scope !== current && !saved.some(item => item.id === p.id)).concat(saved);
      backend.config.routes = { ...backend.config.routes, ...body.routes };
    }
    backend.config.section_revisions[scope] = `${scope}-${++backend.versions[scope]}`;
    return clone(backend.config);
  }
  const context = vm.createContext({ document: { getElementById: get, createElement: () => new Element() }, jsonRequest: request, Math, Map, Set });
  vm.runInContext(source + `\nvar controller = createServiceConfigUI({initialSection:${JSON.stringify(section)}});`, context);
  const run = code => vm.runInContext(code, context);
  async function input(id, value) { get(id).value = value; await get(id).emit('input'); }
  async function select(id, value) { get(id).value = value; await get(id).emit('change'); }
  function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
  async function pick(name) { const node = get('provider-list').children.find(item => text(item).includes(name)); assert.ok(node, name); await node.emit('click'); }
  return { get, run, input, select, pick };
}
async function independentModulesCannotRevertAnotherTabSave() {
  const backend = server(), asr = tab(backend, 'asr'), text = tab(backend, 'text');
  await asr.run('controller.load()'); await text.run('controller.load()');
  await asr.pick('Legacy shared');
  await asr.input('provider-base-url', 'https://legacy.example/audio/v1');
  await asr.select('provider-speech-api', 'minimax');
  await asr.run('controller.saveProvider()');
  await text.input('correction-model', 'gpt-chosen-by-user');
  await text.run('controller.save()');
  const saved = backend.config.providers.find(p => p.id === 'shared-asr');
  assert.equal(saved.base_url, 'https://legacy.example/audio/v1', 'saving text models must not write a stale ASR address');
  assert.equal(saved.speech_api, 'minimax', 'saving text models must not revert the speech protocol');
  assert.equal(backend.config.providers.find(p => p.id === 'shared').base_url, 'https://legacy.example/v1', 'the independent text address is preserved');
  assert.equal(backend.config.routes.correction.model, 'gpt-chosen-by-user');
}
async function anotherModuleSavePreservesIndependentDraft() {
  const backend = server(), page = tab(backend, 'text');
  await page.run('controller.load()'); await page.pick('Legacy shared');
  await page.input('provider-name', 'Pending text name');
  await page.run("controller.openSection('asr')"); await page.pick('Legacy shared');
  await page.input('provider-base-url', 'https://legacy.example/audio/v1');
  await page.select('provider-speech-api', 'minimax'); await page.run('controller.saveProvider()');
  await page.run("controller.openSection('text')"); await page.pick('Pending text name');
  await page.run('controller.save()');
  const saved = backend.config.providers.find(p => p.id === 'shared');
  assert.equal(saved.name, 'Pending text name', 'explicit text draft edits survive another module save');
  assert.equal(saved.base_url, 'https://legacy.example/v1', 'the text copy never adopts the ASR address');
  const asr = backend.config.providers.find(p => p.id === 'shared-asr');
  assert.equal(asr.base_url, 'https://legacy.example/audio/v1');
  assert.equal(asr.speech_api, 'minimax');
}
async function sameModuleStaleSaveRetainsInputsAndCannotOverwrite() {
  const backend = server(), first = tab(backend, 'text'), second = tab(backend, 'text');
  await first.run('controller.load()'); await second.run('controller.load()');
  await first.input('provider-name', 'Saved in first tab');
  await second.input('provider-name', 'Unsaved second tab');
  await second.input('provider-api-key', 'fixture-secret-kept-in-draft');
  await second.input('correction-model', 'gpt-pending');
  await first.run('controller.saveProvider()');
  for (const save of ['controller.save()', 'controller.saveProvider()']) {
    await second.run(save);
    assert.equal(backend.config.providers.find(p => p.id === 'gpt').name, 'Saved in first tab');
    assert.equal(second.get('provider-name').value, 'Unsaved second tab');
    assert.equal(second.get('provider-api-key').value, 'fixture-secret-kept-in-draft');
    assert.equal(second.get('correction-model').value, 'gpt-pending');
    assert.equal(second.get('service-status').dataset.level, 'error');
    assert.match(second.get('service-status').textContent, /重新读取/);
    assert.match(second.get('service-status').textContent, /草稿仍保留/);
  }
}
async function anotherModuleResponseCannotAuthorizeStaleDraft() {
  const backend = server(), local = tab(backend, 'text'), remote = tab(backend, 'text');
  await local.run('controller.load()'); await remote.run('controller.load()');
  await local.input('provider-name', 'Local pending name');
  await remote.input('provider-name', 'Remote saved name'); await remote.run('controller.saveProvider()');
  await local.run("controller.openSection('asr')");
  await local.input('provider-name', 'Independent ASR update'); await local.run('controller.saveProvider()');
  await local.run("controller.openSection('text')"); await local.run('controller.save()');
  assert.equal(backend.config.providers.find(p => p.id === 'gpt').name, 'Remote saved name');
  assert.match(local.get('service-status').textContent, /重新读取/);
  assert.equal(local.get('provider-name').value, 'Local pending name');
}
(async () => {
  for (const scenario of [independentModulesCannotRevertAnotherTabSave, anotherModuleSavePreservesIndependentDraft,
    sameModuleStaleSaveRetainsInputsAndCannotOverwrite, anotherModuleResponseCannotAuthorizeStaleDraft]) {
    try { await scenario(); console.log(`${scenario.name}: passed`); }
    catch (error) { console.error(`${scenario.name}: failed`); console.error(error); process.exitCode = 1; }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
