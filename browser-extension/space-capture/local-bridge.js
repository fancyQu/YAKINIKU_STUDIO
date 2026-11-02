/* SPDX-License-Identifier: GPL-3.0-only */
(function () {
  "use strict";
  if (!SpaceCaptureRules.localPage(location.href)) return;
  const reply = data => window.postMessage({channel: "yakiniku-space-extension", ...data}, location.origin);
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.channel !== "yakiniku-space-ui") return;
    const {type, request_id, job, job_id} = event.data;
    if (!["hello", "start", "cancel"].includes(type)) return;
    chrome.runtime.sendMessage({type: `space:${type}`, job, job_id}, result => {
      if (chrome.runtime.lastError) { reply({type: "error", request_id, message: "浏览器扩展连接已断开，请重新加载扩展和本页。"}); return; }
      reply({...result, request_id});
    });
  });
  chrome.runtime.onMessage.addListener(message => {
    if (message.type === "space:notice") reply({type: message.level === "error" ? "error" : "status", job_id: message.job_id, message: message.message});
  });
  reply({type: "ready"});
})();
