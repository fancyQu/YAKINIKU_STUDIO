/* Shared local navigation: keep the source page without relying on browser history. */
(function (root) {
  "use strict";
  function createNavigation(host) {
    const pages = {
      "/": { name: "实时工作台", back: "/#recordings" },
      "/static/index.html": { name: "实时工作台", back: "/#recordings" },
      "/static/import.html": { name: "完整音频翻译", back: "/#workspace" },
      "/static/space-downloads.html": { name: "Space 下载", back: "/static/import.html" },
      "/static/providers.html": { name: "API 配置", back: "/static/import.html" },
      "/static/live-settings.html": { name: "直播设置", back: "/#workspace" },
      "/static/preview.html": { name: "项目预览", back: "/static/import.html" },
      "/static/overlay.html": { name: "独立字幕", back: "/#workspace" },
    };
    const location = host.location;
    const doc = host.document;
    const home = location.origin;
    let fallback = pages[location.pathname]?.back || "/#workspace";
    let fallbackLabel = "";
    function safeURL(raw, depth = 0) {
      if (typeof raw !== "string" || !raw || raw.length > 8000 || depth > 4 || /[\u0000-\u001f\\]/.test(raw)) return null;
      let url;
      try { url = new URL(raw, home); } catch { return null; }
      if (url.origin !== home || url.username || url.password || !pages[url.pathname]) return null;
      const clean = new URL(url.pathname, home);
      if (url.pathname === "/static/preview.html") {
        const id = url.searchParams.get("id");
        if (!id || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) return null;
        clean.searchParams.set("id", id);
      }
      if (url.pathname === "/static/providers.html" && ["asr", "text"].includes(url.searchParams.get("section"))) clean.searchParams.set("section", url.searchParams.get("section"));
      const source = url.searchParams.get("return_to");
      if (source && depth < 4) {
        const valid = safeURL(source, depth + 1);
        if (valid) clean.searchParams.set("return_to", valid);
      }
      if (/^#[a-zA-Z][\w-]{0,63}$/.test(url.hash)) clean.hash = url.hash;
      return clean.pathname + clean.search + clean.hash;
    }
    function pageKey(raw) {
      const url = new URL(raw, home);
      url.searchParams.delete("return_to");
      return url.pathname + url.search;
    }
    function currentURL() { return safeURL(location.href) || "/"; }
    function sourceURL() {
      const current = new URL(location.href);
      const explicit = safeURL(current.searchParams.get("return_to"));
      if (explicit && pageKey(explicit) !== pageKey(currentURL())) return explicit;
      const referrer = safeURL(doc?.referrer);
      if (referrer && pageKey(referrer) !== pageKey(currentURL())) return referrer;
      return fallback;
    }
    function pageName(raw) {
      const url = new URL(raw, home);
      if (url.pathname === "/static/providers.html") {
        if (url.searchParams.get("section") === "asr") return "ASR 音频识别配置";
        if (url.searchParams.get("section") === "text") return "校对与翻译配置";
      }
      if ((url.pathname === "/" || url.pathname === "/static/index.html") && url.hash === "#recordings") return "录制文件";
      return pages[url.pathname]?.name || "工作台";
    }
    function withReturnTo(raw) {
      if (typeof raw !== "string" || raw.startsWith("#")) return raw;
      const target = safeURL(raw);
      if (!target || pageKey(target) === pageKey(currentURL())) return raw;
      // If a menu item leads back to an ancestor, reuse that ancestor's context
      // instead of creating a circular A -> B -> A trail.
      let ancestor = new URL(currentURL(), home).searchParams.get("return_to");
      for (let depth = 0; ancestor && depth < 4; depth++) {
        if (pageKey(ancestor) === pageKey(target)) {
          const next = new URL(ancestor, home);
          next.hash = new URL(target, home).hash;
          return next.pathname + next.search + next.hash;
        }
        ancestor = new URL(ancestor, home).searchParams.get("return_to");
      }
      const url = new URL(target, home);
      url.searchParams.set("return_to", currentURL());
      return url.pathname + url.search + url.hash;
    }
    function link(text, href, className) {
      const item = doc.createElement("a");
      item.textContent = text;
      item.href = href;
      item.className = className;
      return item;
    }
    function render() {
      const source = sourceURL();
      for (const item of doc?.querySelectorAll("[data-return-link]") || []) {
        item.href = source;
        item.dataset.navigationBack = "true";
      }
      const mount = doc?.querySelector("[data-app-navigation]");
      if (!mount) return;
      const back = link("← 返回" + (fallbackLabel && source === fallback ? fallbackLabel : pageName(source)), source, "page-back-link");
      if (pageKey(source) === pageKey(currentURL())) back.textContent = "查看录制文件 →";
      back.dataset.navigationBack = "true";
      back.title = "返回打开此页之前的页面";
      const context = doc.createElement("div");
      context.className = "page-navigation-context";
      const current = doc.createElement("span");
      current.textContent = pageName(currentURL());
      current.className = "page-navigation-current";
      context.append(back, current);
      const tabs = doc.createElement("div");
      tabs.className = "page-navigation-tabs";
      const routes = [["实时工作台", "/#workspace"], ["完整音频", "/static/import.html"], ["Space 下载", "/static/space-downloads.html"], ["API 配置", "/static/providers.html"], ["直播设置", "/static/live-settings.html"]];
      for (const [name, path] of routes) {
        const item = link(name, path, "page-navigation-tab");
        const active = new URL(path, home).pathname === location.pathname || path === "/#workspace" && location.pathname === "/static/index.html";
        if (active) item.setAttribute("aria-current", "page");
        // Keep the audio-capturing page open while using another feature.
        if (["/", "/static/index.html"].includes(location.pathname) && !active) { item.target = "_blank"; item.rel = "noopener"; item.title = "在新标签页打开，保留直播工作台"; }
        tabs.append(item);
      }
      if (host.YakinikuTheme) tabs.append(host.YakinikuTheme.createToggle());
      mount.replaceChildren(context, tabs);
    }
    function saveScroll() {
      try { host.sessionStorage?.setItem("yakiniku-scroll:" + currentURL(), String(host.scrollY || 0)); } catch {}
    }
    function restoreScroll(event) {
      if (event?.persisted) return;
      try {
        const saved = host.sessionStorage?.getItem("yakiniku-scroll:" + currentURL());
        if (saved !== null && saved !== undefined && Number.isFinite(Number(saved))) host.requestAnimationFrame(() => host.scrollTo({ top: Math.max(0, Number(saved)), behavior: "instant" }));
      } catch {}
    }
    function onClick(event) {
      if (event.defaultPrevented || event.button > 0) return;
      const item = event.target?.closest?.("a[href]");
      if (!item || item.hasAttribute("download") || item.dataset.navigationBack === "true") { if (item) saveScroll(); return; }
      const raw = item.getAttribute("href");
      const safe = safeURL(raw);
      if (!safe) return;
      if (!item.dataset.navigationOriginal) item.dataset.navigationOriginal = raw;
      item.href = withReturnTo(item.dataset.navigationOriginal);
      saveScroll();
    }
    function install() {
      render();
      doc?.addEventListener("click", onClick, true);
      host.addEventListener("pagehide", saveScroll);
      host.addEventListener("pageshow", restoreScroll);
      host.addEventListener("popstate", render);
      host.addEventListener("hashchange", render);
      host.addEventListener("yakiniku:navigation-change", render);
    }
    function setFallback(url, label = "") {
      const safe = safeURL(url);
      if (safe && (safe !== fallback || label !== fallbackLabel)) { fallback = safe; fallbackLabel = label; render(); }
    }
    return { safeURL, withReturnTo, sourceURL, pageName, install, render, setFallback };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createNavigation };
  else { root.YakinikuNavigation = createNavigation(root); root.YakinikuNavigation.install(); }
})(globalThis);
