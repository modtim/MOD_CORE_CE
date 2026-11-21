"use client";

// GT.D11b — <PaidLock> / <LockedBadge>, the tier-aware gating primitives.
//
// PaidLock wraps a control. While the entitlements block is unresolved it
// renders the child untouched (fail-open). Once resolved, if `cap` is ABSENT
// from the block it renders the child inside a disabled, aria-disabled
// wrapper and appends a LockedBadge. The server's 402 handling stays as the
// authoritative gate — this is purely a client-side disable+badge so a CE
// tenant doesn't click into a 402.
//
// LockedBadge is the standalone "Not part of this edition" chip with an upgrade
// link. Both reuse the Dashboard's upgrade convention: the tenant-facing
// billing page on the dashboard host (Dashboard/js/entitlements.js
// UPGRADE_HASH = 'billing' → here `${getDashboardUrl()}/#billing`).

import React from "react";
import { useEntitlements } from "@/hooks/use-entitlements";
import { getDashboardUrl } from "@/lib/urls";

export function upgradeUrl(): string {
  return `${getDashboardUrl()}/#billing`;
}

export interface LockedBadgeProps {
  /** Optional prefix, e.g. "Stack Wizard". Renders "Stack Wizard — Not part of this edition". */
  label?: string;
}

export function LockedBadge({ label }: LockedBadgeProps): React.ReactElement {
  return (
    <span className="inline-flex items-center gap-1 ml-2 text-[10px] font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5 whitespace-nowrap">
      <span aria-hidden="true">🔒</span>
      <span>{label ? `${label} — ` : ""}Not part of this edition</span>
    </span>
  );
}

export interface PaidLockProps {
  /** Capability name from CORE/API/utils/entitlements.py `class Cap`. */
  cap: string;
  /** Human name of the feature, shown in the badge/tooltip. */
  label?: string;
  /** The control to gate. Rendered disabled when the cap is absent. */
  children: React.ReactNode;
  /** Extra classes for the wrapper span (e.g. "w-full" inside full-width menus). */
  className?: string;
}

export function PaidLock({ cap, label, children, className }: PaidLockProps): React.ReactElement {
  const { has, loading } = useEntitlements();

  // Fail-open: while the block is still loading, render the child exactly
  // as supplied. (has() itself is fail-open, so a resolved block without
  // the capability is the only locked path.)
  if (loading) return <>{children}</>;
  if (has(cap)) return <>{children}</>;

  return (
    <span
      className={`inline-flex items-center${className ? ` ${className}` : ""}`}
      data-mod-locked="1"
      aria-disabled="true"
      title={`${label ?? cap} — not part of this edition`}
      onClick={(e) => e.preventDefault()}
      onPointerDown={(e) => e.preventDefault()}
    >
      <span
        className="inline-flex items-center opacity-50 pointer-events-none select-none"
        inert
      >
        {children}
      </span>
      <LockedBadge label={label} />
    </span>
  );
}

export default PaidLock;
