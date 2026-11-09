// The service worker exists for one thing: receiving what other apps share to
// Campaign (Android's share sheet, e.g. a transcript from Pixel Recorder).
// Android POSTs the share to /share; this holds it on the device and opens
// the app on #/share, where the DM picks the campaign and session. Holding it
// here, not posting it on, means nothing is lost to a sign-in on the way.
// Every other request goes to the network as if this weren't here.

const SHARE_CACHE = "campaign-share";
const PENDING = "/share/pending";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "POST" && url.origin === location.origin && url.pathname === "/share") {
    e.respondWith(receive(e.request));
  }
});

async function receive(request) {
  const form = await request.formData();
  // Shared with the audio too? The transcript is the text file.
  const files = form.getAll("file").filter((f) => typeof f !== "string" && f.size > 0);
  const file = files.find((f) => f.type.startsWith("text/") || /\.(txt|md)$/i.test(f.name)) || files[0];
  const isFile = Boolean(file);
  const shared = {
    name: isFile ? file.name : "",
    title: form.get("title") || "",
    text: isFile ? await file.text() : (form.get("text") || ""),
    at: new Date().toISOString(),
  };
  const cache = await caches.open(SHARE_CACHE);
  await cache.put(PENDING, new Response(JSON.stringify(shared), { headers: { "Content-Type": "application/json" } }));
  return Response.redirect("/#/share", 303);
}
