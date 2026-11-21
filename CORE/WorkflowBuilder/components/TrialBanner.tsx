"use client";
import { apiUrl as resolveApiUrl } from "@/lib/runtime-config";

// GG.UI.9 — trial countdown banner for the builder layout.
//
// Fetches GET /library once on mount (NO interval — the no-backoff poll
// storm that froze the WB was a self-DDoS lesson, see lib/poller.ts) and
// shows a dismissible banner for any active trial entitlement expiring
// within 3 days. Dismissal persists in localStorage per item keyed by the
// item's expires_at, so if a trial is renewed a stale dismissal never
// hides the new window.

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import { ACCESS_TOKEN_KEY, TOKEN_REFRESHED_EVENT } from "@/lib/api-client";

const DISMISS_KEY = "mod_trial_banner_dismissed";
const WINDOW_MS = 3 * 24 * 60 * 60 * 1000;

interface LibraryItem {
  item_id: string;
  name: string;
  kind: string;
  source: string;
  active: boolean;
  expires_at: string | null;
}

interface TrialBannerProps {
  /** Test overrides — production callers omit both and get the env URL
   *  plus the stored Keycloak access token. */
  apiUrl?: string;
  accessToken?: string | null;
  /** FIX-CE-TRIALS-OFF (owner 11-15 ~8:15 PM CT popup answer "Hide in paid
   *  too"): trials are OFF in the launch build for EVERYONE (CE AND paid).
   *  The banner renders ONLY when a caller opts in; the production mount
   *  omits this, so no countdown banner shows in any edition. Run-time
   *  hide only: ids, the dismiss key, the /library fetch and the committed
   *  trial fix 623c6b12db stay untouched; nothing is deleted. */
  enabled?: boolean;
}
function readDismissed(): Record<string, string> {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function persistDismissed(map: Record<string, string>): void {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(map));
  } catch {
    // localStorage full/blocked — banner simply won't persist.
  }
}

function daysLeft(expiresAt: string, now: number): number {
  return Math.max(0, Math.ceil((new Date(expiresAt).getTime() - now) / (24 * 60 * 60 * 1000)));
}

export function TrialBanner({ apiUrl = resolveApiUrl(), accessToken, enabled = false }: TrialBannerProps = {}) {
  // FIX-CE-TRIALS-OFF: trials are OFF in the launch build for EVERYONE (CE AND paid).
  // The production mount omits `enabled`, so this renders nothing in any edition; the
  // component contract (fetch-once, countdown, dismissal) is unchanged and its test
  // opts in with enabled: true. Run-time hide only - no ids/routes deleted.
  if (!enabled) return null;
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [fetched, setFetched] = useState(false);
  const [dismissed, setDismissed] = useState<Record<string, string>>({});
  // Bump on every token arrival: prop change, TOKEN_REFRESHED_EVENT, or poll hit.
  const [tokenTick, setTokenTick] = useState(0);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (hasFetched.current) return;
    const token = accessToken ?? (typeof window !== "undefined"
      ? sessionStorage.getItem(ACCESS_TOKEN_KEY)
      : null);
    if (!token) return;

    hasFetched.current = true;
    setDismissed(readDismissed());
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`${apiUrl}/library`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const list: LibraryItem[] = Array.isArray(data) ? data : (data?.items || []);
        if (!cancelled) setItems(list);
      } catch {
        // Background nicety — never block the builder on this.
      } finally {
        if (!cancelled) setFetched(true);
      }
    })();
    return () => { cancelled = true; };
  }, [apiUrl, accessToken, tokenTick]);

  // Token-source listeners: (A) prop change already covers the common path.
  // (B) TOKEN_REFRESHED_EVENT fires when a background poll silently refreshes.
  //     saveWorkflowTokens on initial mount does NOT dispatch it, so we also
  //     need the poll below for the initial mount case.
  useEffect(() => {
    const handler = () => { setTokenTick((t) => t + 1); };
    window.addEventListener(TOKEN_REFRESHED_EVENT, handler);
    return () => window.removeEventListener(TOKEN_REFRESHED_EVENT, handler);
  }, []);

  // (C) Bounded polling — checks every 500 ms for up to ~15 s until a token
  //     appears or the component unmounts. Fires once and tears itself down.
  useEffect(() => {
    if (hasFetched.current || accessToken) return;
    let tick = 0;
    const limit = 30; // 30 * 500ms ≈ 15 s
    const id = setInterval(() => {
      if (hasFetched.current) { clearInterval(id); return; }
      tick++;
      if (tick >= limit) { clearInterval(id); return; }
      const token = typeof window !== "undefined"
        ? sessionStorage.getItem(ACCESS_TOKEN_KEY)
        : null;
      if (token) { clearInterval(id); setTokenTick((t) => t + 1); }
    }, 500);
    return () => clearInterval(id);
  }, [accessToken]);

  const now = useMemo(() => Date.now(), [fetched]);
  const expiring = useMemo(
    () =>
      items.filter((it) => {
        if (it.source !== "trial" || it.active === false || !it.expires_at) return false;
        const exp = it.expires_at;
        if (!exp) return false;
        const t = new Date(exp).getTime();
        if (isNaN(t) || t <= now) return false;
        if (t - now > WINDOW_MS) return false;
        return dismissed[it.item_id] !== exp;
      }),
    [items, now, dismissed],
  );

  const dismiss = useCallback(
    (item: LibraryItem) => {
      setDismissed((prev) => {
        const next = { ...prev, [item.item_id]: item.expires_at as string };
        persistDismissed(next);
        return next;
      });
    },
    [],
  );

  if (!fetched || expiring.length === 0) return null;

  return (
    <div
      role="status"
      data-testid="trial-banner"
      className="flex flex-col gap-1 border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900"
    >
      {expiring.map((it) => {
        const exp = it.expires_at;
        if (!exp) return null;
        const d = daysLeft(exp, now);
        const when = d <= 1 ? "ends today" : `ends in ${d} days`;
        return (
          <div key={it.item_id} className="flex items-center gap-2">
            <span>
              Your access to <strong>{it.name}</strong> {when}
            </span>
            <a
              data-testid={`trial-banner-buy-${it.item_id}`}
              href={`/store/${encodeURIComponent(it.kind || "")}/${encodeURIComponent(it.item_id)}`}
              className="font-medium text-amber-900 underline hover:no-underline"
            >
              Buy
            </a>
            <button
              type="button"
              data-testid={`trial-banner-dismiss-${it.item_id}`}
              aria-label="Dismiss"
              onClick={() => dismiss(it)}
              className="ml-auto rounded p-0.5 hover:bg-amber-100"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

export default TrialBanner;
