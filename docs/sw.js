// Minimal service worker. The Streamlit app needs a live connection to its server, so nothing is
// cached; the fetch handler exists so browsers treat the page as installable, and it shows a
// friendly message when the phone is offline.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body dir="rtl" style="font-family:sans-serif;text-align:center;padding:3rem">' +
    '<h2>אין חיבור לאינטרנט</h2><p>מחשבון הסנטפים דורש חיבור. נסו שוב כשיש קליטה.</p></body>',
    { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
