// GENERATED FILE — do not edit by hand.
// Source: CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts
// Regenerate: node CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js
// (mod-assistant-agent §6.4 — shared nav-action runtime)
(function (global) {
"use strict";
var exports = {};
"use strict";
/**
 * MOD Assistant — shared nav-action runtime (mod-assistant-agent §6.4).
 *
 * Canonical implementation of the client-side action runtime used by
 * BOTH WorkflowBuilder (`components/assistant-panel.tsx`,
 * `assistant-launcher.tsx`) and Dashboard (`js/assistant-panel.js`,
 * `js/assistant-launcher.js`). Historically each app carried its own
 * ~identical copy of this logic; this module is the single source of
 * truth going forward.
 *
 * The server (`services/assistant_actions.py`) already validates every
 * emitted action against the site map + per-tenant allowlist BEFORE it
 * streams down the SSE connection. This module re-validates on the
 * client as belt-and-suspenders (§6 design.md "Action vocabulary is
 * fixed ... validated server-side") — it rejects anything shaped wrong
 * or of an unknown action type rather than trusting the wire blindly.
 *
 * App-specific behavior that intentionally stays OUT of this module
 * (kept in each app's own wrapper instead):
 *   - Cross-app URL resolution: WorkflowBuilder reads
 *     `process.env.NEXT_PUBLIC_DASHBOARD_URL` (Next.js build-time env);
 *     Dashboard reads `window.APP_CONFIG` (runtime-injected by
 *     inject-config.sh) AND appends auth token query params when the
 *     target is WorkflowBuilder (sessionStorage doesn't cross
 *     subdomains). Neither mechanism is available to the other app, so
 *     `executeAction` takes a `resolveNavigateUrl` callback rather than
 *     hard-coding either strategy.
 *   - Dialog handling: default dispatches a `mod:assistant-open-dialog`
 *     CustomEvent (identical in both apps today); overridable via
 *     `onOpenDialog` for future divergence.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.DEFAULT_HIGHLIGHT_MS = exports.HIGHLIGHT_CLASS = exports.BODY_CLASS = exports.PENDING_COOKIE = exports.OPEN_COOKIE = exports.STORAGE_OPEN_KEY = exports.BROADCAST_NAME = void 0;
exports.isValidRoute = isValidRoute;
exports.isSafeId = isSafeId;
exports.isSafeAnchorSelector = isSafeAnchorSelector;
exports.isKnownActionType = isKnownActionType;
exports.validateAction = validateAction;
exports.parentCookieDomain = parentCookieDomain;
exports.safeReadOpenFlag = safeReadOpenFlag;
exports.safeWriteOpenFlag = safeWriteOpenFlag;
exports.writePendingActions = writePendingActions;
exports.readAndClearPendingActions = readAndClearPendingActions;
exports.createBroadcastRelay = createBroadcastRelay;
exports.highlightElement = highlightElement;
exports.navigateLabel = navigateLabel;
exports.openNewTabLabel = openNewTabLabel;
exports.chainLabel = chainLabel;
exports.actionLabel = actionLabel;
exports.executeAction = executeAction;
// ---------------------------------------------------------------------------
// Constants (identical literal values in both apps — DO NOT change without
// updating the server-side site map / allowlist in lockstep).
// ---------------------------------------------------------------------------
exports.BROADCAST_NAME = "mod_assistant";
exports.STORAGE_OPEN_KEY = "mod.assistantPanelOpen";
exports.OPEN_COOKIE = "mod_assistant_open";
exports.PENDING_COOKIE = "mod.assistant.pending";
exports.BODY_CLASS = "assistant-panel-open";
exports.HIGHLIGHT_CLASS = "mod-assistant-highlight";
exports.DEFAULT_HIGHLIGHT_MS = 1600;
// Actions the client runtime knows how to execute (support-contacted is a
// receipt payload, not something `executeAction` runs).
const KNOWN_ACTION_TYPES = new Set([
    "navigate",
    "open-new-tab",
    "open-dialog",
    "open-tab",
    "scroll-to",
    "highlight",
    "support-contacted",
    "chain",
]);
// ---------------------------------------------------------------------------
// Route / target allowlisting.
//
// The authoritative allowlist is the server's site map (URLs + dialog ids
// it will actually emit). The client can't fetch that without an extra
// round trip (design.md explicitly rejects that), so it enforces a
// *shape* allowlist instead: same-origin relative paths only for
// navigation, safe identifier-only strings for dialog/tab ids, and a
// fixed `data-assistant-anchor` selector shape for highlight/scroll-to.
// This blocks the open-redirect / XSS classes of bad input even without
// a live copy of the site map.
// ---------------------------------------------------------------------------
const SAFE_ID_RE = /^[a-zA-Z0-9_-]+$/;
const SAFE_ANCHOR_SELECTOR_RE = /^\[data-assistant-anchor="[a-zA-Z0-9_-]+"\]$/;
function isValidRoute(url) {
    if (typeof url !== "string" || url.length === 0)
        return false;
    // Must be a same-origin relative path: starts with a single "/",
    // never "//" (protocol-relative) and never contains a "://" scheme.
    if (!url.startsWith("/"))
        return false;
    if (url.startsWith("//"))
        return false;
    if (/^[a-z][a-z0-9+.-]*:/i.test(url))
        return false;
    return true;
}
function isSafeId(id) {
    return typeof id === "string" && id.length > 0 && SAFE_ID_RE.test(id);
}
function isSafeAnchorSelector(selector) {
    return typeof selector === "string" && SAFE_ANCHOR_SELECTOR_RE.test(selector);
}
function isKnownActionType(type) {
    return typeof type === "string" && KNOWN_ACTION_TYPES.has(type);
}
/**
 * Validates a single action against its schema + the shape allowlist
 * above. Returns the action unchanged (narrowed) when valid, or `null`
 * when it should be dropped. `chain` recurses and drops only the
 * invalid children rather than the whole chain, mirroring the server's
 * `validate_action` behavior (§6.1/§6.2 tasks.md).
 */
function validateAction(raw) {
    var _a;
    if (!raw || typeof raw !== "object")
        return null;
    const action = raw;
    if (!isKnownActionType(action.action))
        return null;
    switch (action.action) {
        case "navigate":
        case "open-new-tab":
            return isValidRoute(action.url) ? action : null;
        case "open-dialog":
        case "open-tab":
            return isSafeId((_a = action.id) !== null && _a !== void 0 ? _a : action.label) ? action : null;
        case "scroll-to":
        case "highlight":
            return isSafeAnchorSelector(action.selector) ? action : null;
        case "support-contacted":
            return action;
        case "chain": {
            if (!Array.isArray(action.actions))
                return null;
            const validated = action.actions
                .map((child) => validateAction(child))
                .filter((child) => child !== null);
            if (validated.length === 0)
                return null;
            return Object.assign(Object.assign({}, action), { actions: validated });
        }
        default:
            return null;
    }
}
// ---------------------------------------------------------------------------
// Cookie helpers (cross-subdomain handoff for chain actions + open flag).
// ---------------------------------------------------------------------------
function parentCookieDomain(hostname) {
    try {
        const host = hostname !== null && hostname !== void 0 ? hostname : (typeof location !== "undefined" ? location.hostname : "");
        if (!host)
            return "";
        if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host === "localhost" || host.indexOf(".") === -1) {
            return "";
        }
        return "." + host.split(".").slice(-2).join(".");
    }
    catch (_a) {
        return "";
    }
}
function safeReadOpenFlag() {
    try {
        const m = document.cookie.match(/(?:^|;\s*)mod_assistant_open=([^;]+)/);
        if (m)
            return m[1] === "1";
    }
    catch (_a) {
        /* fall through */
    }
    try {
        return localStorage.getItem(exports.STORAGE_OPEN_KEY) === "1";
    }
    catch (_b) {
        return false;
    }
}
function safeWriteOpenFlag(open) {
    try {
        localStorage.setItem(exports.STORAGE_OPEN_KEY, open ? "1" : "0");
    }
    catch (_a) {
        /* quota / privacy mode — swallow */
    }
    try {
        const domain = parentCookieDomain();
        const domainPart = domain ? `; Domain=${domain}` : "";
        const securePart = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
        document.cookie =
            `${exports.OPEN_COOKIE}=${open ? "1" : "0"}; Path=/; Max-Age=31536000${domainPart}; SameSite=Lax${securePart}`;
    }
    catch (_b) {
        /* swallow */
    }
}
function writePendingActions(actions) {
    try {
        const json = JSON.stringify(actions);
        const b64 = btoa(unescape(encodeURIComponent(json)));
        const d = parentCookieDomain();
        const domain = d ? `; Domain=${d}` : "";
        const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
        document.cookie = `${exports.PENDING_COOKIE}=${b64}; Path=/; Max-Age=60${domain}; SameSite=Lax${secure}`;
    }
    catch (_a) {
        /* noop */
    }
}
function readAndClearPendingActions() {
    try {
        const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + exports.PENDING_COOKIE + "=([^;]+)"));
        if (!m)
            return [];
        const b64 = m[1];
        const d = parentCookieDomain();
        const domain = d ? `; Domain=${d}` : "";
        document.cookie = `${exports.PENDING_COOKIE}=; Path=/; Max-Age=0${domain}; SameSite=Lax`;
        const json = decodeURIComponent(escape(atob(b64)));
        const parsed = JSON.parse(json);
        return Array.isArray(parsed) ? parsed : [];
    }
    catch (_a) {
        return [];
    }
}
/** No-op relay used when BroadcastChannel isn't available (SSR, old browsers). */
const NOOP_RELAY = { postOpenState() { }, close() { } };
function createBroadcastRelay(onOpenState) {
    if (typeof BroadcastChannel === "undefined")
        return NOOP_RELAY;
    let channel;
    try {
        channel = new BroadcastChannel(exports.BROADCAST_NAME);
    }
    catch (_a) {
        return NOOP_RELAY;
    }
    if (onOpenState) {
        channel.onmessage = (ev) => {
            const data = ev && ev.data;
            if (data && data.type === "open-state" && typeof data.open === "boolean") {
                onOpenState(data.open);
            }
        };
    }
    return {
        postOpenState(open) {
            try {
                channel && channel.postMessage({ type: "open-state", open });
            }
            catch (_a) {
                /* peer tab gone — non-fatal */
            }
        },
        close() {
            try {
                channel && channel.close();
            }
            catch (_a) {
                /* noop */
            }
            channel = null;
        },
    };
}
// ---------------------------------------------------------------------------
// Highlight.
// ---------------------------------------------------------------------------
/**
 * Highlights the element matching `selector` for `durationMs`. A no-op
 * (never throws) when the selector is unsafe or matches no element —
 * a hallucinated/stale anchor must degrade silently, not break the UI.
 */
function highlightElement(selector, durationMs = exports.DEFAULT_HIGHLIGHT_MS) {
    if (!isSafeAnchorSelector(selector))
        return;
    if (typeof document === "undefined")
        return;
    let el = null;
    try {
        el = document.querySelector(selector);
    }
    catch (_a) {
        return;
    }
    if (!el)
        return;
    el.classList.add(exports.HIGHLIGHT_CLASS);
    setTimeout(() => {
        try {
            el && el.classList.remove(exports.HIGHLIGHT_CLASS);
        }
        catch (_a) {
            /* noop */
        }
    }, Math.max(0, durationMs || exports.DEFAULT_HIGHLIGHT_MS));
}
// ---------------------------------------------------------------------------
// Action labels (for the panel's inline action buttons).
// ---------------------------------------------------------------------------
function navigateLabel(a) {
    if (a.label)
        return `Take me to ${a.label}`;
    const dest = a.app === "workflow_builder" ? "Workflow Builder" : a.app === "dashboard" ? "Dashboard" : a.url || "";
    return `Take me to ${dest}`;
}
function openNewTabLabel(a) {
    if (a.label)
        return `Open ${a.label} in a new tab`;
    return `Open ${a.url} in a new tab`;
}
function chainLabel(a) {
    const parts = [];
    for (const child of a.actions || []) {
        if (child.action === "navigate")
            parts.push(navigateLabel(child));
        else if (child.action === "open-dialog")
            parts.push("open " + (child.id || "").replace(/-/g, " "));
        else if (child.action === "open-new-tab")
            parts.push("open " + (child.label || child.url || "") + " in a new tab");
    }
    return parts.length === 0 ? "Run these steps" : parts.join(", then ");
}
function actionLabel(a) {
    if (a.action === "navigate")
        return navigateLabel(a);
    if (a.action === "open-new-tab")
        return openNewTabLabel(a);
    if (a.action === "open-dialog")
        return `Open ${(a.id || "").replace(/-/g, " ")}`;
    if (a.action === "chain")
        return chainLabel(a);
    return "Open";
}
function defaultResolve(action) {
    var _a;
    return (_a = action.url) !== null && _a !== void 0 ? _a : null;
}
function defaultOpenDialog(id) {
    if (typeof window === "undefined")
        return;
    window.dispatchEvent(new CustomEvent("mod:assistant-open-dialog", { detail: { id } }));
}
/**
 * Executes a single (already-validated-by-the-server) action. Re-runs
 * `validateAction` defensively first — an invalid/unknown action is
 * silently dropped (matching design.md §7 "rejects unknown actions
 * silently with a logged warning").
 */
function executeAction(raw, opts = {}) {
    var _a, _b;
    const action = validateAction(raw);
    if (!action) {
        try {
            console.warn("[assistant-nav] dropped invalid/unknown action", raw);
        }
        catch (_c) {
            /* noop */
        }
        return;
    }
    const resolveNavigateUrl = (_a = opts.resolveNavigateUrl) !== null && _a !== void 0 ? _a : defaultResolve;
    const onOpenDialog = (_b = opts.onOpenDialog) !== null && _b !== void 0 ? _b : defaultOpenDialog;
    if (action.action === "navigate") {
        const target = resolveNavigateUrl(action);
        if (target && typeof window !== "undefined")
            window.location.href = target;
    }
    else if (action.action === "open-new-tab") {
        const target = resolveNavigateUrl(action);
        if (target && typeof window !== "undefined")
            window.open(target, "_blank", "noopener");
    }
    else if (action.action === "open-dialog") {
        if (action.id)
            onOpenDialog(action.id);
    }
    else if (action.action === "open-tab") {
        if (opts.onOpenTab && action.id)
            opts.onOpenTab(action.id);
    }
    else if (action.action === "scroll-to" || action.action === "highlight") {
        if (action.selector) {
            if (opts.onHighlight)
                opts.onHighlight(action.selector, action.durationMs);
            else
                highlightElement(action.selector, action.durationMs);
        }
    }
    else if (action.action === "chain" && Array.isArray(action.actions)) {
        for (let i = 0; i < action.actions.length; i++) {
            const child = action.actions[i];
            if (child && child.action === "navigate" && i + 1 < action.actions.length) {
                writePendingActions(action.actions.slice(i + 1));
            }
            executeAction(child, opts);
            if (child && child.action === "navigate")
                return; // page is unloading
        }
    }
}

global.ModAssistantNav = exports;
})(typeof window !== "undefined" ? window : this);
