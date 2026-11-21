"use client";

/**
 * MOD Assistant — WorkflowBuilder launcher (mod-assistant-agent §7).
 *
 * Robot icon button rendered in the canvas top-right toolbar row,
 * immediately to the LEFT of <NotificationsBell>. Click toggles a
 * body-level class `assistant-panel-open` that §8's panel will hook
 * into — for §7 itself the class is just an observable open/closed
 * flag.
 *
 * Shares two pieces of state with the Dashboard launcher:
 *   - localStorage key `mod.assistantPanelOpen` ("0" | "1")
 *   - BroadcastChannel "mod_assistant" with messages
 *       { type: "open-state", open: boolean }
 *
 * That gives us cross-tab AND cross-app open-state sync (a user
 * who opens the panel in WorkflowBuilder sees it open the next
 * time they hop to the Dashboard, and vice versa).
 *
 * Per-tenant enable gate: GETs /v1/assistant/config on mount; if
 * `enabled: false`, the component renders null and registers no
 * keyboard listeners. The fetch failing (auth unavailable, network)
 * also yields null — safer to hide than to show a broken launcher.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { MessageCircle } from "lucide-react";
import {
    BODY_CLASS,
    BroadcastRelay,
    createBroadcastRelay,
    safeReadOpenFlag,
    safeWriteOpenFlag,
} from "@/lib/assistant-nav/navActions";
import { useEntitlements } from "@/hooks/use-entitlements";

interface Props {
    accessToken: string | null;
    apiUrl: string;
}

export default function AssistantLauncher({ accessToken, apiUrl }: Props) {
    // FIX-CE-SHOW-ASSISTANT (owner 11-10, "Show them in CE"): the launcher is
    // shown in every edition. The /assistant/* API ships in CE
    // (open-core/manifest.yaml deny_exceptions, FIX-MOD-ASSISTANT-IN-CE-MANIFEST),
    // so the former CE hide (2400UI16) is gone. If the API is unreachable the
    // fetch loop below leaves the launcher hidden (same as before).
    const { loading } = useEntitlements();
    const [enabled, setEnabled] = useState<boolean | null>(null);
    // One-shot gate created on the component's FIRST render (useRef keeps the
    // same object across re-renders) so the fetch loop awaits the very promise
    // this watcher releases when the entitlements load finishes.
    const gateRef = useRef<{ wait: Promise<void>; release: () => void } | null>(null);
    if (!gateRef.current) {
        let release: (() => void) | null = null;
        const wait = new Promise<void>((resolve) => { release = resolve; });
        gateRef.current = { wait, release: release ?? (() => {}) };
    }
    const [open, setOpen] = useState<boolean>(() => safeReadOpenFlag());
    const relayRef = useRef<BroadcastRelay | null>(null);

    // Apply the open flag to <body> AND optionally broadcast to
    // peer tabs. broadcast=false on the receiving side prevents an
    // infinite postMessage loop.
    const applyOpenState = useCallback(
        (next: boolean, opts: { broadcast: boolean }) => {
            setOpen(next);
            if (typeof document !== "undefined") {
                document.body.classList.toggle(BODY_CLASS, next);
            }
            safeWriteOpenFlag(next);
            if (opts.broadcast && relayRef.current) {
                relayRef.current.postOpenState(next);
            }
        },
        [],
    );

    const toggle = useCallback(() => {
        applyOpenState(!open, { broadcast: true });
    }, [applyOpenState, open]);

    const close = useCallback(() => {
        if (open) applyOpenState(false, { broadcast: true });
    }, [applyOpenState, open]);

    // Entitlements watcher. Releases the one-shot gate the config-fetch loop
    // awaits (request timing unchanged: the gate resolves at the exact moment
    // /platform/features finishes).
    useEffect(() => {
        if (!loading && gateRef.current) gateRef.current.release();
        // 2400UI16 — bounded fallback: window.fetch has no timeout, so a hung
        // /platform/features leaves `loading` true forever and the gate would
        // never release (assistant invisible in PAID). Release it after 5s
        // regardless; release() is idempotent so a late timer is a no-op.
        const fallback = setTimeout(() => {
            if (gateRef.current) gateRef.current.release();
        }, 5000);
        return () => clearTimeout(fallback);
    }, [loading]);

    // Fetch the per-tenant enable flag. Resilient to TRANSIENT API
    // unavailability: previously a single failed fetch (API mid-restart
    // returning 502/503, or a network blip) set enabled=false *permanently*
    // — so any API restart while the Builder was open made the assistant
    // button silently vanish until a full page reload. Now we retry transient
    // failures with backoff (~30s, covers a full restart), re-check when the
    // tab regains focus, and only disable definitively on an explicit server
    // `enabled:false` or a 401/403. Mirrors the Dashboard launcher's
    // resilience (assistant-launcher.js).
    useEffect(() => {
        if (!accessToken) {
            setEnabled(false);  // not authed yet; effect re-runs when the token arrives
            return;
        }
        let cancelled = false;

        // Returns "ok"/"auth" (definitive — stop) or "transient" (retry).
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
            // Wait for the entitlements read to finish (or for the attempt to
            // fail; the gate always releases) before touching /assistant/config.
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
            // Budget exhausted on transient errors — stay hidden; the focus
            // handler re-checks when the user returns to the tab.
        })();

        // Recover a button hidden by a transient blip: re-check on focus.
        const onFocus = () => { if (!cancelled) fetchOnce(); };
        window.addEventListener("focus", onFocus);
        return () => {
            cancelled = true;
            window.removeEventListener("focus", onFocus);
        };
    }, [accessToken, apiUrl]);

    // Hydrate the body class from persisted state ONCE on mount.
    // We don't want to re-sync every render or we'd fight user
    // clicks. The `open` state itself drives subsequent updates.
    useEffect(() => {
        if (typeof document === "undefined") return;
        document.body.classList.toggle(BODY_CLASS, safeReadOpenFlag());
        // No cleanup — leaving the class behind is intentional so a
        // mid-session unmount doesn't yank the panel closed.
    }, []);

    // BroadcastChannel for cross-tab + cross-app open-state sync.
    // Canonical relay lives in lib/assistant-nav/navActions.ts (§6.4).
    useEffect(() => {
        if (enabled !== true) return;
        const relay = createBroadcastRelay((open) => {
            applyOpenState(open, { broadcast: false });
        });
        relayRef.current = relay;
        return () => {
            relay.close();
            relayRef.current = null;
        };
    }, [enabled, applyOpenState]);

    // Keyboard shortcuts: Ctrl/Cmd+Shift+A toggles, Escape closes
    // when open. Bound globally to the document so the user doesn't
    // have to focus the launcher first.
    useEffect(() => {
        if (enabled !== true) return;
        const handler = (e: KeyboardEvent) => {
            if (
                e.shiftKey &&
                (e.ctrlKey || e.metaKey) &&
                (e.key === "A" || e.key === "a")
            ) {
                e.preventDefault();
                toggle();
                return;
            }
            if (e.key === "Escape" && document.body.classList.contains(BODY_CLASS)) {
                close();
            }
        };
        document.addEventListener("keydown", handler);
        return () => document.removeEventListener("keydown", handler);
    }, [enabled, toggle, close]);

    if (enabled !== true) return null;

    // Visual: brand-token styling per
    // [[reference_dashboard_brand_tokens]] so dark mode (now
    // default) renders correctly without per-mode CSS. The classes
    // map to shadcn tokens defined in WorkflowBuilder's globals.
    return (
        <button
            type="button"
            id="assistant-launcher"
            title="Ask MOD Assistant (Ctrl+Shift+A)"
            aria-label="Open MOD Assistant"
            aria-pressed={open}
            onClick={toggle}
            className={
                // Inline topbar icon button — matches the notification bell +
                // theme toggle (which sit to its right). No longer a
                // bottom-right FAB.
                "relative p-2 text-gray-600 hover:text-gray-800 hover:bg-gray-100 " +
                "rounded-lg transition-colors focus:outline-none " +
                (open ? "bg-gray-100 text-gray-800" : "")
            }
        >
            <span className="relative inline-flex">
                <MessageCircle className="w-6 h-6" aria-hidden="true" />
                {/* AI sparkle accent (the industry-standard "AI" signifier) */}
                <svg viewBox="0 0 24 24" className="absolute -top-1.5 -right-2 w-3 h-3" fill="currentColor" aria-hidden="true">
                    <path d="M12 2l1.6 6.4L20 10l-6.4 1.6L12 18l-1.6-6.4L4 10l6.4-1.6z" />
                </svg>
            </span>
        </button>
    );
}
