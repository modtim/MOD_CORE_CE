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
 *   - Cross-app URL resolution: WorkflowBuilder reads the dashboard URL
 *     from runtime config (`dashboardUrl()` in `lib/runtime-config`, served
 *     via /runtime-config.js in prod, env in dev);
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

// ---------------------------------------------------------------------------
// Constants (identical literal values in both apps — DO NOT change without
// updating the server-side site map / allowlist in lockstep).
// ---------------------------------------------------------------------------

export const BROADCAST_NAME = "mod_assistant";
export const STORAGE_OPEN_KEY = "mod.assistantPanelOpen";
export const OPEN_COOKIE = "mod_assistant_open";
export const PENDING_COOKIE = "mod.assistant.pending";
export const BODY_CLASS = "assistant-panel-open";
export const HIGHLIGHT_CLASS = "mod-assistant-highlight";
export const DEFAULT_HIGHLIGHT_MS = 1600;

export type AppId = "dashboard" | "workflow_builder";

export type ActionType =
    | "navigate"
    | "open-new-tab"
    | "open-dialog"
    | "open-tab"
    | "scroll-to"
    | "highlight"
    | "chain"
    | "support-contacted";

// Actions the client runtime knows how to execute (support-contacted is a
// receipt payload, not something `executeAction` runs).
const KNOWN_ACTION_TYPES: ReadonlySet<string> = new Set<ActionType>([
    "navigate",
    "open-new-tab",
    "open-dialog",
    "open-tab",
    "scroll-to",
    "highlight",
    "support-contacted",
    "chain",
]);

export interface ActionEmit {
    action: ActionType;
    url?: string;
    id?: string;
    label?: string;
    app?: AppId;
    selector?: string;
    durationMs?: number;
    actions?: ActionEmit[];
    // support-contacted: confirmation-receipt payload (not a clickable action).
    recipient?: string;
    severity?: string;
    tenant_admin_count?: number;
    subject?: string;
}

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

export function isValidRoute(url: unknown): url is string {
    if (typeof url !== "string" || url.length === 0) return false;
    // Must be a same-origin relative path: starts with a single "/",
    // never "//" (protocol-relative) and never contains a "://" scheme.
    if (!url.startsWith("/")) return false;
    if (url.startsWith("//")) return false;
    if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return false;
    return true;
}

export function isSafeId(id: unknown): id is string {
    return typeof id === "string" && id.length > 0 && SAFE_ID_RE.test(id);
}

export function isSafeAnchorSelector(selector: unknown): selector is string {
    return typeof selector === "string" && SAFE_ANCHOR_SELECTOR_RE.test(selector);
}

export function isKnownActionType(type: unknown): type is ActionType {
    return typeof type === "string" && KNOWN_ACTION_TYPES.has(type);
}

/**
 * Validates a single action against its schema + the shape allowlist
 * above. Returns the action unchanged (narrowed) when valid, or `null`
 * when it should be dropped. `chain` recurses and drops only the
 * invalid children rather than the whole chain, mirroring the server's
 * `validate_action` behavior (§6.1/§6.2 tasks.md).
 */
export function validateAction(raw: unknown): ActionEmit | null {
    if (!raw || typeof raw !== "object") return null;
    const action = raw as ActionEmit;
    if (!isKnownActionType(action.action)) return null;

    switch (action.action) {
        case "navigate":
        case "open-new-tab":
            return isValidRoute(action.url) ? action : null;
        case "open-dialog":
        case "open-tab":
            return isSafeId(action.id ?? (action as any).label) ? action : null;
        case "scroll-to":
        case "highlight":
            return isSafeAnchorSelector(action.selector) ? action : null;
        case "support-contacted":
            return action;
        case "chain": {
            if (!Array.isArray(action.actions)) return null;
            const validated = action.actions
                .map((child) => validateAction(child))
                .filter((child): child is ActionEmit => child !== null);
            if (validated.length === 0) return null;
            return { ...action, actions: validated };
        }
        default:
            return null;
    }
}

// ---------------------------------------------------------------------------
// Cookie helpers (cross-subdomain handoff for chain actions + open flag).
// ---------------------------------------------------------------------------

export function parentCookieDomain(hostname?: string): string {
    try {
        const host = hostname ?? (typeof location !== "undefined" ? location.hostname : "");
        if (!host) return "";
        if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host === "localhost" || host.indexOf(".") === -1) {
            return "";
        }
        return "." + host.split(".").slice(-2).join(".");
    } catch {
        return "";
    }
}

export function safeReadOpenFlag(): boolean {
    try {
        const m = document.cookie.match(/(?:^|;\s*)mod_assistant_open=([^;]+)/);
        if (m) return m[1] === "1";
    } catch {
        /* fall through */
    }
    try {
        return localStorage.getItem(STORAGE_OPEN_KEY) === "1";
    } catch {
        return false;
    }
}

export function safeWriteOpenFlag(open: boolean): void {
    try {
        localStorage.setItem(STORAGE_OPEN_KEY, open ? "1" : "0");
    } catch {
        /* quota / privacy mode — swallow */
    }
    try {
        const domain = parentCookieDomain();
        const domainPart = domain ? `; Domain=${domain}` : "";
        const securePart = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
        document.cookie =
            `${OPEN_COOKIE}=${open ? "1" : "0"}; Path=/; Max-Age=31536000${domainPart}; SameSite=Lax${securePart}`;
    } catch {
        /* swallow */
    }
}

export function writePendingActions(actions: ActionEmit[]): void {
    try {
        const json = JSON.stringify(actions);
        const b64 = btoa(unescape(encodeURIComponent(json)));
        const d = parentCookieDomain();
        const domain = d ? `; Domain=${d}` : "";
        const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
        document.cookie = `${PENDING_COOKIE}=${b64}; Path=/; Max-Age=60${domain}; SameSite=Lax${secure}`;
    } catch {
        /* noop */
    }
}

export function readAndClearPendingActions(): ActionEmit[] {
    try {
        const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + PENDING_COOKIE + "=([^;]+)"));
        if (!m) return [];
        const b64 = m[1];
        const d = parentCookieDomain();
        const domain = d ? `; Domain=${d}` : "";
        document.cookie = `${PENDING_COOKIE}=; Path=/; Max-Age=0${domain}; SameSite=Lax`;
        const json = decodeURIComponent(escape(atob(b64)));
        const parsed = JSON.parse(json);
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

// ---------------------------------------------------------------------------
// Cross-tab BroadcastChannel relay (panel open/close sync).
// ---------------------------------------------------------------------------

export interface BroadcastRelay {
    postOpenState(open: boolean): void;
    close(): void;
}

/** No-op relay used when BroadcastChannel isn't available (SSR, old browsers). */
const NOOP_RELAY: BroadcastRelay = { postOpenState() {}, close() {} };

export function createBroadcastRelay(onOpenState?: (open: boolean) => void): BroadcastRelay {
    if (typeof BroadcastChannel === "undefined") return NOOP_RELAY;
    let channel: BroadcastChannel | null;
    try {
        channel = new BroadcastChannel(BROADCAST_NAME);
    } catch {
        return NOOP_RELAY;
    }
    if (onOpenState) {
        channel.onmessage = (ev: MessageEvent) => {
            const data = ev && ev.data;
            if (data && data.type === "open-state" && typeof data.open === "boolean") {
                onOpenState(data.open);
            }
        };
    }
    return {
        postOpenState(open: boolean) {
            try {
                channel && channel.postMessage({ type: "open-state", open });
            } catch {
                /* peer tab gone — non-fatal */
            }
        },
        close() {
            try {
                channel && channel.close();
            } catch {
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
export function highlightElement(selector: unknown, durationMs: number = DEFAULT_HIGHLIGHT_MS): void {
    if (!isSafeAnchorSelector(selector)) return;
    if (typeof document === "undefined") return;
    let el: Element | null = null;
    try {
        el = document.querySelector(selector);
    } catch {
        return;
    }
    if (!el) return;
    el.classList.add(HIGHLIGHT_CLASS);
    setTimeout(() => {
        try {
            el && el.classList.remove(HIGHLIGHT_CLASS);
        } catch {
            /* noop */
        }
    }, Math.max(0, durationMs || DEFAULT_HIGHLIGHT_MS));
}

// ---------------------------------------------------------------------------
// Action labels (for the panel's inline action buttons).
// ---------------------------------------------------------------------------

export function navigateLabel(a: ActionEmit): string {
    if (a.label) return `Take me to ${a.label}`;
    const dest = a.app === "workflow_builder" ? "Workflow Builder" : a.app === "dashboard" ? "Dashboard" : a.url || "";
    return `Take me to ${dest}`;
}

export function openNewTabLabel(a: ActionEmit): string {
    if (a.label) return `Open ${a.label} in a new tab`;
    return `Open ${a.url} in a new tab`;
}

export function chainLabel(a: ActionEmit): string {
    const parts: string[] = [];
    for (const child of a.actions || []) {
        if (child.action === "navigate") parts.push(navigateLabel(child));
        else if (child.action === "open-dialog") parts.push("open " + (child.id || "").replace(/-/g, " "));
        else if (child.action === "open-new-tab") parts.push("open " + (child.label || child.url || "") + " in a new tab");
    }
    return parts.length === 0 ? "Run these steps" : parts.join(", then ");
}

export function actionLabel(a: ActionEmit): string {
    if (a.action === "navigate") return navigateLabel(a);
    if (a.action === "open-new-tab") return openNewTabLabel(a);
    if (a.action === "open-dialog") return `Open ${(a.id || "").replace(/-/g, " ")}`;
    if (a.action === "chain") return chainLabel(a);
    return "Open";
}

// ---------------------------------------------------------------------------
// Execution.
// ---------------------------------------------------------------------------

export interface ExecuteActionOptions {
    /** Resolves a navigate/open-new-tab action's `url` to the final
     * destination (handles cross-app + auth-handoff logic that's
     * app-specific). Return `null` to abort the navigation silently
     * (the caller already logged why). */
    resolveNavigateUrl?: (action: ActionEmit) => string | null;
    onOpenDialog?: (id: string) => void;
    onOpenTab?: (label: string) => void;
    onHighlight?: (selector: string, durationMs?: number) => void;
}

function defaultResolve(action: ActionEmit): string | null {
    return action.url ?? null;
}

function defaultOpenDialog(id: string): void {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new CustomEvent("mod:assistant-open-dialog", { detail: { id } }));
}

/**
 * Executes a single (already-validated-by-the-server) action. Re-runs
 * `validateAction` defensively first — an invalid/unknown action is
 * silently dropped (matching design.md §7 "rejects unknown actions
 * silently with a logged warning").
 */
export function executeAction(raw: ActionEmit, opts: ExecuteActionOptions = {}): void {
    const action = validateAction(raw);
    if (!action) {
        try {
            console.warn("[assistant-nav] dropped invalid/unknown action", raw);
        } catch {
            /* noop */
        }
        return;
    }
    const resolveNavigateUrl = opts.resolveNavigateUrl ?? defaultResolve;
    const onOpenDialog = opts.onOpenDialog ?? defaultOpenDialog;

    if (action.action === "navigate") {
        const target = resolveNavigateUrl(action);
        if (target && typeof window !== "undefined") window.location.href = target;
    } else if (action.action === "open-new-tab") {
        const target = resolveNavigateUrl(action);
        if (target && typeof window !== "undefined") window.open(target, "_blank", "noopener");
    } else if (action.action === "open-dialog") {
        if (action.id) onOpenDialog(action.id);
    } else if (action.action === "open-tab") {
        if (opts.onOpenTab && action.id) opts.onOpenTab(action.id);
    } else if (action.action === "scroll-to" || action.action === "highlight") {
        if (action.selector) {
            if (opts.onHighlight) opts.onHighlight(action.selector, action.durationMs);
            else highlightElement(action.selector, action.durationMs);
        }
    } else if (action.action === "chain" && Array.isArray(action.actions)) {
        for (let i = 0; i < action.actions.length; i++) {
            const child = action.actions[i];
            if (child && child.action === "navigate" && i + 1 < action.actions.length) {
                writePendingActions(action.actions.slice(i + 1));
            }
            executeAction(child, opts);
            if (child && child.action === "navigate") return; // page is unloading
        }
    }
}
