/* SPDX-License-Identifier: GPL-3.0-only
 * Fetch/XHR response-cloning technique adapted from cat-catch search.js.
 * The upstream global deep-search and worker rewriting are deliberately removed.
 */
(function () {
  "use strict";
  const R = SpaceCaptureRules;
  const originalFetch = window.fetch;
  const originalOpen = XMLHttpRequest.prototype.open;
  const requests = new WeakMap(), watched = new WeakSet();
  let state = null;
  function active() { return state && state.expires > Date.now(); }
  function emit(payload) {
    if (active()) window.postMessage({channel: "yakiniku-space-page", nonce: state.nonce, payload}, location.origin);
  }
  function classify(value) {
    if (!active()) return null;
    const url = R.parsed(value, location.href)?.href;
    if (!url) return null;
    if (R.graphURL(url, state.space_id)) return {kind: "graph", url};
    if (R.statusURL(url, state.media_key)) return {kind: "status", url};
    if (R.keyURL(url, state.space_id)) return {kind: "key", url};
    if (R.cdnURL(url) && /\.m3u8$/i.test(new URL(url).pathname) && state.roots.includes(R.cdnRoot(url))) return {kind: "playlist", url};
    return null;
  }
  function jsonResult(kind, data) {
    if (!active()) return;
    if (kind === "graph") {
      const metadata = data?.data?.audioSpace?.metadata;
      if (metadata?.rest_id === state.space_id && typeof metadata.media_key === "string") state.media_key = metadata.media_key;
    }
    if (kind === "status") {
      const url = data?.source?.noRedirectPlaybackUrl || data?.source?.location;
      const root = R.cdnRoot(url);
      if (root && !state.roots.includes(root)) {
        state.roots.push(root);
        emit({kind: "scope", url, space_id: state.space_id});
      }
    }
  }
  function textResult(item, text) {
    if (!active() || typeof text !== "string" || text.length > R.MAX_TEXT) return;
    if (item.kind === "playlist") {
      if (R.playlist(item.url, text, state.space_id, state.roots)) emit({kind: "playlist", url: item.url, text});
    } else {
      try { jsonResult(item.kind, JSON.parse(text)); } catch { /* not a JSON response */ }
    }
  }
  function keyResult(url, buffer) {
    if (!active() || buffer.byteLength !== 16 || !R.keyURL(url, state.space_id)) return;
    const key = btoa(String.fromCharCode(...new Uint8Array(buffer)));
    emit({kind: "key", url, key_base64: key});
  }
  async function inspect(response, item, generation) {
    if (!response.ok || !active() || state.nonce !== generation) return;
    const length = Number(response.headers?.get("content-length"));
    if (length > R.MAX_TEXT) return;
    if (item.kind === "key") { keyResult(item.url, await response.arrayBuffer()); return; }
    textResult(item, await response.text());
  }
  const wrappedFetch = async function (...args) {
    const url = typeof args[0] === "string" || args[0] instanceof URL ? String(args[0]) : args[0]?.url;
    const item = classify(url);
    const generation = state?.nonce;
    const response = await originalFetch.apply(this, args);
    if (active() && state.nonce === generation) {
      const associated = item || classify(url);
      if (associated) {
        try { inspect(response.clone(), associated, generation).catch(() => {}); } catch { /* preserve original response */ }
      }
    }
    return response;
  };
  function onXHR(xhr) {
    const saved = requests.get(xhr);
    if (!saved || !active() || saved.generation !== state.nonce || xhr.status < 200 || xhr.status >= 300) return;
    const item = saved.item || classify(saved.url);
    if (!item) return;
    try {
      if (item.kind === "key") {
        if (xhr.responseType === "arraybuffer") keyResult(item.url, xhr.response);
        else if (xhr.responseType === "blob") xhr.response.arrayBuffer().then(bytes => keyResult(item.url, bytes)).catch(() => {});
        else if ((!xhr.responseType || xhr.responseType === "text") && xhr.responseText.length === 16) keyResult(item.url, Uint8Array.from(xhr.responseText, c => c.charCodeAt(0) & 255).buffer);
      } else if (xhr.responseType === "json") jsonResult(item.kind, xhr.response);
      else if (!xhr.responseType || xhr.responseType === "text") textResult(item, xhr.responseText);
    } catch { /* never change player errors */ }
  }
  const wrappedOpen = function (method, url, ...rest) {
    const result = originalOpen.call(this, method, url, ...rest);
    const item = classify(url);
    requests.set(this, active() ? {item, url, generation: state.nonce} : null);
    if (!watched.has(this)) { watched.add(this); this.addEventListener("load", () => onXHR(this)); }
    return result;
  };
  window.fetch = wrappedFetch;
  XMLHttpRequest.prototype.open = wrappedOpen;
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.channel !== "yakiniku-space-control") return;
    const data = event.data;
    if (data.type === "activate" && R.SPACE_ID.test(data.space_id || "") && typeof data.nonce === "string" && R.canonicalSpace(location.href)?.id === data.space_id && data.expires > Date.now() && data.expires <= Date.now() + 10 * 60 * 1000 + 1000) {
      state = {space_id: data.space_id, nonce: data.nonce, expires: data.expires, roots: [], media_key: null};
    }
    if (data.type === "deactivate" && state && data.nonce === state.nonce) {
      state = null;
      if (window.fetch === wrappedFetch) window.fetch = originalFetch;
      if (XMLHttpRequest.prototype.open === wrappedOpen) XMLHttpRequest.prototype.open = originalOpen;
    }
  });
})();
