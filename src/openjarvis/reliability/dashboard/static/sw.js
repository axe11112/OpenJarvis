/* Sir's service worker.
 *
 * Two jobs, and deliberately no third.
 *
 * **Wake the phone.** A push arrives with no payload — by design. Web Push can
 * carry encrypted data, and carrying it would mean incident details sitting in
 * Apple's or Google's push service on their way to the handset. Instead the
 * push is an empty knock: it wakes this worker, which asks the Mac over the
 * tailnet what is actually wrong. The private network stays the only place the
 * details travel.
 *
 * **Open the call.** Tapping the notification focuses an existing Sir window if
 * one is open, and otherwise opens the call screen.
 *
 * There is no offline caching of application data. A dashboard that shows a
 * cached incident from an hour ago during an outage is worse than one that
 * admits it cannot reach the Mac.
 */

/* Bump this on every asset change.
 *
 * A cached call screen is worse than no call screen: the phone keeps running
 * whichever voice.js it installed first, so a fix on the Mac appears to do
 * nothing and the bug looks unfixable from the operator's side. `skipWaiting`
 * plus `clients.claim` below mean a new version takes over on the next load
 * rather than after every tab is closed. */
const SHELL = "sir-shell-v3-mediarecorder";

// Only the files needed to render "I cannot reach the Mac" and a call screen.
const SHELL_FILES = [
  "/voice",
  "/static/voice.css",
  "/static/voice.js",
  "/static/wiz.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== SHELL).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

/* Network-first for the shell; never for data.
 *
 * API responses are not cached at any age: every one of them is a claim about
 * production right now, and a stale claim about production is the specific
 * failure this whole system exists to prevent. */
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // The page and its script are always fetched fresh when the Mac is reachable.
  // Caching them is only a courtesy for a phone that has lost the tailnet.
  if (url.pathname === "/sw.js") return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.ok && SHELL_FILES.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || offline()))
  );
});

function offline() {
  return new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
      "<body style='background:#0b0d10;color:#e8edf3;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0;text-align:center'>" +
      "<div><h1 style='font-size:20px'>Sir is out of reach</h1>" +
      "<p style='color:#93a1b3'>Your phone cannot see the Mac. Check Tailscale is connected.</p></div>",
    { headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

/* ------------------------------------------------------------- push */

self.addEventListener("push", (event) => {
  event.waitUntil(
    (async () => {
      let title = "Sir needs your attention";
      let body = "Tap to answer.";
      try {
        // The knock carries nothing; the detail comes from the Mac, privately.
        const response = await fetch("/api/voice/pending", { cache: "no-store" });
        const payload = await response.json();
        if (payload && payload.call && payload.call.detail) body = payload.call.detail;
      } catch (_) {
        // Out of range: still ring. A notification that says less is better
        // than one that never arrives.
      }
      await self.registration.showNotification(title, {
        body,
        icon: "/static/icon-192.png",
        badge: "/static/icon-192.png",
        // One notification per incident: re-using the tag replaces rather than
        // stacks, so a run of related events cannot fill the lock screen.
        tag: "sir-call",
        renotify: true,
        requireInteraction: true,
        data: { url: "/voice" },
      });
    })()
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/voice";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (client.url.includes("/voice") && "focus" in client) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
