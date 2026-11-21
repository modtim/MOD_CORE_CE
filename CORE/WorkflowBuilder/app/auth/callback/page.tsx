"use client";

// /auth/callback — landing page for the silent SSO round-trip (GSEC R-077).
//
// Keycloak redirects here with either:
//   ?code=...&state=...        → prompt=none succeeded (an SSO session exists).
//                                Exchange the code for tokens via the BFF,
//                                store them in sessionStorage, go to the app.
//   ?error=login_required      → no SSO session (user not logged in). A
//                                stale login on a `/?workflow=<id>` link goes
//                                back to that link with the access-denied
//                                screen; otherwise fall back to the normal
//                                Dashboard login (the link is remembered).
//   ?error=<other>             → unexpected; surface it, then Dashboard login.
//
// The PKCE verifier was stashed in sessionStorage by startSilentSso() before
// the redirect (same tab), so it is still available here.

import { useEffect, useRef } from "react";
import { apiUrl, dashboardUrl } from "@/lib/runtime-config";
import {
  SSO_CLIENT_ID,
  callbackUri,
  consumePkceVerifier,
  consumeReturnTo,
  consumeSsoContext,
  markSsoAttempted,
} from "@/lib/silent-sso";
import {
  saveWorkflowTokens,
  parseWorkflowIdParam,
  decideColdLinkOutcome,
  readActiveIdentityCookie,
  stashPendingWorkflowId,
} from "@/lib/api-client";

export default function SsoCallbackPage() {
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const params = new URLSearchParams(window.location.search);
    // Always strip the token-bearing query so a refresh never re-exchanges
    // a one-use code.
    const cleanup = () =>
      window.history.replaceState({}, document.title, window.location.pathname);

    const code = params.get("code");
    const error = params.get("error");
    const desc = params.get("error_description") || error || "";

    // The stashed return-to is only ever mined for a bare workflow id: the
    // stored string itself never reaches a redirect target (COMMON MUST 3).
    const pendingIdFromReturnTo = (returnTo: string | null): number | null => {
      if (!returnTo) return null;
      try {
        return parseWorkflowIdParam(new URL(returnTo, window.location.origin).search);
      } catch {
        return null;
      }
    };

    // No SSO session — the silent attempt failed. A stale login on a deep
    // link stays on the WB origin (the access-denied screen renders there);
    // a never-logged-in visitor still goes to the Dashboard, link remembered.
    if (error || !code) {
      console.warn("WB silent SSO fell back to login:", desc);
      const ctx = consumeSsoContext();
      const pendingId = pendingIdFromReturnTo(ctx.returnTo);
      const outcome = decideColdLinkOutcome({
        pendingWorkflowId: pendingId,
        priorLogin: ctx.priorLogin,
        identityCookie: readActiveIdentityCookie(),
      });
      cleanup();
      if (outcome === "denied" && pendingId !== null) {
        // Re-open the SAME link, rebuilt from the validated integer only, and
        // arm the loop guard so the next WB load must not restart silent SSO.
        markSsoAttempted(ctx.priorLogin);
        window.location.replace(window.location.origin + "/?workflow=" + pendingId);
        return;
      }
      if (pendingId !== null) stashPendingWorkflowId(pendingId);
      window.location.href = dashboardUrl();
      return;
    }

    const verifier = consumePkceVerifier();
    (async () => {
      try {
        const resp = await fetch(`${apiUrl()}/auth/exchange-code`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            code,
            redirect_uri: callbackUri(),
            code_verifier: verifier,
            client_id: SSO_CLIENT_ID,
          }),
        });
        if (!resp.ok) throw new Error(`exchange-code HTTP ${resp.status}`);
        const data = await resp.json();
        if (!data.access_token) throw new Error("exchange-code: no access_token");
        saveWorkflowTokens(data.access_token, data.refresh_token || null);
        cleanup();
        const returnTo = consumeReturnTo();
        window.location.href = returnTo || window.location.origin + "/";
      } catch (e) {
        console.error("WB silent SSO code exchange failed:", e);
        cleanup();
        // A failed exchange is a server/PKCE fault, NOT a stale login: never
        // show the denied screen, just remember the link for the login round
        // trip.
        const ctx = consumeSsoContext();
        const pendingId = pendingIdFromReturnTo(ctx.returnTo);
        if (pendingId !== null) stashPendingWorkflowId(pendingId);
        window.location.href = dashboardUrl();
      }
    })();
  }, []);

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 text-slate-200">
      <div className="text-center">
        <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-slate-600 border-t-sky-400" />
        <p className="text-sm text-slate-400">Signing you in…</p>
      </div>
    </main>
  );
}
