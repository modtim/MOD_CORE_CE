"use client";

/**
 * sz-tcp.6 — cost-basis badge for the tiered-credit wizard.
 *
 * The wizard's tier cards carry a `cost_basis` on the server
 * (`node_authoring_service.py`): 'estimated' (AI-suggested, may be
 * wrong), 'calibrated' (measured against a real run), 'manual' (the
 * author overrode the tiers) and 'free' (zero-cost node). This chip
 * colour-codes that basis so an author can see, at a glance, whether
 * the numbers they are looking at are machine guesses or their own
 * decisions: estimated = amber, calibrated = emerald, manual = blue,
 * free = gray.
 *
 * Unknown/absent bases render nothing — old nodes have no cost_basis
 * and the wizard must not invent one.
 */
import React from "react";

export type CostBasis = "estimated" | "calibrated" | "manual" | "free";

interface CostBasisStyle {
  /** Human label shown inside the chip. */
  label: string;
  /** Tailwind classes (amber / emerald / blue / gray families). */
  chip: string;
}

const BASE_STYLES: Record<CostBasis, CostBasisStyle> = {
  // AI guess — the number could move after a real run.
  estimated: { label: "Estimated", chip: "border-amber-500 text-amber-700 bg-amber-50" },
  // Measured against a completed run — trustworthy.
  calibrated: { label: "Calibrated", chip: "border-emerald-600 text-emerald-700 bg-emerald-50" },
  // The author typed these tiers themselves.
  manual: { label: "Manual", chip: "border-blue-600 text-blue-700 bg-blue-50" },
  // Costs nothing.
  free: { label: "Free", chip: "border-slate-300 text-slate-600 bg-slate-50" },
};

/** Normalise a raw server value; null when it is not a known basis. */
export function normalizeCostBasis(value: unknown): CostBasis | null {
  if (value == null) return null;
  const key = String(value).trim().toLowerCase();
  return BASE_STYLES[key as CostBasis] ? (key as CostBasis) : null;
}

interface CostBasisBadgeProps {
  /** Raw `cost_basis` from the node payload; unknown values render nothing. */
  costBasis?: unknown;
  /** Extra Tailwind classes for the caller's layout (margins, etc.). */
  className?: string;
}

/** Small colour-coded chip: "Estimated" / "Calibrated" / "Manual" / "Free". */
export function CostBasisBadge({ costBasis, className = "" }: CostBasisBadgeProps): React.JSX.Element | null {
  const basis = normalizeCostBasis(costBasis);
  if (!basis) return null;
  const style = BASE_STYLES[basis];
  return (
    <span
      data-testid="cost-basis-badge"
      data-cost-basis={basis}
      title={`Credit cost basis: ${style.label}`}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${style.chip} ${className}`}
    >
      {style.label}
    </span>
  );
}

export default CostBasisBadge;
