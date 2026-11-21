"use client";

/**
 * MOD Assistant — WorkflowBuilder chat panel (mod-assistant-agent §8).
 *
 * Mirrors CORE/Dashboard/js/assistant-panel.js feature-for-feature:
 *   - Right-anchored slide-in, drag-resizable (320..720px), persisted
 *   - Per-tab session id in sessionStorage
 *   - Streaming chat against POST /v1/assistant/chat (SSE)
 *   - User/assistant message bubbles, in-flight thinking indicator,
 *     streamed-token append, jump-to-latest pill
 *   - Inline action buttons (navigate / open-new-tab / open-dialog /
 *     chain). Action runtime never auto-executes — the user clicks.
 *   - Citations link block on assistant messages with `citation`
 *     events
 *   - Empty state with suggested prompts on first open
 *   - Error state with Retry that resubmits the last user message
 *   - Confirmation dialog: when the turn needs a one-time
 *     confirm token (risky tool, or any risky tool in a turn tainted by
 *     untrusted content), the panel shows an accessible modal listing the
 *     pending tools and, on Confirm, re-POSTs the SAME request with
 *     `confirm_tokens`. Cancel sends nothing. Tokens are single-use: they
 *     live in a per-send closure, cleared the moment they are sent.
 *
 * Lifecycle: gated on AssistantTenantConfig.enabled via /assistant/
 * config. The panel hooks into `<body class="assistant-panel-open">`
 * which the AssistantLauncher button toggles — so the launcher and
 * panel are decoupled and either can ship without the other.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bot, X } from "lucide-react";
import { dashboardUrlOrNull } from "@/lib/runtime-config";
import { wbNavLabel } from "@/lib/product-names";
import { useEntitlements } from "@/hooks/use-entitlements";

import {
    ActionEmit as SharedActionEmit,
    BODY_CLASS,
    createBroadcastRelay,
    executeAction as sharedExecuteAction,
    readAndClearPendingActions as sharedReadAndClearPendingActions,
    writePendingActions as sharedWritePendingActions,
    safeWriteOpenFlag,
    actionLabel as sharedActionLabel,
} from "@/lib/shared/assistant-actions";

const STORAGE_WIDTH_KEY = "mod.assistantPanelWidth";
const SESSION_ID_KEY = "mod.assistantSessionId";
// Id of the open confirm dialog, so the focus-trap effect can
// find it without threading a ref through the portal render.
const CONFIRM_DIALOG_ID = "mod-assistant-confirm-dialog";

// Persist the user's open/closed choice to the SHARED flag (localStorage +
// parent-domain cookie) and broadcast so a live launcher (this app or the
// sibling app in another tab) updates immediately. This is the counterpart
// to the launcher's safeWriteOpenFlag — the panel's own close button has to
// write the SAME winning cookie or the choice won't stick. Canonical
// read/write/broadcast logic now lives in lib/assistant-nav/navActions.ts
// (mod-assistant-agent §6.4) — this is a thin wrapper so call sites below
// don't change.
function persistOpenFlag(open: boolean): void {
    safeWriteOpenFlag(open);
    const relay = createBroadcastRelay();
    relay.postOpenState(open);
    relay.close();
}

const DEFAULT_WIDTH = 400;
const MIN_WIDTH = 320;
const MAX_WIDTH = 720;

const SUGGESTED_PROMPTS = [
    "Create a new node",
    "Show me my usage",
    "Show me my workers",
];

interface Citation { handle?: string; title?: string; url?: string; }
// Canonical action shape now lives in lib/assistant-nav/navActions.ts
// (§6.4) — aliased locally so call sites below don't change.
type ActionEmit = SharedActionEmit;
interface AssistantMsg {
    role: "user" | "assistant" | "error";
    text: string;
    actions?: ActionEmit[];
    citations?: Citation[];
    streaming?: boolean;
    // False on an expired/invalid-confirm-token error: the Retry
    // button must not render, because retrying would silently re-run the turn
    // without a token and the single-use token cannot be re-minted client-side.
    retryable?: boolean;
}

// Confirmation dialog. The orchestrator refuses a risky tool call
// and answers with a gate envelope (services/assistant_confirm.py:check_tool_call
// -> {"allowed": false, "needs_confirmation": true, "confirm_token": ...,
//  "summary": ..., "risk_category": ..., "forced_by": "untrusted_content"}).
// That envelope is the `tool` message the orchestrator persists for the refused
// call (assistant_orchestrator.py:2203), so the transcript replayed by
// GET /assistant/history is the only place the client can see it — the SSE
// stream carries just `tool-call {name, args}` (assistant_orchestrator.py:2229).
interface ConfirmInfo {
    tool: string;
    summary: string;
    forcedBy?: string | null;
}

interface Props {
    accessToken: string | null;
    apiUrl: string;
}

// One-line "what am I being asked to allow" copy for the dialog. The gate's
// own summary is preferred when present; this is the fallback built from the
// `tool-call` args, and every value is rendered as a React text child (never
// innerHTML), so model-authored text cannot inject markup.
function confirmSummary(tool: string, args: unknown, gateSummary?: string): string {
    const named = (gateSummary || "").trim();
    if (named) return named;
    const pairs: string[] = [];
    if (args && typeof args === "object") {
        for (const k of Object.keys(args)) {
            const v = String((args as Record<string, unknown>)[k] ?? "");
            pairs.push(`${k}=${v.length > 60 ? v.slice(0, 60) + "…" : v}`);
        }
    }
    return `assistant wants to run ${tool}: ${pairs.length ? pairs.join(", ") : "(no args)"}`;
}

// The gate's risk category for a call FORCED into confirmation by a tainted
// turn (services/assistant_confirm.py:UNTRUSTED_RISK_CATEGORY).
const UNTRUSTED_RISK_CATEGORY = "untrusted_context";

function sessionId(): string {
    try {
        let sid = sessionStorage.getItem(SESSION_ID_KEY);
        if (!sid) {
            sid = (typeof crypto !== "undefined" && (crypto as any).randomUUID)
                ? (crypto as any).randomUUID()
                : "s-" + Date.now() + "-" + Math.random().toString(36).slice(2);
            sessionStorage.setItem(SESSION_ID_KEY, sid!);
        }
        return sid!;
    } catch {
        return "s-" + Date.now();
    }
}

function readWidth(): number {
    try {
        const raw = parseInt(localStorage.getItem(STORAGE_WIDTH_KEY) || "", 10);
        if (!isNaN(raw) && raw >= MIN_WIDTH && raw <= MAX_WIDTH) return raw;
    } catch { /* noop */ }
    return DEFAULT_WIDTH;
}

function writeWidth(w: number): void {
    try { localStorage.setItem(STORAGE_WIDTH_KEY, String(w)); } catch { /* noop */ }
}

// Cross-subdomain handoff for chain actions that span apps. Delegates
// to the shared runtime (§6.4) so the cookie name/shape can't drift
// from the Dashboard panel's copy.
function writePendingActions(actions: ActionEmit[]): void {
    sharedWritePendingActions(actions);
}

function readAndClearPendingActions(): ActionEmit[] {
    return sharedReadAndClearPendingActions();
}

// Resolve a navigate URL to its target subdomain when the action
// declares a different `app` than this one. Falls back to the
// relative URL when the env var isn't configured (dev / on-prem).
const THIS_APP = "workflow_builder";

function resolveNavigateUrl(action: ActionEmit): string | null {
    // Cross-app URLs come from runtime config (window.__MOD_RUNTIME_CONFIG__
    // via lib/runtime-config; container env in prod, NEXT_PUBLIC_* in dev),
    // never hard-coded. Returns null when the URL isn't configured so the
    // caller skips the navigation rather than silently going to "/".
    const url = action.url || "";
    const app = (action as any).app as string | undefined;
    if (!app || app === THIS_APP) return url;
    if (app === "dashboard") {
        const base = dashboardUrlOrNull();
        if (!base) {
            console.error(
                "[assistant] navigate to dashboard requested but " +
                "the dashboard URL is not configured (runtime-config).",
            );
            return null;
        }
        return base + url;
    }
    if (app === "workflow_builder") {
        return url;
    }
    return url;
}

// Local action runtime — thin wrapper over the canonical shared
// implementation (mod-assistant-agent §6.4,
// lib/assistant-nav/navActions.ts). Cross-app URL resolution stays
// app-specific (Next.js build-time env) and is passed in as a callback.
function executeAction(action: ActionEmit): void {
    sharedExecuteAction(action, { resolveNavigateUrl });
}

function actionLabel(a: ActionEmit): string {
    return sharedActionLabel(a);
}

export default function AssistantPanel({ accessToken, apiUrl }: Props) {
    // FIX-CE-SHOW-ASSISTANT (owner 11-10, "Show them in CE"): the panel works
    // in every edition; the /assistant/* API ships in CE (open-core/manifest.yaml
    // deny_exceptions). The former 2400UI16 CE hide is gone.
    const { loading } = useEntitlements();
    const gateRef = useRef<{ wait: Promise<void>; release: () => void } | null>(null);
    const releasedRef = useRef(false);
    if (!gateRef.current) {
        let release: (() => void) | null = null;
        const wait = new Promise<void>((resolve) => { release = resolve; });
        gateRef.current = { wait, release: release ?? (() => {}) };
    }
    const [enabled, setEnabled] = useState<boolean | null>(null);
    const [open, setOpen] = useState<boolean>(false);
    const [width, setWidth] = useState<number>(() => readWidth());
    const [messages, setMessages] = useState<AssistantMsg[]>([]);
    const [input, setInput] = useState<string>("");
    const [sending, setSending] = useState<boolean>(false);
    const [atBottom, setAtBottom] = useState<boolean>(true);

    // Footer "Contact human support" dialog state. Direct-to-support
    // path for users who don't want to chat first. POSTs to
    // /assistant/escalate which calls the same escalate_assistant_session
    // helper the LLM `contact_support` tool uses, so the audit row,
    // email shape, and tenant-admin fan-out match either way.
    const [escalateOpen, setEscalateOpen] = useState<boolean>(false);
    const [escalateSubject, setEscalateSubject] = useState<string>("");
    const [escalateMessage, setEscalateMessage] = useState<string>("");
    const [escalateSeverity, setEscalateSeverity] =
        useState<"normal" | "emergency">("normal");
    const [escalateSending, setEscalateSending] = useState<boolean>(false);

    // ---- Confirmation dialog state --------------------------
    // `pendingConfirms` is the dialog's payload: one entry per tool the
    // orchestrator refused this turn. Tokens are NEVER stored in this state —
    // they live only in `confirmTokenRef`, a per-send closure cleared the
    // moment the re-send fires (single-use: services/assistant_confirm.py
    // consumes the token server-side, and re-using a stale one would only
    // produce a "Confirm token already used or expired" error).
    const [pendingConfirms, setPendingConfirms] = useState<ConfirmInfo[]>([]);
    const [confirmUntrusted, setConfirmUntrusted] = useState<boolean>(false);
    const [confirmBusy, setConfirmBusy] = useState<boolean>(false);
    const confirmTokenRef = useRef<Record<string, string>>({});

    const messagesEndRef = useRef<HTMLDivElement | null>(null);
    const messagesScrollRef = useRef<HTMLDivElement | null>(null);
    const inputRef = useRef<HTMLInputElement | null>(null);
    const dragStateRef = useRef<{ startX: number; startW: number; dragging: boolean }>({
        startX: 0, startW: 0, dragging: false,
    });
    const historyLoadedRef = useRef<boolean>(false);

    // ---- entitlements watcher --------------------------------------------
    // Releases the one-shot gate the config/history fetches await.
    useEffect(() => {
        if (!loading && gateRef.current && !releasedRef.current) {
            releasedRef.current = true;
            gateRef.current.release();
        }
        // 2400UI16 — bounded fallback: window.fetch has no timeout, so a hung
        // /platform/features leaves `loading` true forever and the gate would
        // never release (panel invisible in PAID). Release it after 5s
        // regardless; release() is idempotent so a late timer is a no-op.
        const fallback = setTimeout(() => {
            if (gateRef.current) gateRef.current.release();
        }, 5000);
        return () => clearTimeout(fallback);
    }, [loading]);

    // ---- per-tenant gate -------------------------------------------------

    // Resilient to TRANSIENT API unavailability — same fragility the launcher
    // had: a single failed config fetch (API mid-restart 502/503, or a network
    // blip) used to set enabled=false permanently, so an API restart while the
    // panel was mounted made it refuse to render even after the API recovered.
    // Retry transient failures with backoff (~30s), re-check on tab focus, and
    // only disable on an explicit server enabled:false or a 401/403. Mirrors
    // assistant-launcher.tsx.
    useEffect(() => {
        if (!accessToken) { setEnabled(false); return; }
        let cancelled = false;

        const fetchOnce = async (): Promise<"ok" | "auth" | "transient"> => {
            try {
                const resp = await fetch(`${apiUrl}/assistant/config`, {
                    headers: { Authorization: `Bearer ${accessToken}` },
                });
                if (resp.ok) {
                    const cfg = await resp.json();
                    if (!cancelled) setEnabled(Boolean(cfg.enabled));
                    return "ok";
                }
                if (resp.status === 401 || resp.status === 403) {
                    if (!cancelled) setEnabled(false);
                    return "auth";
                }
                return "transient";  // 5xx / 502 / 503 — API restarting
            } catch {
                return "transient";  // network error — API unreachable
            }
        };

        (async () => {
            await gateRef.current!.wait;
            if (cancelled) return;
            const backoffs = [250, 500, 1000, 2000, 4000, 4000, 4000, 4000, 4000];
            for (let i = 0; i <= backoffs.length && !cancelled; i++) {
                const outcome = await fetchOnce();
                if (outcome !== "transient") return;  // definitive answer — done
                if (i < backoffs.length) {
                    await new Promise((r) => setTimeout(r, backoffs[i]));
                }
            }
        })();

        const onFocus = () => { if (!cancelled) fetchOnce(); };
        window.addEventListener("focus", onFocus);
        return () => {
            cancelled = true;
            window.removeEventListener("focus", onFocus);
        };
    }, [accessToken, apiUrl]);

    // ---- resume the most-recent server session ---------------------------
    // The assistant session is server-owned per (user, tenant) with a
    // retention window, so resuming it here makes the conversation carry
    // across page navigation AND across the Dashboard/Builder subdomains
    // (sessionStorage can't — different origins). On mount we adopt the
    // server's latest session id + repaint its transcript. Only replaces a
    // still-empty panel, so it never clobbers a message typed before history
    // landed. "New chat" (startNewConversation) is the explicit fresh start.
    useEffect(() => {
        if (!accessToken || historyLoadedRef.current) return;
        let cancelled = false;
        (async () => {
            await gateRef.current!.wait;
            if (cancelled) return;
            historyLoadedRef.current = true;
            try {
                const resp = await fetch(`${apiUrl}/assistant/history`, {
                    headers: { Authorization: `Bearer ${accessToken}` },
                });
                if (!resp.ok) return;
                const data = await resp.json();
                if (cancelled || !data || !data.session_id) return;
                try { sessionStorage.setItem(SESSION_ID_KEY, data.session_id); } catch { /* noop */ }
                const restored: AssistantMsg[] = (data.messages || []).map((m: any) => ({
                    role: m.role === "assistant" ? "assistant" : "user",
                    text: m.content || "",
                    actions: Array.isArray(m.actions) && m.actions.length ? m.actions : undefined,
                }));
                if (restored.length) {
                    setMessages(prev => (prev.length === 0 ? restored : prev));
                }
            } catch { /* transient — leave a fresh session */ }
        })();
        return () => { cancelled = true; };
    }, [accessToken, apiUrl]);

    // ---- watch body class flips (the AssistantLauncher toggles it) -------

    useEffect(() => {
        if (typeof document === "undefined") return;
        const sync = () => {
            setOpen(document.body.classList.contains(BODY_CLASS));
        };
        sync();
        const obs = new MutationObserver(sync);
        obs.observe(document.body, { attributes: true, attributeFilter: ["class"] });
        return () => obs.disconnect();
    }, []);

    // ---- focus input on open ---------------------------------------------

    useEffect(() => {
        if (open && inputRef.current) inputRef.current.focus();
    }, [open]);

    // ---- drain any pending cross-page actions on mount -------------------
    //
    // When the assistant emits a chain action like [navigate → open-
    // dialog], the navigate fires immediately (page unloads); the
    // open-dialog gets persisted to a cookie scoped to the parent
    // domain so it survives the cross-app navigation. Here on mount
    // we read + clear that cookie and dispatch what's left. A short
    // timeout gives the WB's mod:assistant-open-dialog listener time
    // to mount.
    useEffect(() => {
        const pending = readAndClearPendingActions();
        if (pending.length === 0) return;
        const t = setTimeout(() => {
            pending.forEach((a) => executeAction(a));
        }, 300);
        return () => clearTimeout(t);
    }, []);

    // ---- scroll behaviour ------------------------------------------------

    const scrollToBottom = useCallback(() => {
        const el = messagesScrollRef.current;
        if (el) el.scrollTop = el.scrollHeight;
        setAtBottom(true);
    }, []);

    useEffect(() => {
        if (atBottom) scrollToBottom();
    }, [messages, atBottom, scrollToBottom]);

    const onScroll = useCallback(() => {
        const el = messagesScrollRef.current;
        if (!el) return;
        const buffer = 24;
        setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < buffer);
    }, []);

    // ---- close handler ---------------------------------------------------

    const close = useCallback(() => {
        document.body.classList.remove(BODY_CLASS);
        persistOpenFlag(false);
    }, []);

    const startNewConversation = useCallback(() => {
        // Drop the per-tab session id so the backend allocates a fresh
        // AssistantSession on the next turn. Clear messages + reset
        // scroll state; React focuses the input on the next render.
        try { sessionStorage.removeItem(SESSION_ID_KEY); } catch { /* noop */ }
        setMessages([]);
        setInput("");
        setAtBottom(true);
        if (inputRef.current) inputRef.current.focus();
    }, []);

    // ---- send + SSE ------------------------------------------------------

    const send = useCallback(async (text: string, confirmTokens?: Record<string, string>) => {
        const trimmed = (text || "").trim();
        if (!trimmed) return;
        // The tokens handed to us are consumed HERE, never stored:
        // they are single-use (services/assistant_confirm.py:_consume) and the
        // caller clears its own reference right after this call.
        const tokens = confirmTokens && Object.keys(confirmTokens).length
            ? confirmTokens
            : null;
        setMessages(m => [
            ...m,
            { role: "user", text: trimmed },
            { role: "assistant", text: "", streaming: true, actions: [], citations: [] },
        ]);
        setInput("");
        setSending(true);
        // What this turn needs confirmed, collected from the stream.
        // Local (not state) so a second send can never inherit a stale token.
        const pending: ConfirmInfo[] = [];
        let untrusted = false;
        try {
            const resp = await fetch(`${apiUrl}/assistant/chat`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
                },
                body: JSON.stringify({
                    message: trimmed,
                    session_id: sessionId(),
                    // pathname + hash so a Dashboard tab + hash routing
                    // disambiguates from the bare WB canvas.
                    current_page: typeof location !== "undefined"
                        ? location.pathname + (location.hash || "")
                        : "",
                    // Disambiguates "/" — same URL in both site-map
                    // entries (Dashboard home + WB canvas).
                    current_app: "workflow_builder",
                    // The one-time tokens the user just agreed to.
                    // Omitted (not `{}`) when there are none so the request
                    // shape is unchanged for ordinary turns.
                    ...(tokens ? { confirm_tokens: tokens } : {}),
                }),
            });
            if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
            await consumeStream(resp.body, (event, data) => {
                if (event === "text-token" && typeof data?.text === "string") {
                    setMessages(m => {
                        const copy = m.slice();
                        const last = copy[copy.length - 1];
                        if (last && last.role === "assistant") {
                            copy[copy.length - 1] = { ...last, text: (last.text || "") + data.text };
                        }
                        return copy;
                    });
                } else if (event === "action-emit") {
                    setMessages(m => {
                        const copy = m.slice();
                        const last = copy[copy.length - 1];
                        if (last && last.role === "assistant") {
                            copy[copy.length - 1] = {
                                ...last,
                                actions: [...(last.actions || []), data as ActionEmit],
                            };
                        }
                        return copy;
                    });
                } else if (event === "citation") {
                    setMessages(m => {
                        const copy = m.slice();
                        const last = copy[copy.length - 1];
                        if (last && last.role === "assistant") {
                            copy[copy.length - 1] = {
                                ...last,
                                citations: [...(last.citations || []), data as Citation],
                            };
                        }
                        return copy;
                    });
                } else if (event === "error" && data?.message) {
                    const msg = String(data.message);
                    // An expired/invalid confirm token is the
                    // orchestrator's own error (assistant_confirm.py:316/320).
                    // Show it verbatim and offer NOTHING automatic: the Retry
                    // button would re-run the turn without a token, which is
                    // exactly the loop the single-use design forbids.
                    const tokenish = /confirm token/i.test(msg);
                    setMessages(m => {
                        const copy = m.slice();
                        copy.pop();
                        copy.push({
                            role: "error", text: msg,
                            ...(tokenish ? { retryable: false } : {}),
                        });
                        return copy;
                    });
                    if (tokenish) {
                        // The dialog is already gone (Confirm closed it); drop
                        // any stale pending list so the user cannot re-confirm.
                        setPendingConfirms([]);
                        setConfirmUntrusted(false);
                    }
                } else if (event === "needs-confirmation"
                    || (event === "tool-call" && data?.needs_confirmation === true)) {
                    // The gate envelope. See the API-gap note at
                    // the top of this file for where the token still has to
                    // come from.
                    const name = String(data?.name ?? data?.tool ?? "");
                    if (!name) return;
                    const summary = confirmSummary(
                        name, data?.args, typeof data?.summary === "string" ? data.summary : undefined,
                    );
                    const forced = data?.forced_by === "untrusted_content"
                        || data?.risk_category === UNTRUSTED_RISK_CATEGORY;
                    if (forced) untrusted = true;
                    pending.push({
                        tool: name,
                        summary,
                        forcedBy: forced ? "untrusted_content" : null,
                    });
                    // The token is held ONLY in the per-send closure ref.
                    if (typeof data?.confirm_token === "string" && data.confirm_token) {
                        confirmTokenRef.current = {
                            ...confirmTokenRef.current,
                            [name]: data.confirm_token,
                        };
                    }
                }
                // event === "session" / "done" — no-op
            });
            // mark streaming done
            setMessages(m => {
                const copy = m.slice();
                const last = copy[copy.length - 1];
                if (last && last.role === "assistant") {
                    copy[copy.length - 1] = { ...last, streaming: false };
                }
                return copy;
            });
            // The turn ended with refusals: open the dialog. The
            // tokens stay in confirmTokenRef (cleared only when Confirm fires).
            if (pending.length > 0) {
                setPendingConfirms(pending);
                setConfirmUntrusted(untrusted);
                // A fresh dialog means the previous re-send finished: release
                // the guard so this one can fire.
                setConfirmBusy(false);
            }
        } catch (err) {
            setMessages(m => {
                const copy = m.slice();
                copy.pop();
                copy.push({ role: "error", text: "Network or server error. Click Retry." });
                return copy;
            });
        } finally {
            setSending(false);
        }
    }, [apiUrl, accessToken]);

    const retryLast = useCallback(() => {
        // Find the last user message and resubmit it.
        for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].role === "user") {
                send(messages[i].text);
                return;
            }
        }
    }, [messages, send]);

    // ---- Confirmation dialog handlers -----------------------

    const cancelConfirm = useCallback(() => {
        // Cancel sends NOTHING. The pending tokens (if the stream ever
        // carried any) are dropped here so they can never be replayed.
        confirmTokenRef.current = {};
        setPendingConfirms([]);
        setConfirmUntrusted(false);
        setMessages(m => [
            ...m,
            { role: "assistant", text: "Action cancelled", actions: [], citations: [] },
        ]);
    }, [setMessages]);

    // Focus trap + Escape = Cancel while the dialog is open. Mirrors the
    // Dashboard panel's modA11y.attach (CORE/Dashboard/js/modal.js:127): a
    // capture-phase document keydown that keeps Tab inside the dialog and
    // routes Escape to Cancel. Effects flush after the render pass, so
    // confirmDialogRef is populated by the time this runs.
    useEffect(() => {
        if (typeof document === "undefined" || pendingConfirms.length === 0) return;
        const container = document.getElementById(CONFIRM_DIALOG_ID);
        if (!container) return;
        const focusables = container.querySelectorAll(
            'button:not([disabled]),input,textarea,select,a[href],[tabindex]:not([tabindex="-1"])',
        );
        const first = focusables.length ? focusables[0] : null;
        const last = focusables.length ? focusables[focusables.length - 1] : null;
        const onKey = (e: KeyboardEvent) => {
            if (!container.isConnected) return;
            if (e.key === "Escape") {
                e.stopPropagation();
                cancelConfirm();
                return;
            }
            if (e.key !== "Tab") return;
            const active = document.activeElement;
            const inside = active === container || container.contains(active);
            if (!inside) {
                e.preventDefault();
                if (first) (first as HTMLElement).focus({ preventScroll: true });
                return;
            }
            if (e.shiftKey) {
                if (first && active === first) {
                    e.preventDefault();
                    if (last) (last as HTMLElement).focus({ preventScroll: true });
                }
            } else if (last && active === last) {
                e.preventDefault();
                if (first) (first as HTMLElement).focus({ preventScroll: true });
            }
        };
        document.addEventListener("keydown", onKey, true);
        return () => document.removeEventListener("keydown", onKey, true);
    }, [pendingConfirms, cancelConfirm]);

    const confirmAndResend = useCallback(async () => {
        if (confirmBusy) return;
        setConfirmBusy(true);
        // Close the dialog BEFORE firing so a re-entrant stream cannot
        // re-open it behind itself.
        setPendingConfirms([]);
        setConfirmUntrusted(false);
        // Take the tokens exactly once, then clear the ref: single-use.
        const tokens = confirmTokenRef.current;
        confirmTokenRef.current = {};
        // Resubmit the last user message with them (same request shape +
        // confirm_tokens, per services/assistant_orchestrator.py:1858).
        for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].role === "user") {
                send(messages[i].text, tokens);
                return;
            }
        }
        // No user turn to replay (dialog opened by a resume, not a send):
        // nothing to re-send, so just release the busy flag.
        setConfirmBusy(false);
    }, [confirmBusy, messages, send, setConfirmBusy]);

    const submitEscalate = useCallback(async (e?: React.FormEvent) => {
        if (e) e.preventDefault();
        if (escalateSending) return;
        if (!escalateSubject.trim() || !escalateMessage.trim()) return;
        setEscalateSending(true);
        try {
            const headers: Record<string, string> = {
                "Content-Type": "application/json",
            };
            if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
            const resp = await fetch(`${apiUrl}/assistant/escalate`, {
                method: "POST", headers,
                body: JSON.stringify({
                    session_id: sessionId(),
                    subject: escalateSubject,
                    message: escalateMessage,
                    severity: escalateSeverity,
                }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            // Push a synthetic assistant message carrying the
            // support-contacted action so the receipt renders inline
            // with chat history. No call to the orchestrator — direct
            // path.
            setMessages(m => [
                ...m,
                {
                    role: "assistant",
                    text: data.ok
                        ? "I've contacted human support on your behalf."
                        : `I couldn't reach support: ${data.message || "unknown error"}.`,
                    actions: data.ok
                        ? [{
                            action: "support-contacted",
                            recipient: data.recipient,
                            severity: data.severity,
                            tenant_admin_count: data.tenant_admin_count,
                            subject: escalateSubject,
                        } as any]
                        : undefined,
                },
            ]);
            setEscalateOpen(false);
            setEscalateSubject("");
            setEscalateMessage("");
            setEscalateSeverity("normal");
        } catch (err) {
            setMessages(m => [
                ...m,
                {
                    role: "error",
                    text: `Could not send: ${err instanceof Error ? err.message : "unknown error"}`,
                },
            ]);
        } finally {
            setEscalateSending(false);
        }
    }, [
        escalateSending, escalateSubject, escalateMessage, escalateSeverity,
        apiUrl, accessToken,
    ]);

    // ---- resize drag -----------------------------------------------------

    const onResizeMouseDown = useCallback((e: React.MouseEvent) => {
        dragStateRef.current = {
            startX: e.clientX,
            startW: width,
            dragging: true,
        };
        e.preventDefault();
    }, [width]);

    useEffect(() => {
        const onMove = (e: MouseEvent) => {
            if (!dragStateRef.current.dragging) return;
            const delta = dragStateRef.current.startX - e.clientX;
            const next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, dragStateRef.current.startW + delta));
            setWidth(next);
        };
        const onUp = () => {
            if (!dragStateRef.current.dragging) return;
            dragStateRef.current.dragging = false;
            writeWidth(width);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        return () => {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
        };
    }, [width]);

    // ---- sync body CSS variable for the push-content rule ---------------
    //
    // The panel is `position: fixed` so it sits on top of the canvas
    // unless we make the rest of the page reserve space for it.
    // Tracking the width in --mod-asst-panel-w lets the body
    // padding-right (set by globals.css) keep up with drag-resize.
    useEffect(() => {
        if (typeof document === "undefined") return;
        if (enabled === true && open) {
            document.body.style.setProperty("--mod-asst-panel-w", `${width}px`);
        }
    }, [enabled, open, width]);

    useEffect(() => {
        return () => {
            // On unmount, clean up the body CSS variable so the
            // padding rule falls back to the default.
            if (typeof document !== "undefined") {
                document.body.style.removeProperty("--mod-asst-panel-w");
            }
        };
    }, []);

    // ---- gating ----------------------------------------------------------

    if (enabled !== true) return null;

    // ---- render ----------------------------------------------------------

    const showEmpty = messages.length === 0;

    // The panel renders via createPortal so its `position: fixed` is
    // scoped to the viewport, NOT to any ancestor (ReactFlow's
    // transform-wrapper would scope it otherwise and the panel would
    // shrink to the canvas).
    const panel = (
        <aside
            role="complementary"
            aria-label="MOD Assistant"
            className={
                "fixed right-0 z-50 flex flex-col bg-card text-card-foreground " +
                "border border-r-0 border-border rounded-l-xl shadow-xl " +
                "transition-transform duration-200 " +
                (open ? "translate-x-0" : "translate-x-full")
            }
            style={{ width, top: "10vh", height: "80vh" }}
        >
            {/* left-edge drag handle */}
            <div
                onMouseDown={onResizeMouseDown}
                role="separator"
                aria-orientation="vertical"
                className="absolute left-0 top-0 bottom-0 -translate-x-1/2 w-1.5 cursor-col-resize hover:bg-accent/30"
            />

            {/* header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-border flex-shrink-0">
                <span className="flex items-center gap-2 text-sm font-semibold">
                    <Bot className="w-4 h-4" />
                    {wbNavLabel('wb-nav-assistant', 'MOD Assistant')}
                </span>
                <div className="flex items-center gap-1">
                    <button
                        type="button"
                        onClick={startNewConversation}
                        title="Start a new conversation"
                        className="px-2 py-1 text-xs rounded hover:bg-muted whitespace-nowrap"
                    >
                        New Conversation
                    </button>
                    <button
                        type="button"
                        onClick={close}
                        title="Close (Esc)"
                        aria-label="Close"
                        className="p-1 rounded hover:bg-muted"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {/* messages */}
            <div
                ref={messagesScrollRef}
                onScroll={onScroll}
                className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-3"
            >
                {showEmpty && (
                    <div className="flex flex-col gap-2 items-start">
                        <p className="text-sm text-muted-foreground">
                            Hi! I&apos;m MOD Assistant. Ask me where to go, how to do
                            something, or why a run failed.
                        </p>
                        {SUGGESTED_PROMPTS.map(p => (
                            <button
                                key={p}
                                type="button"
                                onClick={() => send(p)}
                                className="bg-muted text-foreground border border-border rounded-full px-3 py-1.5 text-xs hover:bg-accent/10"
                            >
                                {p}
                            </button>
                        ))}
                    </div>
                )}

                {messages.map((m, idx) => {
                    if (m.role === "user") {
                        return (
                            <div key={idx} className="self-end max-w-[90%] flex flex-col items-end">
                                <div className="px-3 py-2 rounded-xl text-sm bg-primary text-primary-foreground whitespace-pre-wrap break-words">
                                    {m.text}
                                </div>
                            </div>
                        );
                    }
                    if (m.role === "error") {
                        return (
                            <div key={idx} className="self-stretch flex flex-col gap-1">
                                <div className="px-3 py-2 rounded-xl text-sm bg-destructive/15 border border-destructive/40 text-foreground">
                                    {m.text}
                                </div>
                                {m.retryable === false ? null : (
                                    <button
                                        type="button"
                                        onClick={retryLast}
                                        className="self-start bg-accent text-accent-foreground rounded-lg px-3 py-1.5 text-xs hover:brightness-110"
                                    >
                                        Retry
                                    </button>
                                )}
                            </div>
                        );
                    }
                    // assistant
                    return (
                        <div key={idx} className="self-start max-w-[90%] flex flex-col items-start gap-1.5">
                            <div className="px-3 py-2 rounded-xl text-sm bg-muted text-foreground whitespace-pre-wrap break-words">
                                {m.text === "" && m.streaming
                                    ? <span className="italic text-muted-foreground">thinking…</span>
                                    : m.text}
                            </div>
                            {m.actions && m.actions.length > 0 && (
                                <div className="flex flex-col gap-1.5 w-full">
                                    {m.actions.map((a, i) => {
                                        // support-contacted is a confirmation
                                        // receipt, not a clickable action.
                                        if (a.action === "support-contacted") {
                                            const recipient = (a as any).recipient as string | undefined;
                                            const severity = (a as any).severity as string | undefined;
                                            const adminCount = (a as any).tenant_admin_count as number | undefined;
                                            const subject = (a as any).subject as string | undefined;
                                            return (
                                                <div
                                                    key={i}
                                                    className="flex gap-2 items-start rounded-lg px-3 py-2 text-[12px] bg-primary/[0.06] border border-primary/20"
                                                >
                                                    <span className="text-primary font-bold leading-none">✓</span>
                                                    <div className="flex flex-col gap-0.5">
                                                        <div className="font-semibold text-foreground">
                                                            {severity === "emergency"
                                                                ? "Support contacted — flagged emergency"
                                                                : "Support contacted"}
                                                        </div>
                                                        <div className="text-muted-foreground">
                                                            Transcript emailed to {recipient || "your address"}
                                                            {adminCount && adminCount > 0
                                                                ? ` (cc'd ${adminCount} tenant admin${adminCount === 1 ? "" : "s"})`
                                                                : ""}
                                                        </div>
                                                        {subject ? (
                                                            <div className="text-muted-foreground">Subject: {subject}</div>
                                                        ) : null}
                                                    </div>
                                                </div>
                                            );
                                        }
                                        return (
                                            <button
                                                key={i}
                                                type="button"
                                                onClick={() => executeAction(a)}
                                                className="bg-accent text-accent-foreground rounded-lg px-3 py-1.5 text-xs font-medium text-left hover:brightness-110"
                                            >
                                                {actionLabel(a)}
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                            {m.citations && m.citations.length > 0 && (
                                <div className="flex flex-col gap-1 mt-1">
                                    {m.citations.map((c, i) => (
                                        <a
                                            key={i}
                                            href={c.url || "#"}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="text-[11px] text-muted-foreground underline hover:text-foreground"
                                        >
                                            Source: {c.title || "docs"}
                                        </a>
                                    ))}
                                </div>
                            )}
                        </div>
                    );
                })}
                <div ref={messagesEndRef} />
            </div>

            {/* jump-to-latest pill */}
            {!atBottom && (
                <button
                    type="button"
                    onClick={scrollToBottom}
                    className="absolute bottom-20 left-1/2 -translate-x-1/2 bg-primary text-primary-foreground rounded-full px-3 py-1 text-[11px] shadow-lg"
                >
                    Jump to latest
                </button>
            )}

            {/* escalate dialog — collapsed by default */}
            {escalateOpen && (
                <form
                    onSubmit={submitEscalate}
                    className="mx-4 mb-2 flex flex-col gap-2 p-3 rounded-lg bg-muted border border-border"
                >
                    <div className="text-sm font-semibold">Contact human support</div>
                    <input
                        type="text"
                        value={escalateSubject}
                        onChange={(e) => setEscalateSubject(e.target.value)}
                        placeholder="Short subject (e.g. 'Workers stuck')"
                        maxLength={200}
                        autoFocus
                        className="w-full px-2 py-1.5 text-sm border border-border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-primary"
                    />
                    <textarea
                        value={escalateMessage}
                        onChange={(e) => setEscalateMessage(e.target.value)}
                        placeholder="What's happening? Your chat transcript is attached automatically."
                        rows={3}
                        maxLength={2000}
                        className="w-full px-2 py-1.5 text-sm border border-border rounded-md bg-background resize-y focus:outline-none focus:ring-2 focus:ring-primary"
                    />
                    <div className="flex items-center gap-2">
                        <label className="text-xs text-muted-foreground">Severity:</label>
                        <select
                            value={escalateSeverity}
                            onChange={(e) => setEscalateSeverity(e.target.value as "normal" | "emergency")}
                            className="px-2 py-1 text-xs border border-border rounded-md bg-background"
                        >
                            <option value="normal">Normal</option>
                            <option value="emergency">Emergency — service down / data loss</option>
                        </select>
                    </div>
                    <div className="flex gap-2">
                        <button
                            type="submit"
                            disabled={escalateSending || !escalateSubject.trim() || !escalateMessage.trim()}
                            className="bg-primary text-primary-foreground rounded-lg px-3 py-1.5 text-xs font-medium hover:brightness-110 disabled:opacity-60 disabled:cursor-not-allowed"
                        >
                            {escalateSending ? "Sending…" : "Send to support"}
                        </button>
                        <button
                            type="button"
                            onClick={() => setEscalateOpen(false)}
                            className="text-xs text-muted-foreground underline hover:text-foreground"
                        >
                            Cancel
                        </button>
                    </div>
                </form>
            )}

            {/* Confirmation dialog. role=dialog + aria-modal +
                aria-labelledby; Escape = Cancel (focus trap installed by the
                effect above). Every tool name / arg summary is a React text
                child, so model-authored text can never inject markup. */}
            {pendingConfirms.length > 0 && (
                <div
                    id={CONFIRM_DIALOG_ID}
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="mod-asst-confirm-title"
                    tabIndex={-1}
                    className="fixed inset-0 z-60 flex items-center justify-center bg-black/45"
                >
                    <div
                        className="w-full max-w-3xl mx-6 rounded-lg bg-card border border-border shadow-xl"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div id="mod-asst-confirm-title" className="px-4 pt-3 text-sm font-semibold">
                            Confirm before the assistant acts
                        </div>
                        {confirmUntrusted && (
                            <p className="px-4 py-2 text-xs text-muted-foreground">
                                This turn included content from outside MOD (logs, docs, or your
                                own uploads), which the assistant must treat as data, not
                                instructions. Anything that writes or sends now needs your
                                confirmation.
                            </p>
                        )}
                        <div className="px-4 py-2 flex flex-col gap-2">
                            {pendingConfirms.map((c, i) => (
                                <div key={i} className="flex flex-col gap-1">
                                    <div className="text-xs font-semibold">
                                        {c.tool}
                                    </div>
                                    <div className="text-xs text-muted-foreground">
                                        {c.summary}
                                    </div>
                                    {c.forcedBy === "untrusted_content" ? (
                                        <div className="text-[11px] text-muted-foreground">
                                            Forced by untrusted content in this turn.
                                        </div>
                                    ) : null}
                                </div>
                            ))}
                        </div>
                        <div className="px-4 py-3 flex gap-2 items-center">
                            <button
                                type="button"
                                onClick={() => void confirmAndResend()}
                                className="bg-primary text-primary-foreground rounded-lg px-3 py-1.5 text-xs font-medium hover:brightness-110"
                            >
                                Confirm
                            </button>
                            <button
                                type="button"
                                onClick={cancelConfirm}
                                className="text-xs text-muted-foreground underline hover:text-foreground"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* input */}
            <form
                onSubmit={(e) => { e.preventDefault(); send(input); }}
                className="flex gap-2 px-4 py-3 border-t border-border flex-shrink-0"
            >
                <input
                    ref={inputRef}
                    type="text"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder="Ask me anything…"
                    autoComplete="off"
                    className="flex-1 px-3 py-2 border border-border rounded-lg text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary"
                />
                <button
                    type="submit"
                    disabled={sending}
                    className="bg-primary text-primary-foreground rounded-lg px-4 py-2 text-sm font-medium hover:brightness-110 disabled:opacity-60 disabled:cursor-not-allowed"
                >
                    Send
                </button>
            </form>

            {/* footer: "Contact human support" — direct path bypassing chat */}
            <div className="flex justify-center pb-2 pt-1 flex-shrink-0">
                <button
                    type="button"
                    onClick={() => setEscalateOpen(true)}
                    className="text-[11px] text-muted-foreground underline hover:text-primary"
                >
                    Contact human support
                </button>
            </div>
        </aside>
    );

    // Portal to body so position:fixed sees the viewport and not any
    // transformed ancestor (ReactFlow's canvas).
    return typeof document === "undefined" ? null : createPortal(panel, document.body);
}

// SSE consumer shared with the Dashboard panel via spec; kept inline
// here so this component is a single file.
async function consumeStream(
    body: ReadableStream<Uint8Array>,
    onEvent: (event: string, data: any) => void,
): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            let event = "message";
            let data = "";
            block.split("\n").forEach(line => {
                if (line.startsWith("event:")) event = line.slice(6).trim();
                else if (line.startsWith("data:")) data += line.slice(5).trim();
            });
            if (!data) continue;
            try { onEvent(event, JSON.parse(data)); } catch { /* drop malformed */ }
        }
    }
}
