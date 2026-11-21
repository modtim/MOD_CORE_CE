/**
 * LDF-3 §5 — Per-user namespacing for the WorkflowBuilder Build Queue's
 * localStorage. The original storage key was global; on a shared browser
 * the next user inherited the previous user's queue. This helper derives
 * a stable per-user suffix from the access token's JWT `sub` claim
 * without an API roundtrip.
 *
 * Returns `"anon"` for unauthenticated callers / malformed tokens so the
 * caller never has to handle null. Output is capped at 64 chars to keep
 * keys reasonable.
 */
export function deriveBuildQueueUserId(accessToken: string | null | undefined): string {
  if (!accessToken) return "anon";
  const parts = accessToken.split(".");
  if (parts.length !== 3) return "anon";
  try {
    // base64url → base64. JWT payloads strip `=` padding, but atob()
    // requires the encoded string length to be a multiple of 4. Without
    // restoring the padding atob fails for many real-world JWTs and
    // we silently fall through to "anon" — which collapses every user
    // onto the same storage key (Tim's #10 regression: User A and B on
    // the same browser saw each other's Build Queue).
    let padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4 !== 0) padded += "=";
    const json = typeof atob === "function"
      ? atob(padded)
      : Buffer.from(padded, "base64").toString("utf8");
    const payload = JSON.parse(json) as Record<string, unknown>;
    const candidate = payload.sub ?? payload.user_id ?? payload.email;
    if (candidate === undefined || candidate === null) return "anon";
    return String(candidate).slice(0, 64);
  } catch {
    return "anon";
  }
}

export function buildQueueStorageKey(userId: string): string {
  return `mod.buildQueue.v1.user.${userId}`;
}

export const LEGACY_GLOBAL_BUILDS_KEY = "mod.buildQueue.v1";
export const OLD_SINGLE_BUILD_KEY = "mod.activeBuild.v1";
