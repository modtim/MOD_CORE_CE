// Network-resilience bootstrap. Runs before every other script.
//
// Two-layer defence against transient ERR_NETWORK_CHANGED bursts
// (Chrome's NetworkChangeNotifier aborts every in-flight fetch when the
// OS reports an interface flap — VPN cycle, WiFi reassociation, adapter
// sleep/wake — which torpedoes the post-login JS module cascade).
//
//   1. Service worker /sw-retry.js retries failed same-origin static
//      GETs transparently. Active from the 2nd visit onward.
//   2. First-visit fallback: if the module graph fails before the SW is
//      controlling the page, auto-reload once. The reload picks up the
//      now-active SW and proceeds normally.
// R-167 — moved verbatim from the inline <script> in index.html so the
// dashboard CSP can drop 'unsafe-inline' from script-src.
(function () {
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw-retry.js').catch(function () { /* non-fatal */ });
    }

    var RELOAD_KEY = 'mod_module_reload_count';
    // BUG.B47 — at most ONE recovery reload. Two (the old value) made a page
    // that errors on every load boot three times before it renders, which is
    // what the owner saw as the dashboard "restarting multiple times" on F5.
    var MAX_RELOADS = 1;
    var reloadInFlight = false;

    function readCount() {
        try { return parseInt(sessionStorage.getItem(RELOAD_KEY) || '0', 10) || 0; } catch (e) { return 0; }
    }
    function writeCount(n) {
        try { sessionStorage.setItem(RELOAD_KEY, String(n)); } catch (e) { /* noop */ }
    }
    function clearCount() {
        try { sessionStorage.removeItem(RELOAD_KEY); } catch (e) { /* noop */ }
    }
    function triggerReload(reason) {
        if (reloadInFlight) return;
        // BUG.B47 — the app already booted and rendered this document; a late
        // script error must not restart the whole page on top of it (that is
        // the visible "app restarts several times" on a browser refresh).
        if (window.__modBooted) return;
        if (readCount() >= MAX_RELOADS) return; // genuine failure, stop looping
        reloadInFlight = true;
        writeCount(readCount() + 1);
        console.warn('[mod] module load failure (' + reason + ') — reloading once');
        location.reload();
    }

    // Page initialized cleanly → clear the counter after a grace
    // period (long enough for the lazy-import cascade to settle).
    window.addEventListener('load', function () {
        setTimeout(clearCount, 5000);
    });

    // Static <script> tag failure — includes type="module" scripts
    // whose static-import graph failed. Capture phase because the
    // error event from <script> does not bubble.
    window.addEventListener('error', function (e) {
        if (e && e.target && e.target.tagName === 'SCRIPT' && e.target.src) {
            triggerReload('script-load');
        }
    }, true);

    // Dynamic import() rejection. Match the three browser variants;
    // do NOT match the unqualified "Failed to fetch" because that
    // also fires for API call failures, which we must not reload on.
    window.addEventListener('unhandledrejection', function (e) {
        var msg = e && e.reason && (e.reason.message || String(e.reason));
        if (msg && /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/.test(msg)) {
            triggerReload('dynamic-import');
        }
    });
})();
