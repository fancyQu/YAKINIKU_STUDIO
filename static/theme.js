/* Apply before first paint; appearance preferences never touch live configuration. */
(function (host) {
  "use strict";
  const doc = host.document;
  const storageKey = "yakiniku-theme";
  const validTheme = (value) => value === "dark" || value === "light";
  let preference = null;
  let system;
  try { preference = host.localStorage.getItem(storageKey); } catch {}
  if (!validTheme(preference)) preference = null;
  try { system = host.matchMedia("(prefers-color-scheme: dark)"); } catch {}
  let theme;
  const icons = {
    dark: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    light: '<path d="M20.5 13.1A8.5 8.5 0 0 1 10.9 3.5a8.5 8.5 0 1 0 9.6 9.6Z"/>',
  };
  function updateToggle(button) {
    const target = theme === "dark" ? "白天模式" : "黑夜模式";
    button.setAttribute("aria-label", "切换到" + target);
    button.title = "切换到" + target + "，自动记住选择";
    button.querySelector(".theme-toggle-label").textContent = target;
    button.querySelector(".theme-toggle-icon").innerHTML = icons[theme];
  }
  function apply() {
    theme = preference || (system?.matches ? "dark" : "light");
    doc.documentElement.dataset.theme = theme;
    doc.documentElement.style.colorScheme = theme;
    const meta = doc.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#181818" : "#f5f3ed";
    for (const button of doc.querySelectorAll("[data-theme-toggle]")) updateToggle(button);
  }
  function toggle() {
    preference = theme === "dark" ? "light" : "dark";
    try { host.localStorage.setItem(storageKey, preference); } catch {}
    apply();
  }
  function createToggle() {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "theme-toggle";
    button.setAttribute("data-theme-toggle", "");
    button.innerHTML = '<svg class="theme-toggle-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"></svg><span class="theme-toggle-label"></span>';
    updateToggle(button);
    return button;
  }
  apply();
  doc.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-theme-toggle]")) toggle();
  });
  const followSystem = () => { if (!preference) apply(); };
  if (system?.addEventListener) system.addEventListener("change", followSystem);
  else system?.addListener?.(followSystem);
  host.addEventListener("storage", (event) => {
    if (event.key !== storageKey && event.key !== null) return;
    // sessionStorage changes must not override the appearance preference.
    try { if (event.storageArea && event.storageArea !== host.localStorage) return; } catch {}
    preference = validTheme(event.newValue) ? event.newValue : null;
    apply();
  });
  // A page restored from the back/forward cache may have missed storage events.
  host.addEventListener("pageshow", () => {
    try {
      const saved = host.localStorage.getItem(storageKey);
      preference = validTheme(saved) ? saved : null;
    } catch {}
    apply();
  });
  host.YakinikuTheme = { createToggle };
})(globalThis);
