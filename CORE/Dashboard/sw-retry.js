// Retries failed same-origin static-asset GETs to defend against transient
// ERR_NETWORK_CHANGED bursts (and similar fetch-level TypeErrors) during the
// post-login module-graph load.
//
// Intentionally narrow:
//   - GET only (POSTs may not be idempotent)
//   - Same-origin only (don't touch CDN/API hosts)
//   - Static asset extensions only — never API paths
//   - No caching: retry only, to avoid serving stale code after a deploy.

const ASSET_RE = /\.(js|mjs|css|html|json|svg|png|jpg|jpeg|gif|ico|woff2?|ttf|eot|wasm)$/i;
const MAX_RETRIES = 3;
const BACKOFF_BASE_MS = 100;

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    let url;
    try {
        url = new URL(req.url);
    } catch {
        return;
    }

    if (url.origin !== self.location.origin) return;
    if (!ASSET_RE.test(url.pathname)) return;

    event.respondWith(fetchWithRetry(req, MAX_RETRIES));
});

async function fetchWithRetry(request, maxRetries) {
    let lastErr;
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            return await fetch(request);
        } catch (err) {
            lastErr = err;
            if (attempt < maxRetries) {
                const delay = BACKOFF_BASE_MS * Math.pow(3, attempt); // 100, 300, 900 ms
                await new Promise((r) => setTimeout(r, delay));
            }
        }
    }
    throw lastErr;
}
