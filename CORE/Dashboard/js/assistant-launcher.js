// MOD Assistant — Dashboard launcher (mod-assistant-agent §7).
//
// Injects a robot-icon button into the topbar, immediately to the
// LEFT of the notification bell. Click toggles a body-level class
// `assistant-panel-open` that §8's panel will hook into — for §7
// itself the panel doesn't exist yet, the class is just an
// observable open/closed flag.
//
// Lifecycle:
//   1. DOMContentLoaded fires.
//   2. We GET /v1/assistant/config to learn the per-tenant
//      `enabled` flag. If disabled (or the request fails because
//      the user isn't authenticated yet), we bail — nothing renders.
//   3. On success we inject the button and wire:
//      - click handler that toggles the body class
//      - localStorage persistence of the open/closed flag
//      - BroadcastChannel so two tabs of the same browser see
//        each other's open/closed flips
//      - Keyboard shortcuts: Ctrl/Cmd+Shift+A toggles, Escape
//        closes when open
//
// Brand-token styling per
// [[reference_dashboard_brand_tokens]]: the launcher uses
// `text-foreground/70`, `hover:bg-muted` etc. so both
// light and dark modes resolve correctly without per-mode CSS.

(function () {
    "use strict";

    // Canonical constants + nav-action runtime (mod-assistant-agent §6.4)
    // live in window.ModAssistantNav, compiled from
    // CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts by
    // CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js. Loaded by index.html
    // BEFORE this script (js/vendor/assistant-nav.js).
    const _Nav = window.ModAssistantNav;

    // Soft-fail: if the nav script 404'd (e.g. a pre-§6.4 image), window.
    // ModAssistantNav is undefined — log once and bail instead of throwing
    // on the BODY_CLASS read below.
    if (!_Nav) {
        console.warn("[mod-assistant] ModAssistantNav unavailable; launcher disabled");
        return;
    }

    const BODY_CLASS = _Nav.BODY_CLASS;

    let relay = null;

    function _safeReadOpenFlag() {
        return _Nav.safeReadOpenFlag();
    }

    function _safeWriteOpenFlag(open) {
        _Nav.safeWriteOpenFlag(open);
    }

    function _applyOpenState(open, { broadcast }) {
        const root = document.documentElement;
        if (open) {
            document.body.classList.add(BODY_CLASS);
        } else {
            document.body.classList.remove(BODY_CLASS);
        }
        const btn = document.getElementById("assistant-launcher");
        if (btn) {
            btn.setAttribute("aria-pressed", open ? "true" : "false");
            btn.classList.toggle("bg-muted", open);
        }
        _safeWriteOpenFlag(open);
        if (broadcast && relay) {
            relay.postOpenState(open);
        }
    }

    function _toggle() {
        const currentlyOpen = document.body.classList.contains(BODY_CLASS);
        _applyOpenState(!currentlyOpen, { broadcast: true });
    }

    function _close() {
        if (document.body.classList.contains(BODY_CLASS)) {
            _applyOpenState(false, { broadcast: true });
        }
    }

    function _buildButton() {
        const btn = document.createElement("button");
        btn.id = "assistant-launcher";
        btn.type = "button";
        btn.title = "Ask MOD Assistant (Ctrl+Shift+A)";
        btn.setAttribute("aria-label", "Open MOD Assistant");
        btn.setAttribute("aria-pressed", "false");
        // Topbar icon button — sits to the LEFT of the theme toggle (which is
        // left of the notification bell). Matches the other topbar icons
        // (#theme-toggle / #notification-bell): p-2, gray, rounded-lg, hover.
        btn.className =
            "relative p-2 text-gray-600 hover:text-gray-800 hover:bg-gray-100 " +
            "rounded-lg transition-colors focus:outline-none";
        btn.innerHTML =
            // Chat bubble (lucide message-circle) + a small filled sparkle —
            // the cross-industry "AI assistant" signifier. 24px to match the
            // bell / theme-toggle topbar icons.
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
            'viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
            'aria-hidden="true">' +
            '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>' +
            '<path d="M18 1.4l.8 2.6 2.6.8-2.6.8-.8 2.6-.8-2.6-2.6-.8 2.6-.8z" ' +
            'fill="currentColor" stroke="none"/>' +
            '</svg>';
        btn.addEventListener("click", _toggle);
        return btn;
    }

    function _mount(buttonEl) {
        if (document.getElementById("assistant-launcher")) return true;
        // Topbar: sit immediately to the LEFT of the theme toggle (which is
        // itself left of the notification bell). Returning false when the
        // topbar hasn't painted yet makes _init's retry loop wait for it —
        // we intentionally do NOT body-append (no more bottom-right FAB).
        var themeToggle = document.getElementById("theme-toggle");
        if (themeToggle && themeToggle.parentNode) {
            themeToggle.parentNode.insertBefore(buttonEl, themeToggle);
            return true;
        }
        return false;
    }

    function _wireKeyboard() {
        document.addEventListener("keydown", function (e) {
            // Ctrl+Shift+A (or Cmd+Shift+A on macOS) toggles.
            if (e.shiftKey && (e.ctrlKey || e.metaKey) && (e.key === "A" || e.key === "a")) {
                e.preventDefault();
                _toggle();
                return;
            }
            // Escape closes when open. We intentionally check the
            // body class so we don't compete with modals that also
            // listen on Escape — those handle their own scope.
            if (e.key === "Escape" && document.body.classList.contains(BODY_CLASS)) {
                _close();
            }
        });
    }

    function _wireBroadcast() {
        // broadcast: false on the receiving side so we don't echo back
        // into an infinite loop.
        relay = _Nav.createBroadcastRelay(function (open) {
            _applyOpenState(open, { broadcast: false });
        });
    }

    function _bearer() {
        // Dashboard pages persist the JWT in sessionStorage under
        // 'accessToken' (settings.js / billing.js / etc. all use this
        // identical pattern). Use the same pattern here.
        try {
            return sessionStorage.getItem("accessToken") || null;
        } catch (_) { return null; }
    }

    // Fetch result is a 3-way: a config object on success, the
    // string "no-token" if the bearer wasn't ready yet (retryable),
    // or null on anything else (HTTP error, network blip — also
    // retryable because the API might just be mid-restart).
    async function _fetchConfig() {
        const token = _bearer();
        if (!token) return "no-token";
        try {
            const apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
            const resp = await fetch(`${apiBase}/assistant/config`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!resp || !resp.ok) return null;
            return await resp.json();
        } catch (_) {
            return null;
        }
    }

    // Retry budget. Pre-fix the launcher fired exactly once on
    // DOMContentLoaded and gave up forever on the first miss. First-
    // time users (no cached token) lost the race against the
    // Dashboard's `exchangeCodeForToken(code, ...)` call in main.js,
    // and the robot icon never appeared even after auth landed.
    // 12 attempts × 250ms ≈ 3s — covers a slow OIDC exchange + a
    // 1-2s API restart catching the very first config call.
    var MAX_INIT_ATTEMPTS = 12;
    var INIT_RETRY_MS = 250;
    var _initInFlight = false;
    var _initDone = false;

    // 2400UI17 — CE carve. This script ships as a classic <script defer> tag
    // (index.html:71), so it cannot carry a static import; dynamic import() in
    // a classic script resolves against the DOCUMENT url, and the Dashboard is
    // served from the root, so the absolute '/js/api.js' is the same module
    // instance settings.js/analytics.js already pulled into the graph — i.e.
    // the SAME coalesced 30s /platform/features cache. Fail-open: an
    // unresolvable module or an errored payload is NOT CE, so paid tenants keep
    // every call, and its timing, unchanged.
    async function _ceEdition() {
        try {
            const mod = await import('/js/api.js');
            return !(await mod.isPaidEdition());
        } catch (_) { return false; }
    }

    async function _init() {
        if (_initDone || _initInFlight) return;
        _initInFlight = true;
        try {
            for (var attempt = 0; attempt < MAX_INIT_ATTEMPTS; attempt++) {
                var cfg = await _fetchConfig();
                if (cfg && typeof cfg === "object") {
                    if (cfg.enabled === false) {
                        _initDone = true;  // tenant explicitly disabled
                        return;
                    }
                    var btn = _buildButton();
                    if (_mount(btn)) {
                        _wireBroadcast();
                        _wireKeyboard();
                        if (_safeReadOpenFlag()) {
                            _applyOpenState(true, { broadcast: false });
                        }
                        _initDone = true;
                        return;
                    }
                    // Mount-target missing (notification-bell not
                    // rendered yet). Backoff and retry — the
                    // Dashboard's topbar paint can land a frame or
                    // two after DOMContentLoaded.
                }
                // cfg === "no-token" (auth still pending) or
                // cfg === null (transient API error). Backoff.
                await new Promise(function (r) { setTimeout(r, INIT_RETRY_MS); });
            }
            // Burned the budget. Stay registered for the listeners
            // below — a delayed token arrival can still wake us up.
            console.warn(
                "[assistant-launcher] init failed after " + MAX_INIT_ATTEMPTS +
                " attempts; waiting for token-arrival event.",
            );
        } finally {
            _initInFlight = false;
        }
    }

    // Fast-path wakeups for the race we just papered over with
    // retries. A successful refresh (or a sibling tab logging in)
    // fires these; we re-run init opportunistically so the launcher
    // appears the instant auth becomes available, no 250ms delay.
    function _wireAuthArrivalWakeups() {
        // Same-tab refresh: api-client.ts dispatches this after a
        // silent token refresh.
        window.addEventListener("mod:auth:token-refreshed", function () {
            if (!_initDone) _init();
        });
        // Cross-tab login fallback. Dashboard tokens live in sessionStorage
        // (GSEC R-077), which is per-tab and NEVER fires a 'storage' event in
        // a sibling tab — so this listener cannot observe a real cross-tab
        // token write. It is only a best-effort wake for the case where a flow
        // mirrors a token into localStorage (which DOES fire cross-tab
        // 'storage'); the reliable same-tab signal is the
        // mod:auth:token-refreshed event above. Watch both token keys rather
        // than just accessToken so no mirrored-key variant is missed.
        window.addEventListener("storage", function (e) {
            if (!_initDone && e && (e.key === "accessToken" || e.key === "workflow_access_token")) {
                _init();
            }
        });
    }

    _wireAuthArrivalWakeups();

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _init);
    } else {
        _init();
    }
})();
