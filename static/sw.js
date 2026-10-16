/* No offline cache: authenticated service records must remain private and current. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  const payload = event.data?.json() || {};
  const ticketId = Number(payload.ticket_id);
  const url = Number.isSafeInteger(ticketId) && ticketId > 0 ? `/?ticket=${ticketId}` : "/";
  event.waitUntil(self.registration.showNotification(payload.title || "FurnaceDesk", {
    body: payload.body || "Open FurnaceDesk to see your services.",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: payload.tag || "furnacedesk",
    data: { url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin);
  if (url.origin !== self.location.origin) return;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === url.origin && "navigate" in client) {
        await client.navigate(url.href);
        return client.focus();
      }
    }
    return self.clients.openWindow(url.href);
  })());
});
