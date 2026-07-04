// Service worker minimal — condition requise par Android/Chrome pour rendre l'appli
// installable (PWA). L'appli est fondamentalement temps-reel (WebSocket), donc pas
// de mise en cache agressive du contenu dynamique : on se contente de laisser passer
// les requetes reseau normalement, et de mettre en cache les icones statiques.
const CACHE = "devllma-static-v1";
const STATIC_ASSETS = ["/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS)));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request))
    );
  }
  // Tout le reste (page principale, /ws, /stats, /file...) : reseau direct, jamais de cache.
});
