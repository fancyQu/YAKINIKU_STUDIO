/* SPDX-License-Identifier: GPL-3.0-only */
(function () {
  "use strict";
  let config = null, observer = null, timer = null, clicked = false;
  function stop() {
    if (config) window.postMessage({channel: "yakiniku-space-control", type: "deactivate", nonce: config.nonce}, location.origin);
    config = null;
    observer?.disconnect();
    clearTimeout(timer);
  }
  function play() {
    if (!config || clicked) return;
    const button = SpaceCaptureRules.playButton(document, location.href, config.space_id);
    if (button) { clicked = true; observer?.disconnect(); button.click(); }
  }
  chrome.runtime.onMessage.addListener(message => { if (message.type === "space:stop") stop(); });
  window.addEventListener("message", event => {
    if (!config || event.source !== window || event.origin !== location.origin || event.data?.channel !== "yakiniku-space-page" || event.data.nonce !== config.nonce) return;
    if (config.expires <= Date.now()) { stop(); chrome.runtime.sendMessage({type: "space:expired"}); return; }
    chrome.runtime.sendMessage({type: "space:capture", nonce: config.nonce, payload: event.data.payload}, response => {
      if (chrome.runtime.lastError || response?.active === false) stop();
    });
  });
  chrome.runtime.sendMessage({type: "space:config"}, response => {
    if (chrome.runtime.lastError || !response?.active) return;
    config = response;
    window.postMessage({channel: "yakiniku-space-control", type: "activate", space_id: config.space_id, nonce: config.nonce, expires: config.expires}, location.origin);
    observer = new MutationObserver(play);
    observer.observe(document, {childList: true, subtree: true});
    play();
    timer = setTimeout(() => { stop(); chrome.runtime.sendMessage({type: "space:expired"}); }, Math.max(0, config.expires - Date.now()));
    setTimeout(() => observer?.disconnect(), 20000);
  });
})();
