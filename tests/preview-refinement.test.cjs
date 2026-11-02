// Saved-live refinement must be deliberate, preserve the replay and reuse an
// existing file-processing project across reloads instead of uploading twice.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor(id = '') { this.id = id; this.children = []; this.dataset = {}; this.attributes = {}; this.events = {}; this.hidden = false; this.checked = false; this.paused = true; this.currentTime = 0; this.readyState = 0; this.classList = { add() {}, remove() {}, toggle() {} }; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children.flatMap(child => child.id === 'fragment' ? child.children : [child]); }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  async dispatch(type, event = {}) { for (const handler of this.events[type] || []) await handler(event); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  load() {} focus() {} play() { return Promise.resolve(); }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
const languages = ['ja', 'zh', 'bilingual'].map(language => { const e = new Element(); e.dataset.language = language; return e; });
const document = { getElementById: get, createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), querySelectorAll: () => languages };
const identifier = 'a'.repeat(32), refinedId = 'b'.repeat(32);
let testProject = { id: identifier, title: 'Saved live', source_type: 'live', status: 'completed', duration_ms: 1000, audio_saved: true, segments: [{ start_ms: 0, end_ms: 1000, ja: '原字幕', zh: '原译文' }], files: [{ name: 'audio.wav' }] };
let state = { eligible: true, ready: true, busy: false, active: false, message: '准备就绪，点击开始会创建独立精修项目。', chain: [{ label: '音频重新识别', provider: 'MiniMax', model: 'asr-1.0' }, { label: '日语校对', provider: 'GPT', model: 'selected-gpt' }, { label: '中文翻译', provider: 'GPT', model: 'selected-gpt' }] };
let refinementReadFailure = false, postGate = null, posts = [];
const requests = [], timers = [];
const context = vm.createContext({ document, window: {}, location: { search: `?id=${identifier}` }, URLSearchParams, AbortController,
  setTimeout(action, ms) { timers.push(ms); return timers.length; }, clearTimeout() {},
  fetch: async (url, options = {}) => {
    requests.push({ url, options });
    if (options.method === 'POST') {
      posts.push({ url, options });
      if (postGate) await postGate;
      state = { ...state, active: true, project_id: refinedId, project_status: 'processing', stage: 'transcribing', progress: 12, progress_label: 'MiniMax 日语转写中' };
      return { ok: true, json: async () => ({ id: refinedId }) };
    }
    if (url.endsWith('/refinement')) {
      if (refinementReadFailure) return { ok: false, status: 502, json: async () => ({ detail: '配置读取失败' }) };
      return { ok: true, json: async () => structuredClone(state) };
    }
    return { ok: true, json: async () => structuredClone(testProject) };
  },
});
const root = path.join(__dirname, '..');
const utility = fs.readFileSync(path.join(root, 'static/project-utils.js'), 'utf8').replace(/^export /gm, '');
const preview = fs.readFileSync(path.join(root, 'static/preview.js'), 'utf8').replace(/^import .*\n/, '');
const run = source => vm.runInContext(source, context);
(async () => {
  run(utility + '\n' + preview); await new Promise(setImmediate);
  assert.equal(get('preview-refinement').hidden, false);
  assert.equal(get('preview-refine').disabled, true, 'upload cannot start without explicit consent');
  assert.equal(posts.length, 0, 'preview loading never starts paid processing');
  assert.match(get('refinement-models').textContent, /MiniMax · asr-1.0/);
  assert.match(get('refinement-models').textContent, /GPT · selected-gpt/);
  state.message = '';
  await run('loadProject()');
  assert.match(get('refinement-status').textContent, /配置已就绪/);
  assert.doesNotMatch(get('refinement-status').textContent, /请先完成/);
  await get('preview-refine').dispatch('click'); assert.equal(posts.length, 0);
  get('refinement-consent').checked = true; await get('refinement-consent').dispatch('change');
  assert.equal(get('preview-refine').disabled, false);
  let release;
  postGate = new Promise(resolve => { release = resolve; });
  const starting = get('preview-refine').dispatch('click'); await new Promise(setImmediate);
  await get('preview-refine').dispatch('click');
  assert.equal(posts.length, 1, 'pending POST is protected from repeated clicks');
  assert.equal(posts[0].url, `/api/sessions/${identifier}/refine`);
  assert.deepEqual(JSON.parse(posts[0].options.body), { consent: true });
  release(); await starting; postGate = null;
  assert.equal(get('preview-refine').hidden, true);
  assert.equal(get('refinement-consent').checked, false);
  assert.match(get('refinement-status').textContent, /MiniMax 日语转写中 · 12%/);
  assert.equal(get('refinement-project-link').href, `/static/preview.html?id=${refinedId}`);
  assert.equal(get('refinement-progress-wrap').hidden, false);
  assert.equal(get('refinement-progress').value, 12);
  assert.ok(timers.includes(2500), 'a saved replay polls its active refinement');
  assert.equal(run('project.id'), identifier, 'original replay remains displayed');
  assert.equal(run('segments[0].zh'), '原译文');
  await run('loadProject()'); assert.equal(posts.length, 1, 'page reload only polls existing refinement');

  state = { ...state, active: false, project_status: 'completed', stage: 'completed', progress: 100 };
  await run('loadProject()');
  assert.equal(get('preview-refine').hidden, true);
  assert.equal(get('refinement-project-link').textContent, '预览精修结果');
  assert.match(get('refinement-status').textContent, /三种字幕已保存/);
  get('refinement-consent').checked = true; await get('preview-refine').dispatch('click');
  assert.equal(posts.length, 1, 'completed refinements do not accidentally rerun');

  state = { ...state, project_status: 'failed', stage: 'failed', message: '翻译失败，可以继续未完成步骤。' };
  await run('loadProject()');
  assert.equal(get('preview-refine').textContent, '继续精准校验');
  assert.equal(get('refinement-status').dataset.level, 'error');
  get('refinement-consent').checked = true; await get('preview-refine').dispatch('click');
  assert.equal(posts.at(-1).url, `/api/imports/${refinedId}/retry`, 'failed refinement resumes its own project');
  assert.equal(posts.filter(post => post.url.endsWith('/refine')).length, 1);

  state = { eligible: true, ready: false, active: false, message: '请先配置 MiniMax 文件语音识别。' };
  await run('loadProject()');
  assert.equal(get('preview-refine').disabled, true);
  assert.equal(get('refinement-config-links').hidden, false);
  assert.match(get('refinement-status').textContent, /配置 MiniMax/);
  state = { ...state, ready: true, busy: true, message: '' };
  const busyPollsBefore = timers.filter(ms => ms === 2500).length;
  await run('loadProject()');
  get('refinement-consent').checked = true; await get('preview-refine').dispatch('click');
  assert.equal(posts.length, 2, 'global processing cannot be interrupted by refinement');
  assert.equal(get('preview-refine').disabled, true);
  assert.match(get('refinement-status').textContent, /当前有录制或处理任务/);
  assert.equal(timers.filter(ms => ms === 2500).length, busyPollsBefore + 1, 'global busy state is polled even when this refinement is not active');
  state.busy = false;
  await run('loadProject()');
  assert.equal(get('preview-refine').disabled, false, 'automatic recheck unlocks the action after other processing finishes');
  assert.match(get('refinement-status').textContent, /配置已就绪/);

  refinementReadFailure = true; await run('loadProject()');
  assert.match(get('refinement-status').textContent, /配置暂时无法读取/);
  assert.equal(get('preview-title').textContent, 'Saved live', 'refinement errors do not break original replay');
  const getCount = requests.filter(request => request.url.endsWith('/refinement')).length;
  const originalId = 'c'.repeat(32);
  testProject = { ...testProject, source_type: 'import', refinement_source_id: originalId, status: 'failed', import_stage: 'failed' };
  await run('loadProject()');
  assert.equal(get('preview-refinement').hidden, true);
  assert.equal(get('preview-source-note').hidden, false);
  assert.equal(get('preview-source-link').href, `/static/preview.html?id=${originalId}`);
  assert.equal(get('preview-retry').textContent, '继续精准校验');
  assert.equal(get('preview-retry').hidden, false);
  assert.equal(requests.filter(request => request.url.endsWith('/refinement')).length, getCount, 'imports retain existing processing workflow');
  testProject.refinement_source_id = 'javascript:alert(1)';
  await run('loadProject()');
  assert.equal(get('preview-source-note').hidden, true, 'source navigation accepts only saved project IDs');
  assert.equal(get('preview-source-link').getAttribute('href'), null);
  assert.equal(get('preview-retry').textContent, '继续处理未完成内容', 'ordinary imports keep their existing action label');
  const markup = fs.readFileSync(path.join(root, 'static/preview.html'), 'utf8');
  assert.match(markup, /录音发送至已配置的 MiniMax/);
  assert.match(markup, /调用费用按服务商计费/);
  console.log('Preview refinement tests passed: consent, duplicate protection, polling, resume, exports link and original replay preservation.');
})().catch(error => { console.error(error); process.exitCode = 1; });
