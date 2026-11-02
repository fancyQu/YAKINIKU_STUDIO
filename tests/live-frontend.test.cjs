// Run the real workbench and overlay code without browser permissions, audio
// uploads, or network calls. Exercise ordering and the actual stop protocol.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.textContent = ''; this.childNodes = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.hidden = false; this.classList = { toggle(){}, add(){} }; }
  cloneNode() { return new Element(); }
  replaceChildren(...children) { this.childNodes = children; }
  append(...children) { this.childNodes.push(...children); }
  addEventListener(type, action) { this.listeners[type] = action; }
  getBoundingClientRect() { return { width:100,height:60 }; }
  getContext() { return { setTransform(){},clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){} }; }
}
function makeDocument() {
  const elements = new Map();
  return { getElementById(id) { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); }, createElement(){return new Element();},createDocumentFragment(){return new Element();} };
}
const document = makeDocument();
document.getElementById('recordings-list').firstElementChild = new Element();
let liveConfig = { backend:'mlx',model:'mlx-whisper-small',translation_route:{provider_id:'text-1',model:'chosen-live-model'},translation_ready:true };
let modelStatus = { state:'ready',model:'mlx-whisper-small',backend:'mlx' };
let healthStatus = { active:false,import_active:false,subtitle_active:false,model_preparing:false };
const published = [];
const animationFrames = [];
class Channel { constructor(){this.onmessage=null;} postMessage(message){published.push({...message});} }
const requests=[];
const windowListeners={};
const context=vm.createContext({
  document,window:{devicePixelRatio:1,BroadcastChannel:Channel,addEventListener(type,action){windowListeners[type]=action;}},BroadcastChannel:Channel,
  location:{origin:'http://127.0.0.1:8765',search:''}, requestAnimationFrame(action){animationFrames.push(action);},AbortController,URL,URLSearchParams,
  setTimeout,clearTimeout,setInterval,clearInterval,ArrayBuffer,Uint8Array,
  WebSocket:{OPEN:1,CLOSING:2},performance,
  fetch:async(url,options)=>{requests.push({url,options}); return {ok:true,json:async()=>url==='/api/health'?{...healthStatus}:url==='/api/live-settings'?{...liveConfig}:url==='/api/live-model/status'||url==='/api/live-model/prepare'?{...modelStatus}:url==='/api/settings'?{transcription_mode:'local',transcription_model:'tiny',configuration_ready:false}:[]};},
});
const run=(source)=>vm.runInContext(source,context);
function emit(message) { context.testMessage=message;run('handleProviderMessage(capture,testMessage)'); }
function segment(seq,zh='',session='live-a') { return {type:zh?'translation':'transcript',session_id:session,segment:{session_id:session,id:`s-${seq}`,segment_id:`s-${seq}`,sequence:seq,start_ms:seq*1000,end_ms:seq*1000+800,ja_raw:`原稿${seq}`,ja:`日本語${seq}`,zh,translation_status:zh?'done':'pending'}}; }
const get=(id)=>document.getElementById(id);
(async()=>{
  run(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'));
  await new Promise(setImmediate);
  assert.equal(get('start-button').disabled,false,'only live translation + loaded model gate capture; missing file/correction settings do not');
  assert.equal(get('live-model-name').textContent,'MLX Whisper Small');
  healthStatus.model_preparing=true;await run('loadLiveReadiness()');
  assert.equal(get('start-button').disabled,true,'background preparation locks capture even after its original request has gone away');
  assert.equal(get('prepare-live-model').disabled,true);assert.match(get('live-prepare-help').textContent,/后台加载与预热/);
  assert.doesNotMatch(get('live-prepare-help').textContent,/已有录制/);
  healthStatus.model_preparing=false;await run('loadLiveReadiness()');assert.equal(get('start-button').disabled,false);
  healthStatus.subtitle_active=true;await run('loadLiveReadiness()');assert.match(get('live-prepare-help').textContent,/字幕补全/);assert.equal(get('start-button').disabled,true);
  healthStatus.subtitle_active=false;await run('loadLiveReadiness()');
  modelStatus={state:'not_prepared',prepared_files:false,download:{state:'missing'}};await run('loadLiveReadiness()');
  assert.equal(get('prepare-live-model').disabled,true);assert.match(get('live-prepare-help').textContent,/点击「模型下载」/);
  const beforeMissingPrepare=requests.filter(r=>r.url==='/api/live-model/prepare').length;
  await run('prepareLiveModel()');
  assert.equal(requests.filter(r=>r.url==='/api/live-model/prepare').length,beforeMissingPrepare,'missing files never trigger a prepare request');
  assert.equal(requests.filter(r=>r.url==='/api/live-model/download').length,0,'readiness checks never download');
  healthStatus.model_downloading=true;await run('loadLiveReadiness()');
  assert.equal(get('start-button').disabled,true);assert.equal(get('open-model-download').textContent,'查看下载进度');assert.match(get('live-prepare-help').textContent,/下载或校验/);
  healthStatus.model_downloading=false;
  liveConfig.backend='tiny';liveConfig.model='tiny';modelStatus={state:'ready',backend:'tiny',model:'tiny',active_download:{state:'downloading',backend:'mlx',model:'mlx-whisper-large-v3-turbo'}};
  await run('loadLiveReadiness()');assert.equal(get('open-model-download').hidden,false);assert.equal(get('open-model-download').textContent,'查看下载进度');assert.equal(get('start-button').disabled,true);
  liveConfig.backend='mlx';liveConfig.model='mlx-whisper-small';
  modelStatus={state:'failed',message:'GPU unavailable',prepared_files:true};await run('loadLiveReadiness()');
  assert.equal(get('start-button').disabled,true);
  assert.equal(get('live-prepare-help').textContent,'GPU unavailable');
  modelStatus={state:'ready',model:'mlx-whisper-small'};await run('prepareLiveModel()');
  assert.equal(get('start-button').disabled,false);
  assert.ok(requests.some(r=>r.url==='/api/live-model/prepare'&&r.options.method==='POST'));
  liveConfig.translation_ready=false;await run('loadLiveReadiness()');assert.equal(get('start-button').disabled,true);
  liveConfig.translation_ready=true;await run('loadLiveReadiness()');
  run("capture={session:{id:'live-a'},recognitionMode:'mlx'};phase='recording';resetTranscript();");
  emit(segment(1));emit(segment(2));emit(segment(3));
  emit(segment(3,'中文三'));emit(segment(1,'中文一'));emit(segment(2,'中文二'));
  assert.equal(get('live-ja').textContent,'日本語3');assert.equal(get('live-zh').textContent,'中文三');
  assert.equal(run('segments.size'),3);assert.equal(run("segments.get('s-1').zh"),'中文一');
  emit(segment(4));assert.equal(get('live-ja').textContent,'日本語3');assert.equal(get('live-zh').textContent,'中文三');
  assert.equal(get('latest-recognized-text').textContent,'日本語4');assert.equal(get('latest-recognized').hidden,false);
  emit(segment(4,'中文四'));assert.equal(get('latest-recognized').hidden,true);
  emit(segment(99,'错误上场','old-session'));assert.equal(run('segments.size'),4);assert.equal(get('live-zh').textContent,'中文四');
  const overlayDocument=makeDocument();let overlayChannel;
  class OverlayChannel{constructor(){overlayChannel=this;}postMessage(){}}
  const overlayContext=vm.createContext({window:{BroadcastChannel:OverlayChannel},BroadcastChannel:OverlayChannel,document:overlayDocument,Set,Number,String});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/overlay.js'),'utf8'),overlayContext);
  const snapshot=published.filter(p=>p.type==='caption').at(-1);
  overlayChannel.onmessage({data:snapshot});
  assert.equal(overlayDocument.getElementById('overlay-ja').textContent,get('live-ja').textContent);
  assert.equal(overlayDocument.getElementById('overlay-zh').textContent,get('live-zh').textContent);
  overlayChannel.onmessage({data:{...snapshot,session_id:'live-b',revision:1,ja:'新场',zh:'新场中文'}});
  overlayChannel.onmessage({data:{...snapshot,revision:9999,ja:'旧场迟到',zh:'旧场中文'}});
  assert.equal(overlayDocument.getElementById('overlay-ja').textContent,'新场','old-session snapshots cannot contaminate the overlay');

  const displayAcks=[];
  context.displaySocket={readyState:1,send(packet){displayAcks.push(JSON.parse(packet));}};
  run('capture.socket=displaySocket');
  emit(segment(5,'第五句'));emit(segment(6,'第六句'));
  assert.equal(displayAcks.length,0,'receipt is not yet a rendered caption');
  while(animationFrames.length){const frame=animationFrames.splice(0);for(const action of frame)action();}
  assert.equal(displayAcks.length,1);assert.equal(displayAcks[0].segment_id,'s-6','a pair replaced before the paint must not report itself as visible');
  assert.equal(displayAcks[0].type,'caption_displayed');assert.ok(displayAcks[0].render_delay_ms>=0);

  // A partial worklet chunk is emitted first, followed by an application flush
  // marker, server acknowledgement, and only then the stop control message.
  const order=[];let socketClosed=false;
  const current={session:{id:'stop-test'},recognitionMode:'mlx',frames:0,sampleRate:48000,stream:{getTracks:()=>[]},context:{state:'running',async close(){this.state='closed';}},node:{disconnect(){},port:{postMessage(message){assert.equal(message.type,'flush');const recording=new ArrayBuffer(137*4),transcription=new ArrayBuffer(68*2);context.workletChunk={recording,transcription,rms:0};run('onWorkletMessage(capture,workletChunk)');context.workletChunk={type:'flushed',frames:137};run('onWorkletMessage(capture,workletChunk)');}}},socket:{readyState:1,bufferedAmount:0,send(packet){if(packet instanceof Uint8Array){order.push(`pcm-${packet[0]}`);return;}const message=JSON.parse(packet);order.push(message.type);if(message.type==='flush')emit({type:'audio_ack',session_id:'stop-test',received_frames:137});if(message.type==='stop')emit({type:'audio_saved',session_id:'stop-test',session:{id:'stop-test',duration_ms:3,status:'backfilling'},pending_count:2});},close(){socketClosed=true;this.readyState=3;}}};
  current.savedPromise=new Promise(resolve=>{current.savedResolve=resolve;});
  current.audioSavedPromise=new Promise(resolve=>{current.audioSavedResolve=resolve;});
  context.testCapture=current;run("capture=testCapture;phase='recording';");
  await run('stopCapture()');
  assert.deepEqual(order,['pcm-0','pcm-1','flush','stop']);assert.equal(current.frames,137);
  assert.equal(run('phase'),'backfilling');assert.equal(socketClosed,false,'audio save confirmation must not close the subtitle-completion connection');
  assert.match(get('recording-status').textContent,/音频已保存/);
  assert.match(get('notice').textContent,/可以立即下载/);
  emit({type:'saved',session_id:'stop-test',session:{id:'stop-test',duration_ms:3,status:'completed'}});
  await new Promise(setImmediate);
  assert.equal(run('phase'),'idle');assert.equal(socketClosed,true);
  assert.match(get('recording-status').textContent,/音频与字幕已保存/);
  assert.equal(run('capture'),null);
  console.log('Live frontend tests passed: ready gating, 3/1/2 completion, session isolation, paired overlay, tail acknowledgement, early audio save and background completion.');
})().catch(error=>{console.error(error);process.exitCode=1;});
