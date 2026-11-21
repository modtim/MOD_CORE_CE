"use client";

/**
 * Q41 (Q9 follow-up, owner 11-05): the Workflow Builder's access-denied screen
 * for a user who has NO valid login — a saved login went stale and they
 * opened a `/?workflow=<id>` link cold.
 *
 * "Request access" cannot work for such a user: requestWorkflowAccess
 * (hooks/useWorkflowApi.ts) returns early and yields false when there is no
 * token, so the button does nothing. The dialog therefore offers "Sign in
 * again" instead, which starts the SAME login redirect the Q9 cold-link flow
 * uses — startSilentSso (lib/silent-sso.ts), the exact call initializeAuth
 * makes at hooks/useWorkflowApi.ts:563 — with the original workflow link as
 * the return target so the user lands back on `/?workflow=<id>` after signing
 * in (Q9 passes the return target as startSilentSso's first argument, stored
 * under SSO_RETURN_TO_KEY and consumed by /auth/callback).
 *
 * Security (COMMON MUST 3 / MUST 1):
 *  - The return target is NEVER a stored or user-supplied string put into a
 *    navigation. It is REBUILT from the integer that passes the strict
 *    `parseWorkflowIdParam` validation (lib/api-client.ts) — the SAME
 *    validator the Q9 cold-link flow applies to every redirect target — and
 *    is always same-origin (`/?workflow=<id>` on this origin). A candidate
 *    that names any origin (absolute URL of any scheme, or protocol-relative
 *    `//host`) is refused outright.
 *  - No token, refresh token, PKCE code/verifier or `state` value is read,
 *    logged or placed in a URL here; nothing is logged at all.
 */

import { parseWorkflowIdParam, stashPendingWorkflowId } from "@/lib/api-client";
import { getDashboardUrl } from "@/lib/urls";
import { startSilentSso } from "@/lib/silent-sso";

/**
 * Validate a candidate return target and rebuild it from the parsed integer.
 * Returns the same-origin target `/?workflow=<id>`, or null when the
 * candidate must be refused (foreign origin, junk id, no id).
 */
export function validatedWorkflowReturnTarget(candidate: string | null): string | null {
  if (candidate === null) return null;
  // Same-origin only: an absolute URL (any scheme) or a protocol-relative
  // "//host/..." could name another origin -> refused before anything is read.
  if (/^[a-z][a-z0-9+.-]*:/i.test(candidate) || candidate.startsWith("//")) return null;
  const q = candidate.indexOf("?");
  const search = q === -1 ? "" : candidate.slice(q);
  const id = parseWorkflowIdParam(search);
  if (id === null) return null;
  // Rebuilt from the validated integer only — never the candidate string.
  return `/?workflow=${id}`;
}

/**
 * Start the login round trip for the denied screen's "Sign in again" button.
 * Returns true when a redirect was initiated, false when the target was
 * refused (nothing navigated).
 */
export async function startSignInAgain(workflowId: number | null): Promise<boolean> {
  const returnTo = validatedWorkflowReturnTarget(
    workflowId === null ? null : `/?workflow=${workflowId}`,
  );
  if (returnTo === null) return false;

  // The exact login redirect the Q9 cold-link flow uses. `priorLogin: true`
  // because this screen only appears for a user whose login went stale — the
  // flag is what the callback's loop guard feeds back into decideColdLinkOutcome,
  // so a second failed attempt shows the denied screen again instead of looping.
  const started = await startSilentSso(returnTo, { priorLogin: true });
  if (started) return true;

  // Silent SSO could not be built (no discovery endpoint / Keycloak
  // unreachable): the same fallback the Q9 cold-link login path uses —
  // remember the id (bare integer in sessionStorage) and send the user to the
  // Dashboard to log in, so the handoff returns to `/?workflow=<id>`.
  const id = parseWorkflowIdParam(returnTo.slice(returnTo.indexOf("?")));
  if (id !== null) stashPendingWorkflowId(id);
  window.location.href = getDashboardUrl();
  return true;
}
