const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(id = "") { this.id = id; this.value = ""; this.dataset = {}; this.children = []; this.events = {}; this.textContent = ""; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...items) { for (const item of items) { item.remove(); item.parent = this; this.children.push(item); } }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; this.detachCount = (this.detachCount || 0) + 1; } }
  insertBefore(item, reference) { item.remove(); item.parent = this; const index = reference ? this.children.indexOf(reference) : this.children.length; this.children.splice(index, 0, item); }
  replaceChildren(...items) { this.children = items.flatMap((x) => x.id === "fragment" ? x.children : [x]); }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  async emit(type) { for (const fn of this.events[type] || []) await fn({ preventDefault() {} }); }
}
const elements = new Map(), get = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
const listeners = {}, messages = [], requests = [], timers = new Map();
let jobs = [], sequence = 0, failStart = false, postGate = null, listGate = null, listFail = false, cancelGate = null, cancelFail = false, helloGate = null, failHello = false;
const origin = "http://127.0.0.1:8765", spaceId = "1dKrPrwgXpXJX", token = "fixture-private-capability";
const window = {
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  postMessage(message, target) {
    assert.equal(target, origin); messages.push(message);
    queueMicrotask(async () => {
      if (message.type === "hello" && helloGate) await helloGate;
      const reply = { channel: "yakiniku-space-extension", request_id: message.request_id,
        type: message.type === "hello" ? failHello ? "error" : "ready" : failStart && message.type === "start" ? "error" : "started", message: "fixture start failed" };
      for (const fn of listeners.message || []) {
        fn({source:window, origin:"https://x.com", data:reply});
        fn({source:window, origin, data:reply});
      }
    });
  },
};
const context = vm.createContext({
  window, document:{getElementById:get, createElement:(tag)=>new Element(tag),createDocumentFragment:()=>new Element("fragment")},
  location:{origin}, URL, crypto:{randomUUID:()=>`request-${++sequence}`},
  localStorage:{setItem(){throw new Error("Credentials must not be persisted");}},
  setTimeout(fn) { const id = ++sequence; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
  clockTime:(ms)=>`${ms / 1000}s`,
  jsonRequest:async (url, options={}) => {
    requests.push({url,options});
    if (options.method === "POST" && url === "/api/space-downloads") {
      if (postGate) await postGate;
      return {id:"a".repeat(32),space_id:spaceId,source_url:`https://x.com/i/spaces/${spaceId}`,capture_token:token};
    }
    if (options.method === "POST") {
      if (url.endsWith("/cancel")) {
        if (cancelGate) await cancelGate;
        if (cancelFail) throw new Error("取消任务失败，请重试。");
        jobs = jobs.map((job) => url.includes(job.id) ? { ...job, stage: "cancelled" } : job);
      }
      return {};
    }
    if (listGate) await listGate;
    if (listFail) throw new Error("下载任务读取失败，请重试。");
    return {jobs};
  },
});
const source = fs.readFileSync(path.join(__dirname,"../static/space-downloads.js"),"utf8").replace(/^import .*\n/gm,"").replace(/^export /gm,"");
const run = (text) => vm.runInContext(text,context);
function walk(element) { return [element, ...element.children.flatMap(walk)]; }
(async () => {
  run(source); await new Promise(setImmediate);
  assert.equal(get("space-start").disabled, false, "matching local extension reply enables start");
  assert.throws(()=>run('canonicalSpaceURL("https://x.com/home")'));
  assert.throws(()=>run('canonicalSpaceURL("https://x.com/i/spaces/1dKrPrwgXpXJX/likes")'));
  assert.throws(()=>run('canonicalSpaceURL("https://x.com.evil.example/i/spaces/1dKrPrwgXpXJX")'));
  assert.throws(()=>run('canonicalSpaceURL("")'), /请粘贴有效的 X Space 回放链接/);
  assert.throws(()=>run('canonicalSpaceURL("不是网址")'), /请粘贴有效的 X Space 回放链接/);
  assert.equal(run('canonicalSpaceURL("https://twitter.com/i/spaces/1dKrPrwgXpXJX?s=20")'),`https://x.com/i/spaces/${spaceId}`);
  get("space-url").value = `https://x.com/i/spaces/${spaceId}?s=20`;
  let release; postGate = new Promise(resolve=>{release=resolve;});
  const starting = get("space-form").emit("submit"); await new Promise(setImmediate);
  await get("space-form").emit("submit");
  assert.equal(requests.filter(r=>r.options.method==="POST").length,1,"repeat clicks cannot create duplicate tasks");
  release(); await starting; postGate=null;
  const startMessage=messages.find(m=>m.type==="start");
  assert.equal(startMessage.job.capture_token,token);
  assert.deepEqual(JSON.parse(requests.find(r=>r.options.method==="POST").options.body),{url:`https://x.com/i/spaces/${spaceId}`});
  assert.ok([...elements.values()].every(e=>!e.textContent.includes(token)),"capability never appears in page text");
  jobs=[{id:"a".repeat(32),space_id:spaceId,stage:"capturing",message:"等待密钥",files:[{name:"audio.mp3"}]}];
  await run("loadJobs()");
  assert.equal(walk(get("space-jobs")).filter(e=>e.download).length,0,"capturing never exposes an unverified file");
  jobs=[{...jobs[0],stage:"completed",duration_ms:2000,segments:2,files:[{name:"audio.mp3"},{name:"audio.json"}]}];
  await run("loadJobs()");
  assert.equal(walk(get("space-jobs")).filter(e=>e.download).length,2,"only completed tasks offer MP3 and report");
  assert.ok(walk(get("space-jobs")).some(e=>e.id==="audio" && e.preload==="none"),"result audio does not autoplay");
  const existingPlayer = walk(get("space-jobs")).find((element) => element.id === "audio");
  existingPlayer.currentTime = 17; existingPlayer.paused = false;
  const completedJob = jobs[0];
  jobs = [{id:"b".repeat(32),space_id:spaceId,stage:"downloading",message:"下载1/10",files:[]}, completedJob];
  await run("loadJobs()");
  jobs[0] = {...jobs[0],message:"下载2/10"};
  await run("loadJobs()");
  assert.equal(walk(get("space-jobs")).find((element) => element.id === "audio"), existingPlayer, "progress updates reuse completed players");
  assert.equal(existingPlayer.currentTime, 17);
  assert.equal(existingPlayer.paused, false);
  assert.equal(existingPlayer.detachCount || 0, 0, "a playing result is never detached for another job's update");
  assert.equal(existingPlayer.parent.parent, get("space-jobs"));
  jobs = [completedJob]; await run("loadJobs()");
  assert.equal(existingPlayer.detachCount || 0, 0, "removing an earlier task leaves the completed player attached");
  failStart=true; get("space-url").value=`https://x.com/i/spaces/${spaceId}`;
  await get("space-form").emit("submit");
  assert.ok(requests.some(r=>r.url.endsWith("/cancel")),"failed browser start revokes the new task");
  assert.ok(requests.every(r=>r.url.startsWith("/api/space-downloads")),"no social or unrelated APIs are called");

  let releaseHello; helloGate = new Promise((resolve) => { releaseHello = resolve; });
  const checking = get("extension-check").emit("click");
  assert.equal(get("extension-check").disabled, true);
  assert.equal(get("extension-check").attributes["aria-busy"], "true");
  assert.match(get("extension-check").textContent, /检测中/);
  const hellos = messages.filter((message) => message.type === "hello").length;
  await get("extension-check").emit("click");
  assert.equal(messages.filter((message) => message.type === "hello").length, hellos, "duplicate connection checks are ignored");
  releaseHello(); await checking; helloGate = null;
  assert.equal(get("extension-check").attributes["aria-busy"], "false");
  assert.match(get("space-notice").textContent, /已连接/);
  failHello = true; await get("extension-check").emit("click"); failHello = false;
  assert.equal(get("space-start").disabled, true);
  assert.equal(get("space-notice").dataset.level, "error");
  assert.match(get("space-notice").textContent, /首次使用说明/);

  let releaseList; listGate = new Promise((resolve) => { releaseList = resolve; });
  const refreshing = get("space-refresh").emit("click");
  assert.equal(get("space-refresh").disabled, true);
  assert.equal(get("space-refresh").attributes["aria-busy"], "true");
  const beforeRepeatRefresh = requests.length;
  await get("space-refresh").emit("click");
  assert.equal(requests.length, beforeRepeatRefresh);
  releaseList(); await refreshing; listGate = null;
  assert.equal(get("space-refresh").disabled, false);
  assert.match(get("space-notice").textContent, /列表已更新/);
  listFail = true; await get("space-refresh").emit("click"); listFail = false;
  assert.equal(get("space-notice").dataset.level, "error");
  assert.equal(get("space-refresh").disabled, false, "a failed refresh is retryable");

  jobs = [{id:"a".repeat(32),space_id:spaceId,stage:"capturing",files:[]}];
  await run("loadJobs()");
  const cancel = walk(get("space-jobs")).find((element) => element.textContent === "取消任务");
  let releaseCancel; cancelGate = new Promise((resolve) => { releaseCancel = resolve; });
  const cancelling = cancel.emit("click");
  assert.equal(cancel.disabled, true); assert.match(cancel.textContent, /正在取消/);
  const beforeRepeatCancel = requests.length;
  await cancel.emit("click");
  assert.equal(requests.length, beforeRepeatCancel);
  releaseCancel(); await cancelling; cancelGate = null;
  assert.match(get("space-notice").textContent, /下载任务已取消/);
  assert.equal(walk(get("space-jobs")).some((element) => element.textContent === "取消任务"), false);
  jobs = [{...jobs[0],stage:"capturing"}]; await run("loadJobs()");
  const failedCancel = walk(get("space-jobs")).find((element) => element.textContent === "取消任务");
  cancelFail = true; await failedCancel.emit("click"); cancelFail = false;
  assert.equal(failedCancel.disabled, false);
  assert.equal(failedCancel.attributes["aria-busy"], "false");
  assert.equal(get("space-notice").dataset.level, "error");
  console.log("Space download UI behavior tests passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
