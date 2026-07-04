/* 夢日記占い Service Worker
 *
 * - アプリシェルはインストール時にプリキャッシュ(オフライン起動)
 * - 辞書データ(data/ja/*)はキャッシュ優先(URLにビルドIDが付くため安全)
 * - その他はネットワーク優先+キャッシュフォールバック
 */

const CACHE_NAME = "dream-diary-v1";
const SHELL = [
  ".",
  "index.html",
  "styles.css",
  "app.js",
  "fx.js",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "data/ja/terms.min.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return; // フォント等の外部リソースはブラウザ任せ

  // 辞書データ: キャッシュ優先(ビルドIDつきURLなので中身が変わればURLも変わる)
  if (url.pathname.includes("/data/ja/")) {
    event.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const hit = await cache.match(request);
        if (hit) return hit;
        const res = await fetch(request);
        if (res.ok) cache.put(request, res.clone());
        return res;
      })
    );
    return;
  }

  // アプリシェル: ネットワーク優先(更新を素早く反映)、失敗時キャッシュ
  event.respondWith(
    fetch(request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return res;
      })
      .catch(async () => {
        const hit = await caches.match(request);
        if (hit) return hit;
        if (request.mode === "navigate") return caches.match("index.html");
        throw new Error("offline");
      })
  );
});
