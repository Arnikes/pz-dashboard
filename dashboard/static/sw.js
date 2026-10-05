/* Served at /sw.js by the HTTP server with a content-derived revision. */
"use strict";
const CACHE_PREFIX = "pz-pult-shell-";
const CACHE_NAME = CACHE_PREFIX + "__PZ_REVISION__";
const ASSETS = __PZ_ASSETS__;
const PUBLIC_SHELL = new Set(ASSETS);

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // Reject redirects/errors so a proxy login page cannot become the shell.
    await Promise.all(ASSETS.map(async (url) => {
      const response = await fetch(url, { cache: "reload", credentials: "omit", redirect: "error" });
      if (!response.ok || response.type !== "basic") throw new Error("Shell unavailable: " + url);
      await cache.put(url, response);
    }));
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") event.waitUntil(self.skipWaiting());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  // API, SSE, mutations, downloads and foreign origins always use the network.
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    if (!["/", "/index.html", "/login", "/static/index.html", "/static/login.html", "/static/offline.html"].includes(url.pathname)) return;
    event.respondWith((async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(request, { signal: controller.signal });
        if (response.status < 500) return response;
      } catch {
        // Transport failure also uses the public shell below.
      } finally {
        clearTimeout(timeout);
      }
      // No cached dashboard or login: authentication always runs on the server.
      const cache = await caches.open(CACHE_NAME);
      return await cache.match("/static/offline.html") || Response.error();
    })());
    return;
  }
  if (url.search || !PUBLIC_SHELL.has(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    // Live CSS/JS must match current network HTML, even before worker activation.
    try {
      const response = await fetch(request);
      if (response.ok && !response.redirected && response.type === "basic") {
        try { await cache.put(request, response.clone()); } catch { /* Storage pressure must not discard a live response. */ }
      }
      return response;
    } catch {
      return await cache.match(request) || Response.error();
    }
  })());
});
