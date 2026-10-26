// Installed so iOS treats the page as an app; it caches nothing, so a phone never shows a stale build.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

// A notification from the desktop (Web Push): only what it carries is shown; nothing is fetched.
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  const title = typeof data.title === "string" && data.title ? data.title : "Astra";
  const body = typeof data.body === "string" ? data.body : "";
  const runtime = typeof data.runtime === "string" ? data.runtime : "";
  event.waitUntil(self.registration.showNotification(title, {
    body, tag: runtime || "astra", data: { url: runtime ? `./?runtime=${encodeURIComponent(runtime)}` : "./" },
  }));
});

// Opening it brings the app forward on that conversation.
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL(event.notification.data && event.notification.data.url || "./", self.registration.scope).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    const open = windows[0];
    return open ? open.navigate(url).then(client => (client || open).focus()) : self.clients.openWindow(url);
  }));
});
