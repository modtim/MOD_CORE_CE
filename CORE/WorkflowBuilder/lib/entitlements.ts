// GT.D11b — WorkflowBuilder half of the tier-aware UI.
//
// Data layer over the /platform/features `entitlements` block. The server
// stays authoritative: every gated action still 402s when the capability is
// absent. These helpers are a CLIENT-SIDE convenience that only gate
// (disable + badge) when a capability is ABSENT from the block. If the block
// is missing or unreadable we fail OPEN (treat everything as unlocked) so a
// missing feature never locks out a paid deployment.
//
// The block is sourced from the same coalesced 30s /platform/features cache
// (lib/platform-features.ts) the credits UI already uses, so this adds no
// extra request on page load.
//
// Capability names are frozen in CORE/API/utils/entitlements.py `class Cap`
// (e.g. "stack_wizard", "marketplace_publish").

import { getPlatformFeatures } from "@/lib/platform-features";

// The shape of the /platform/features `entitlements` block. Mirrors the
// API's entitlements resolver output.
export interface EntitlementsBlock {
  edition?: "ce" | "pro" | "saas" | (string & {});
  tier_name?: string;
  source?: string;
  capabilities?: string[];
  limits?: Record<string, number | "unlimited">;
  /** Present in the failure shape `{ edition, error: "unresolved" }`. */
  error?: string;
  /**
   * GT.D11.s1 — the GY.C0 declared install environment + what it needs.
   * `{}` when the block could not resolve it (never a 500). On the CE carve
   * the map module is stripped, so `needs` is [] and only the declared
   * values are carried — the client never invents a needs list.
   */
  compliance?: {
    declared_environment?: string[];
    source?: string;
    edition?: string;
    map_version?: number;
    needs?: EnvironmentNeed[];
  };
}

/** One control the declared environment needs (GY.C0 CONTROL_CATALOG row). */
export interface EnvironmentNeed {
  id: string;
  title: string;
  description?: string;
  /** Which paid feature provides it ("none" = not shipped in any edition). */
  feature?: string;
  available_on?: "paid" | "ce" | "none" | (string & {});
  /** Availability for THIS install's edition. */
  available_on_this_edition?: boolean;
}

/**
 * Pull the `entitlements` block out of a /platform/features payload. Returns
 * null when the block is absent or unreadable (which callers treat as
 * "unresolved" → fail open). Tolerates the legacy error shape
 * `{ edition, error: "unresolved" }` (no capabilities array → unresolved).
 */
export function extractEntitlements(features: unknown): EntitlementsBlock | null {
  if (!features || typeof features !== "object") return null;
  const block = (features as { entitlements?: unknown }).entitlements;
  if (!block || typeof block !== "object") return null;
  const b = block as EntitlementsBlock;
  // The error shape `{ edition, error }` has no capabilities array — treat
  // the whole block as unresolved (fail open), not as a real "zero caps"
  // grant, so a transient resolver failure never locks paid features.
  if (b.error === "unresolved" && !Array.isArray(b.capabilities)) return null;
  return b;
}

/**
 * True when `cap` is present in the block. Fail-open: an unresolved/absent
 * block (or one with no capabilities array) is treated as granted.
 */
export function hasCapability(block: EntitlementsBlock | null, cap: string): boolean {
  if (!block || !Array.isArray(block.capabilities)) return true;
  return block.capabilities.includes(cap);
}

/**
 * The value of `limits[name]`, or null when the block is unresolved or the
 * limit is unknown. Returns an int or "unlimited".
 */
export function getLimit(
  block: EntitlementsBlock | null,
  name: string,
): number | "unlimited" | null {
  if (!block || !block.limits || !(name in block.limits)) return null;
  return block.limits[name];
}

/**
 * GT.D11.s1 — the GY.C0 declared install environment + what it needs.
 * Returns null when the block is unresolved or carries no `compliance`
 * sub-block (callers render nothing rather than an empty claim). Never
 * invents a needs list: on the CE carve the API returns the declared
 * values with `needs: []`, which passes through here as-is.
 */
export function getDeclaredEnvironment(
  block: EntitlementsBlock | null,
): { declared: string[]; needs: EnvironmentNeed[] } | null {
  if (!block || typeof block.compliance !== "object") return null;
  const c = block.compliance;
  return {
    declared: Array.isArray(c.declared_environment)
      ? c.declared_environment.filter((e) => typeof e === "string")
      : [],
    needs: Array.isArray(c.needs)
      ? c.needs.filter((n) => n && typeof n === "object" && typeof n.id === "string")
      : [],
  };
}

/**
 * Load the entitlements block, reusing the shared /platform/features cache.
 * Never throws: any fetch failure resolves to null (fail open).
 */
export async function loadEntitlements(): Promise<EntitlementsBlock | null> {
  try {
    const feats = await getPlatformFeatures();
    return extractEntitlements(feats);
  } catch {
    return null; // offline / 401 / HTTP error — fail open
  }
}
