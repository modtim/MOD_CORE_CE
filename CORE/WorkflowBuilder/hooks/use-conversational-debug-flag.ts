"use client";

// WSIP 4.5b — the tenant's `conversational_debug_enabled` flag, read once per
// access token, gates RunRail's "Why did this happen?" explain button.
//
// The backend (task 666, being built in parallel) serves
//   GET {api}/wsip/flags -> {"conversational_debug_enabled": boolean}
// authed with `Authorization: Bearer <access token>`. Until that router is
// mounted the fetch 404s, so the button stays hidden — FAIL CLOSED everywhere:
// default false, and any non-ok response (404 unmounted, 401, 5xx), network
// error, or non-boolean field resolves to false. No console.log.
//
// One fetch per accessToken (the effect re-runs only when the token changes),
// and no fetch at all when the token is null. On unmount the result is
// ignored (cancelled flag), so a torn-down component never flips the gate.

import { useEffect, useState } from "react";

/** Shape of GET /wsip/flags — only the one field we care about. */
export interface WsipFlagsResponse {
    conversational_debug_enabled?: unknown;
}

/**
 * True only when the tenant's flag resolves to the boolean `true`.
 * Fail-closed: false while loading, on any error, or on a non-boolean value.
 */
export function useConversationalDebugFlag(
    apiUrl: string | null,
    accessToken: string | null,
): boolean {
    const [enabled, setEnabled] = useState(false);

    useEffect(() => {
        let cancelled = false;
        if (!accessToken || !apiUrl) return;
        (async () => {
            try {
                const resp = await fetch(
                    `${apiUrl}/wsip/flags`,
                    { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
                );
                if (!resp.ok) throw new Error("flags unavailable");
                const body = await resp.json();
                const value = body ? body.conversational_debug_enabled : undefined;
                if (!cancelled) setEnabled(value === true);
            } catch {
                if (!cancelled) setEnabled(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [apiUrl, accessToken]);

    return enabled;
}

export default useConversationalDebugFlag;
