# Source and scope

This browser component is licensed under GPL-3.0-only. Its fetch/XHR response
inspection is adapted from the open-source Cat Catch project by xifangczy:

- https://github.com/xifangczy/cat-catch
- Upstream commit: `2f84456f86230a88bbc8c67ba9ea15fcd64635d6`
- Reference: `catch-script/search.js`; upstream license is included as `LICENSE`.

Modified on 2026-10-30 for a dedicated, explicitly started X Space capture job.
Global JSON/typed-array/key searching, worker-script rewriting, unrelated media
discovery, request-header/cookie collection, and social controls were removed.
The local job bridge, tab scoping, URL association, expiry and MP3 handoff were
added. Original fetch responses and XHR response bodies remain available to the
player. Each capture is associated with the current Space; it is not a general
browsing monitor.

The MAIN script observes fetch/XHR only after activation of the dedicated task
tab, then restores its wrappers after completion, cancellation or expiry. It
does not rewrite workers. Requests entirely inside a worker, playback denied by
X, browser autoplay restrictions, or changes to the X player can require a
manual click on Play recording or the original Cat Catch export. This component
cannot guarantee that every Space URL is downloadable and does not bypass
subscription access or DRM.

Loading this directory into Edge/Chrome grants the host permissions shown in
`manifest.json`. It is intentionally not installed automatically. The extension
has no `cookies`, `debugger`, `webRequest`, `scripting` or all-sites permission.
Only a per-job capture token is kept in extension session storage; media keys
and playlist bodies are not written to extension storage or console logs.
