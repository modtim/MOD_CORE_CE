/**
 * GG.UI.6 — "Needs <item>" CTA panel shown where a run failed because the
 * tenant doesn't own a paid node/workflow (402 paywall / 403 marketplace
 * guard, see lib/blocked-run.ts for the exact API messages).
 *
 * Renders one row per blocked item with:
 *   [Buy]  → /store/{kind}/{id} (listing page) — always
 *   [Try]  → same listing page — ONLY when the reason says trial available
 *   "Ask your admin" — plain text (no mailto), hidden for platform admins
 */

import React from "react";
import { blockedRunStoreHref, type BlockedRunReason } from "@/lib/blocked-run";

interface BlockedRunPanelProps {
  /** Parsed entitlement-refusal reasons (empty → nothing rendered). */
  reasons: BlockedRunReason[];
  /** True for platform admins — the "ask your admin" line is pointless for
   *  someone who can just grant the entitlement themselves. */
  isAdmin?: boolean;
}

export default function BlockedRunPanel({ reasons, isAdmin = false }: BlockedRunPanelProps) {
  if (!reasons || reasons.length === 0) return null;
  return (
    <div
      data-testid="blocked-run-panel"
      className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded-lg"
    >
      <div className="text-sm font-semibold text-amber-900 mb-2">
        This run needs the items below to run:
      </div>
      <div className="space-y-2">
        {reasons.map((reason, i) => {
          const href = blockedRunStoreHref(reason);
          return (
            <div
              key={`${reason.code}-${reason.label}-${i}`}
              data-testid="blocked-run-item"
              className="flex flex-wrap items-center gap-2 text-sm border border-amber-200 bg-white rounded px-2 py-1.5"
            >
              <span className="text-amber-900">Needs {reason.label}:</span>
              <a
                data-testid="blocked-run-buy" style={{ display: "none" }}
                href={href}
                className="inline-flex items-center rounded bg-primary px-2 py-0.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
              >
                Buy
              </a>
              {false && reason.trialAvailable && (
                <a
                  data-testid="blocked-run-try"
                  href={href}
                  className="inline-flex items-center rounded border border-primary px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
                >
                  Try
                </a>
              )}
              {!isAdmin && (
                <span data-testid="blocked-run-ask-admin" className="text-xs text-amber-800">
                  Ask your admin
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
