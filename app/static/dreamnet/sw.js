/* Dreamnet - service worker (adapted from the ANEEMA app package).
   Caches the app shell so the installed app opens fast. Pages and assets are
   network-first, so updates show up right away; API calls are never cached,
   so your writing always comes straight from the server. */

const CACHE = "dreamnet-shell-v25";
const SHELL = [
  "/",
  "/static/dreamnet/app.css?v=4",
  "/static/dreamnet/app.js?v=4",
  "/static/dreamnet/icon.svg",
  "/static/dreamnet/brand/mark-dark.svg",
  "/static/dreamnet/icon-192.png",
  "/static/dreamnet/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.all(SHELL.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const isShell = request.mode === "navigate" || url.pathname.startsWith("/static/dreamnet/");
  if (!isShell) return; // API, admin panel and everything else go straight to the network

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});
