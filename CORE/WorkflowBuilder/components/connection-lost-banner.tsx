"use client";
import { apiUrl, dashboardUrl } from "@/lib/runtime-config";

import React, { useEffect, useState, useCallback, useId } from "react";
import { AlertTriangle, RefreshCw, LogOut } from "lucide-react";
import {
  AUTH_CONNECTION_LOST_EVENT,
  AUTH_CONNECTION_RESTORED_EVENT,
  getAccessToken,
  clearWorkflowTokens,
  broadcastLogout,
} from "@/lib/api-client";

// Soft-auth banner shown in the WorkflowBuilder when authenticatedFetch
// hits a 401-after-refresh-failure on a background poll. Previously
// the WorkflowBuilder would hard-redirect to the Dashboard on any 401,
// which blew away in-progress wizard state the moment a Keycloak token
// expired mid-session — the user saw that as "random page refreshes".
//
// This banner gives the user:
//   1. An explanation that the issue is usually a connection blip.
//   2. A Retry button to re-check /me without leaving the page.
//   3. An opt-in "Go to dashboard" that actually triggers the
//      hard-logout → logged-out screen if the session truly expired.
//
// Placement: mounted once at the top of WorkflowBuilder. Self-dismisses
// when it hears AUTH_CONNECTION_RESTORED_EVENT (any successful fetch
// through authenticatedFetch emits this).

const API_URL = apiUrl();
const DASHBOARD_URL = dashboardUrl();

export function ConnectionLostBanner() {
  const [visible, setVisible] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const baseId = useId();
  const titleId = `${baseId}-title`;
  const descId = `${baseId}-desc`;

  useEffect(() => {
    const onLost = () => setVisible(true);
    const onRestored = () => setVisible(false);
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, onLost);
    window.addEventListener(AUTH_CONNECTION_RESTORED_EVENT, onRestored);
    return () => {
      window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, onLost);
      window.removeEventListener(AUTH_CONNECTION_RESTORED_EVENT, onRestored);
    };
  }, []);

  // Background auto-recovery: while the banner is up, ping /me every
  // 30s. If the API comes back without user action, dispatch RESTORED
  // and clear the banner automatically. Without this, the banner
  // latched once stayed up until the user clicked Retry — which
  // sucks when the blip was a 5-second API restart.
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const token =
          typeof window !== "undefined"
            ? getAccessToken()
            : null;
        const resp = await fetch(`${API_URL}/me`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!cancelled && resp.ok) {
          window.dispatchEvent(new CustomEvent(AUTH_CONNECTION_RESTORED_EVENT));
        }
      } catch {
        /* still offline; next tick will retry */
      }
    };
    const id = window.setInterval(tick, 30_000);
    // Also run once at ~5s so the first auto-heal lands fast after a
    // brief API restart, not waiting a full 30s.
    const initial = window.setTimeout(tick, 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      window.clearTimeout(initial);
    };
  }, [visible]);

  const handleRetry = useCallback(async () => {
    // Ping /me with whatever token we have. We use the raw `fetch`
    // here so the connection-lost/restored machinery in
    // authenticatedFetch fires naturally — a successful retry emits
    // AUTH_CONNECTION_RESTORED_EVENT which hides the banner.
    setRetrying(true);
    try {
      const token =
        typeof window !== "undefined"
          ? getAccessToken()
          : null;
      const resp = await fetch(`${API_URL}/me`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (resp.ok) {
        // Manually dispatch the restored event — /me bypassed
        // authenticatedFetch above, so we need to let the banner know.
        window.dispatchEvent(new CustomEvent(AUTH_CONNECTION_RESTORED_EVENT));
      }
    } catch {
      // Still offline; banner stays up.
    } finally {
      setRetrying(false);
    }
  }, []);

  const handleGoToDashboard = useCallback(() => {
    // Explicit user action — THIS is the path that hard-logs-out.
    // We clear the workflow tokens (sessionStorage now, R-077) and
    // broadcast the logout to sibling WB tabs so they go to the
    // logged-out state too, then bounce to the Dashboard.
    clearWorkflowTokens();
    broadcastLogout();
    window.location.href = DASHBOARD_URL;
  }, []);

  if (!visible) return null;

  return (
    <div
      role="alert"
      aria-labelledby={titleId}
      aria-describedby={descId}
      className="border-l-4 border-amber-500 bg-amber-50 text-amber-900 px-4 py-3 flex items-center gap-3 mb-2"
    >
      <AlertTriangle className="w-5 h-5 shrink-0 text-amber-600" />
      <div className="flex-1 text-sm">
        <div id={titleId} className="font-medium">Workers unavailable</div>
        <div id={descId} className="text-xs mt-0.5">
          We can&apos;t reach the MOD API right now — this is usually a
          connection issue. Your work is saved locally.
        </div>
      </div>
      <button
        onClick={handleRetry}
        disabled={retrying}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-60"
      >
        <RefreshCw className={`w-3.5 h-3.5 ${retrying ? "animate-spin" : ""}`} />
        {retrying ? "Checking…" : "Retry"}
      </button>
      <button
        onClick={handleGoToDashboard}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded border border-amber-600 text-amber-900 hover:bg-amber-100"
      >
        <LogOut className="w-3.5 h-3.5" />
        Go to dashboard
      </button>
    </div>
  );
}

export default ConnectionLostBanner;
