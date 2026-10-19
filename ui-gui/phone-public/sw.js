// Installed so iOS treats the page as an app; it caches nothing, so a phone never shows a stale build.
// Push notifications will be handled here in a later phase.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
