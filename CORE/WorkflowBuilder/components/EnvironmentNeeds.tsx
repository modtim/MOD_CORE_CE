"use client";

// GT.D11.s2 — the GY.C0 declared-environment "needs" readout.
//
// CE shows the declared install environment + what it needs, with the
// upgrade link, instead of letting users click into a 402. Sourced from
// the /platform/features `entitlements.compliance` sub-block (GT.D11.s1,
// main.py `_compliance_needs_block`), which mirrors
// services/compliance_environments.py `declared_environment_status()`.
//
// Honest framing (owner directive 2026-10-01): declaring an environment
// records intent + lists the controls it needs. MOD enables the controls
// it provides; it never certifies or claims compliance. On the CE carve
// the map module is stripped, so the API carries the declared values with
// `needs: []` — we render the declaration and say the needs map is a paid
// feature; we NEVER invent a needs list client-side.
//
// The server stays authoritative: nothing here enforces anything.

import React from "react";
import { useEntitlements } from "@/hooks/use-entitlements";
import { upgradeUrl } from "@/components/PaidLock";
import type { EnvironmentNeed } from "@/hooks/use-entitlements";

export interface EnvironmentNeedsProps {
  /** Optional heading override (default "Declared environment"). */
  heading?: string;
}

/** "fedramp_high_il5" -> "FedRAMP High Il5"-ish plain label. */
function envLabel(env: string): string {
  return env.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function EnvironmentNeeds({ heading }: EnvironmentNeedsProps): React.ReactElement | null {
  const { declared } = useEntitlements();
  const info = declared();
  // No compliance sub-block (unresolved payload) -> render nothing at all.
  if (info === null) return null;
  const envs = info.declared;
  const needs = info.needs;

  const chip = (n: EnvironmentNeed) =>
    n.available_on_this_edition
      ? "bg-emerald-50 text-emerald-800 border border-emerald-200"
      : "bg-amber-50 text-amber-700 border border-amber-200";

  return (
    <div className="rounded-lg border border-gray-200 px-3 py-2 bg-gray-50">
      <p className="text-[10px] font-semibold text-gray-700 mb-1">
        {heading ?? "Declared environment"}
      </p>
      {envs.length > 0 ? (
        <p className="text-[10px] text-gray-700 mb-1">
          {envs.map(envLabel).join(" · ")}
        </p>
      ) : (
        <p className="text-[10px] text-gray-500 italic mb-1">
          None declared — declaring one lists the controls it needs. Declaring
          is never required.
        </p>
      )}
      {needs.length > 0 ? (
        <ul className="list-none text-[10px]">
          {needs.map((n) => (
            <li
              key={n.id}
              className={`inline-flex items-center rounded px-1 py-0.5 ${chip(n)}`}
              title={n.available_on_this_edition
                ? `${n.title} — provided on this edition`
                : `${n.title} — not part of this edition`}
            >
              {n.title}
            </li>
          ))}
        </ul>
      ) : envs.length > 0 ? (
        // Declared but no map here (CE carve): say so, never fabricate.
        <p className="text-[10px] text-gray-500">
          The control map for this environment is not part of this edition.
        </p>
      ) : null}
      <p className="text-[10px] text-gray-600 mt-1">
        MOD enables these controls; it never certifies compliance.
      </p>
    </div>
  );
}

export default EnvironmentNeeds;
