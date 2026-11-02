import { jsonRequest } from "./project-utils.js?v=ui-navigation-1";

export function createSubtitleSettings({ onChange = () => {}, onError = () => {} } = {}) {
  const $ = (id) => document.getElementById(id);
  let saved = null;
  let dirty = false;
  let busy = false;
  let localLock = false;
  let serviceLock = false;
  let pollTimer = null;
  let revision = 0;
  let operation = "";
  const locked = () => localLock || serviceLock;
  function status(message, level = "info") {
    $("subtitle-settings-status").textContent = message;
    $("subtitle-settings-status").dataset.level = level;
  }
  function draft() {
    return {
      enabled: $("subtitle-split-enabled").checked,
      max_chars: Number($("subtitle-max-chars").value),
      max_seconds: Number($("subtitle-max-seconds").value),
    };
  }
  function render() {
    const disabled = busy || locked() || !saved;
    $("subtitle-split-enabled").disabled = disabled;
    $("subtitle-max-chars").disabled = disabled || !$("subtitle-split-enabled").checked;
    $("subtitle-max-seconds").disabled = disabled || !$("subtitle-split-enabled").checked;
    $("save-subtitle-settings").disabled = disabled || !dirty;
    $("reload-subtitle-settings").disabled = busy || localLock;
    $("save-subtitle-settings").textContent = operation === "save" ? "正在保存…" : "保存分句设置";
    $("reload-subtitle-settings").textContent = operation === "load" ? "正在读取…" : dirty ? "放弃修改并重新读取" : "重新读取";
    $("subtitle-settings-form").setAttribute?.("aria-busy", String(busy));
    $("save-subtitle-settings").setAttribute?.("aria-busy", String(operation === "save"));
    $("reload-subtitle-settings").setAttribute?.("aria-busy", String(operation === "load"));
    $("subtitle-settings-badge").textContent = locked() ? "任务进行中" : !saved ? "尚未读取" : dirty ? "修改待保存" : "已保存";
    $("subtitle-settings-badge").dataset.stage = saved && !dirty && !locked() ? "completed" : "queued";
  }
  function notify() { render(); onChange(); }
  function describe() {
    if (locked()) status("正在录制或处理音频，完成后可调整分句设置。未保存的修改会保留。");
    else if (dirty) status("分句设置已修改，请保存后再开始新项目。");
    else if (saved) status(saved.enabled ? `已保存：每句目标 ${saved.max_chars} 字，最长 ${saved.max_seconds} 秒。仅用于新导入和新精准校验项目。` : "已保存：保留识别服务原始分句。仅用于新生成的字幕。", "success");
  }
  function fill(result) {
    revision += 1;
    saved = { enabled: Boolean(result.enabled), max_chars: result.max_chars, max_seconds: result.max_seconds };
    serviceLock = Boolean(result.locked);
    $("subtitle-split-enabled").checked = saved.enabled;
    $("subtitle-max-chars").value = String(saved.max_chars);
    $("subtitle-max-seconds").value = String(saved.max_seconds);
    dirty = false;
  }
  function changed() {
    dirty = !saved || JSON.stringify(draft()) !== JSON.stringify(saved);
    describe(); notify();
  }
  async function refreshLock() {
    const requestRevision = revision;
    try {
      // This endpoint includes recording reservations, subtitle retries and
      // model preparation; polling only its lock preserves the local draft.
      const result = await jsonRequest("/api/subtitle-settings");
      if (requestRevision !== revision || busy) return;
      const nextLock = Boolean(result.locked);
      if (nextLock !== serviceLock) { serviceLock = nextLock; if (!busy) describe(); notify(); }
    } catch {}
    finally { pollTimer = setTimeout(refreshLock, 4000); }
  }
  async function load() {
    if (busy || localLock) return;
    revision += 1;
    busy = true; operation = "load"; status("正在读取字幕分句设置…"); notify();
    try { fill(await jsonRequest("/api/subtitle-settings")); describe(); }
    catch (error) { status(error.message, "error"); onError(error.message); }
    finally {
      busy = false; operation = ""; notify();
      if (pollTimer === null) pollTimer = setTimeout(refreshLock, 4000);
    }
  }
  async function save(event) {
    event?.preventDefault();
    if (busy || locked() || !saved || !dirty) return;
    const values = draft();
    if (!Number.isInteger(values.max_chars) || values.max_chars < 8 || values.max_chars > 80) { status("每句目标字数请填写 8–80 的整数。", "error"); return; }
    if (!Number.isFinite(values.max_seconds) || values.max_seconds < 2 || values.max_seconds > 15) { status("每句最长显示时间请填写 2–15 秒。", "error"); return; }
    revision += 1;
    busy = true; operation = "save"; status("正在保存字幕分句设置…"); notify();
    try {
      fill(await jsonRequest("/api/subtitle-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) }));
      describe();
    } catch (error) { status(error.message, "error"); }
    finally { busy = false; operation = ""; notify(); }
  }
  $("subtitle-split-enabled").addEventListener("change", changed);
  for (const id of ["subtitle-max-chars", "subtitle-max-seconds"]) $(id).addEventListener("input", changed);
  $("subtitle-settings-form").addEventListener("submit", save);
  $("reload-subtitle-settings").addEventListener("click", () => {
    if (dirty && typeof window !== "undefined" && !window.confirm("重新读取会放弃本页尚未保存的字幕分句修改。确定重新读取吗？")) return;
    return load();
  });
  return {
    load,
    get ready() { return Boolean(saved && !dirty && !busy && !locked()); },
    get loaded() { return Boolean(saved); },
    get dirty() { return dirty; },
    get busy() { return busy; },
    get locked() { return locked(); },
    setLocked(value) { const next = Boolean(value); if (localLock !== next) { localLock = next; if (!busy) describe(); } render(); },
  };
}
