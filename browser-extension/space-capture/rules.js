/* SPDX-License-Identifier: GPL-3.0-only */
(function (root) {
  "use strict";
  const SPACE_ID = /^[A-Za-z0-9]{13}$/;
  const MAX_TEXT = 2 * 1024 * 1024;
  function parsed(value, base) { try { return new URL(value, base); } catch { return null; } }
  function xURL(value) {
    const u = parsed(value);
    return u && u.protocol === "https:" && !u.username && !u.password && !u.port && ["x.com", "twitter.com"].includes(u.hostname) ? u : null;
  }
  function localPage(value) {
    const u = parsed(value);
    return u && u.protocol === "http:" && !u.username && !u.password && ["127.0.0.1", "localhost"].includes(u.hostname) && u.pathname === "/static/space-downloads.html" ? u : null;
  }
  function canonicalSpace(value) {
    const u = xURL(value);
    const match = u && u.pathname.match(/^\/i\/spaces\/([A-Za-z0-9]{13})\/?$/);
    return match ? {id: match[1], url: `https://x.com/i/spaces/${match[1]}`} : null;
  }
  function cdnURL(value) {
    const u = parsed(value);
    return u && u.protocol === "https:" && !u.username && !u.password && !u.port && u.hostname.endsWith(".video.pscp.tv") ? u : null;
  }
  function cdnRoot(value) { const u = cdnURL(value); return u ? new URL(".", u).href : null; }
  function keyURL(value, spaceId) {
    const u = xURL(value);
    return u && u.pathname === "/i/live_video_stream/key" && u.searchParams.get("broadcast_id") === spaceId ? u.href : null;
  }
  function graphURL(value, spaceId) {
    const u = xURL(value);
    if (!u || !u.pathname.endsWith("/AudioSpaceById")) return false;
    try { return JSON.parse(u.searchParams.get("variables") || "{}").id === spaceId; } catch { return false; }
  }
  function statusURL(value, mediaKey) {
    const u = xURL(value);
    return !!(u && mediaKey && u.pathname.endsWith(`/live_video_stream/status/${mediaKey}`));
  }
  function playlist(value, text, spaceId, knownRoots = []) {
    const u = cdnURL(value);
    if (!u || typeof text !== "string" || text.length > MAX_TEXT || !text.replace(/^\uFEFF/, "").trimStart().startsWith("#EXTM3U")) return false;
    if (!/^#EXTINF:/m.test(text) || !/^#EXT-X-ENDLIST\r?$/m.test(text)) return false;
    const keyLines = text.split(/\r?\n/).filter(l => l.startsWith("#EXT-X-KEY:") && !l.includes("METHOD=NONE"));
    if (keyLines.length) {
      return keyLines.every(line => {
        const match = line.match(/(?:^|,)URI="([^"]+)"/);
        return !!(match && keyURL(parsed(match[1], u.href)?.href, spaceId));
      });
    }
    return knownRoots.includes(cdnRoot(u.href));
  }
  function validJob(job) {
    const space = canonicalSpace(job?.source_url);
    return !!(space && space.id === job.space_id && /^[a-f0-9]{32}$/.test(job.id || "") && /^[A-Za-z0-9_-]{20,200}$/.test(job.capture_token || ""));
  }
  function validKey(value) {
    if (typeof value !== "string" || !/^[A-Za-z0-9+/]{22}==$/.test(value)) return false;
    try { return atob(value).length === 16; } catch { return false; }
  }
  function playButton(document, href, spaceId) {
    if (canonicalSpace(href)?.id !== spaceId) return null;
    const labels = new Set(["播放录音", "Play recording", "録音を再生"]);
    const found = Array.from(document.querySelectorAll('[role="menu"] button, [role="dialog"] button')).filter(b => !b.disabled && labels.has((b.textContent || "").trim()));
    return found.length === 1 ? found[0] : null;
  }
  const rules = {SPACE_ID, MAX_TEXT, parsed, xURL, localPage, canonicalSpace, cdnURL, cdnRoot, keyURL, graphURL, statusURL, playlist, validJob, validKey, playButton};
  root.SpaceCaptureRules = rules;
  if (typeof module !== "undefined" && module.exports) module.exports = rules;
})(globalThis);
