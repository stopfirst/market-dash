/* 섹터·마켓브레스 서비스 워커
   화면과 데이터 모두 '네트워크 먼저, 실패하면 캐시'.
   → 새 버전을 올리면 바로 반영되고, 비행기·지하철에서도 마지막 화면이 열린다. */
const V = "breadth-v1";
const SHELL = [
  "./sector-dashboard.html",
  "./manifest.json",
  "./icon.svg",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png"
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(V)
      .then(c => Promise.allSettled(SHELL.map(u => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 외부 요청은 그대로 통과

  e.respondWith(
    fetch(req)
      .then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(V).then(c => c.put(req, copy));
        }
        return res;
      })
      .catch(async () => {
        const hit = await caches.match(req, { ignoreSearch: true });
        if (hit) return hit;
        if (req.mode === "navigate") {
          const shell = await caches.match("./sector-dashboard.html");
          if (shell) return shell;
        }
        return new Response("오프라인", { status: 503, statusText: "offline" });
      })
  );
});
