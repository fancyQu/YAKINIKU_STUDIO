// Exercise saved-live preview and the existing import view through production
// helpers/handlers. Incomplete ASR must be retryable even without any Japanese.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
class Element {
  constructor(id=''){this.id=id;this.children=[];this.dataset={};this.attributes={};this.events={};this.hidden=false;this.checked=false;this.paused=true;this.currentTime=0;this.readyState=0;this.classList={add(){},remove(){},toggle(){}};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children.flatMap(child=>child.id==='fragment'?child.children:[child]);}
  addEventListener(type,handler){(this.events[type]||=[]).push(handler);}
  async dispatch(type,event={}){await Promise.all((this.events[type]||[]).map(handler=>handler(event)));}
  setAttribute(key,value){this.attributes[key]=value;}
  removeAttribute(key){delete this.attributes[key];}
  getAttribute(key){return this.attributes[key]??null;}
  load(){}focus(){}play(){return Promise.resolve();}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
const languages=['ja','zh','bilingual'].map(language=>{const e=new Element();e.dataset.language=language;return e;});
const document={getElementById:get,createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>languages};
const identifier='a'.repeat(32);
let testProject={id:identifier,title:'Live test',status:'partial',audio_saved:true,duration_ms:6000,source_type:'live',segments:[
  {id:'asr-failed',start_ms:0,end_ms:2000,ja:'',ja_raw:'',zh:'',asr_status:'failed',translation_status:'deferred'},
  {id:'translation-failed',start_ms:2000,end_ms:4000,ja:'原文',zh:'',asr_status:'done',translation_status:'failed'},
  {id:'quiet',start_ms:4000,end_ms:5000,ja:'',zh:'',asr_status:'no_speech',translation_status:'skipped'},
  {id:'good',start_ms:5000,end_ms:6000,ja:'完成原文',zh:'完成译文',asr_status:'done',translation_status:'done'},
],files:[{name:'audio.wav'},{name:'ja.srt'},{name:'zh-CN.srt'},{name:'bilingual.srt'}]};
const requests=[];const timers=[];
const context=vm.createContext({document,window:{},location:{search:`?id=${identifier}`},URLSearchParams,AbortController,
  setTimeout(action,ms){timers.push(ms);return timers.length;},clearTimeout(){},
  fetch:async(url,options)=>{requests.push({url,options});return{ok:true,json:async()=>structuredClone(testProject)};},
});
const root=path.join(__dirname,'..');
const utility=fs.readFileSync(path.join(root,'static/project-utils.js'),'utf8').replace(/^export /gm,'');
const preview=fs.readFileSync(path.join(root,'static/preview.js'),'utf8').replace(/^import .*\n/,'');
const run=source=>vm.runInContext(source,context);
(async()=>{
  run(utility+'\n'+preview);await new Promise(setImmediate);
  assert.equal(get('preview-count').textContent,'3 句','normal no-speech tasks do not create subtitle cues');
  assert.equal(get('preview-retry').hidden,false,'empty-ASR failures remain recoverable');
  assert.equal(get('preview-retry').textContent,'补全字幕');
  assert.match(get('preview-warning').textContent,/2 段字幕尚未完成/);
  assert.match(get('preview-zh').textContent,/识别未完成/);
  await get('preview-retry').dispatch('click');
  assert.equal(requests.find(request=>request.options?.method==='POST').url,`/api/sessions/${identifier}/retry-translations`);
  testProject={...testProject,status:'finalizing',subtitle_status:'backfilling'};
  await run('loadProject()');
  assert.equal(get('preview-retry').hidden,true);assert.match(get('preview-state').textContent,/音频已保存 · 字幕补全中/);
  assert.doesNotMatch(get('preview-state').textContent,/0%/);assert.ok(timers.includes(2500),'live backfill is polled after a page reload');
  testProject={...testProject,source_type:'import',status:'processing',import_stage:'translating',import_progress:37,import_progress_label:'GPT 校正与翻译中'};
  await run('loadProject()');assert.match(get('preview-state').textContent,/37%/);assert.equal(get('preview-retry').hidden,true);
  testProject={...testProject,status:'failed',subtitle_status:'partial',import_stage:'failed',import_error:'测试错误'};
  await run('loadProject()');assert.equal(get('preview-retry').hidden,false);assert.equal(get('preview-retry').textContent,'继续处理未完成内容');
  await get('preview-retry').dispatch('click');assert.equal(requests.findLast(request=>request.options?.method==='POST').url,`/api/imports/${identifier}/retry`);
  assert.equal(run("pendingSubtitleCount({duration_ms:4000,live_recovery_from_ms:2000,segments:[]})"),1,'interrupted durable tail remains discoverable before it is segmented');
  assert.equal(run("projectBusy({status:'processing',source_type:'live'})"),true);
  console.log('Live preview tests passed: failed ASR visibility/retry, recovered tail, background polling, silence exclusion and unchanged file-import routing.');
})().catch(error=>{console.error(error);process.exitCode=1;});
