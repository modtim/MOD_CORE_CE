"use client";

// GT.D11b — React hook over lib/entitlements.ts.
//
// Exposes has(cap) / limit(name) / edition / tierName / loading and is
// FAIL-OPEN: while the block is still loading, or when /platform/features
// errors or returns the `{ edition, error: "unresolved" }` shape, has()
// reports every capability as granted and loading goes false. Nothing is
// ever locked on an unknown state — the server's 402 remains the
// authoritative gate.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  EntitlementsBlock,
  getDeclaredEnvironment,
  getLimit,
  hasCapability,
  loadEntitlements,
} from "@/lib/entitlements";

export interface UseEntitlementsResult {
  /** True when the capability is granted. Fail-open: true while loading/unresolved. */
  has: (cap: string) => boolean;
  /** limit value (int | "unlimited") or null when unknown. */
  limit: (name: string) => number | "unlimited" | null;
  edition: string | null;
  tierName: string | null;
  /** True only while the first /platform/features load is in flight. */
  loading: boolean;
  /** True when the block resolved with real data (vs unresolved/fail-open). */
  resolved: boolean;
  /**
   * GT.D11.s1 — GY.C0 declared install environment + what it needs, or
   * null when the block carries no `compliance` sub-block (render nothing).
   */
  declared: () => { declared: string[]; needs: EnvironmentNeed[] } | null;
}

/** One control the declared environment needs (GY.C0 CONTROL_CATALOG row). */
export interface EnvironmentNeed {
  id: string;
  title: string;
  description?: string;
  feature?: string;
  available_on?: "paid" | "ce" | "none" | (string & {});
  available_on_this_edition?: boolean;
}

export function useEntitlements(): UseEntitlementsResult {
  const [block, setBlock] = useState<EntitlementsBlock | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    loadEntitlements().then((b) => {
      if (cancelled) return;
      setBlock(b);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const has = useCallback(
    (cap: string) => hasCapability(block, cap),
    [block],
  );
  const limit = useCallback((name: string) => getLimit(block, name), [block]);
  // GT.D11.s1 — the GY.C0 declared environment + its needs list (null when
  // the block carries no compliance sub-block → the component renders nothing).
  const declared = useCallback(() => getDeclaredEnvironment(block), [block]);

  return useMemo(
    () => ({
      has,
      limit,
      edition: block?.edition ?? null,
      tierName: block?.tier_name ?? null,
      loading,
      resolved: block !== null,
      declared,
    }),
    [has, limit, declared, block, loading],
  );
}

export default useEntitlements;
