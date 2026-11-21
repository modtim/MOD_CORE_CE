"use client";

/**
 * Per-workflow burst eligibility — burst-to-saas §3.12.
 *
 * Consent lives where the workflow is EDITED, because that is where someone
 * knows what data flows through it. A tenant-wide switch cannot carry that
 * judgement, which is why the tenant kill switch and this control are separate
 * and why this one defaults off.
 *
 * Renders nothing when the install is not paired with MOD. A spill toggle on
 * an install with nowhere to spill is a control that cannot do anything, and
 * offering it invites someone to believe they have turned something on.
 *
 * Shaped as a menu item so it can sit inside the toolbar's Actions dropdown
 * alongside Load / Edit / Export / Share.
 */

import { useEffect, useState } from "react";
import { Cloud, CheckSquare, Square, Loader2 } from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import { useEntitlements } from "@/hooks/use-entitlements";

interface BurstEligibilityItemProps {
  workflowId: number;
  apiUrl: string;
  /** Fired after a successful change so the parent can toast or refresh. */
  onChanged?: (eligible: boolean) => void;
}

export default function BurstEligibilityItem({
  workflowId,
  apiUrl,
  onChanged,
}: BurstEligibilityItemProps) {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [eligible, setEligible] = useState<boolean>(false);
  const [enabledAt, setEnabledAt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 2400UI122e — burst is PAID (open-core/manifest.yaml:1212): CE ships no
  // /burst/* API, so this badge must not even fetch there. FAIL-OPEN: gated
  // ONLY on a positively-resolved edition "ce" (unresolved/errored → edition
  // null → paid behaviour untouched). Same mechanism as assistant-launcher.tsx:44-49
  // and the bounded gate watcher in data-marking-banner.tsx:83,126-131.
  const { edition, loading } = useEntitlements();
  const ceEdition = edition === "ce";
  const [gateOpen, setGateOpen] = useState(false);

  useEffect(() => {
    if (ceEdition) return;
    if (!loading) setGateOpen(true);
    // Bounded 5s fallback mirrors data-marking-banner.tsx:128 (window.fetch has
    // no timeout, so a hung /platform/features must not leave PAID without its
    // reads).
    const fallback = setTimeout(() => setGateOpen(true), 5000);
    return () => clearTimeout(fallback);
  }, [loading, ceEdition]);

  // Both reads on mount. The component mounts when the Actions menu opens, so
  // this is one round trip per menu open rather than one per page load.
  useEffect(() => {
    let cancelled = false;

    // CE: no fetch at all (see the gate note above). `ceEdition` and `gateOpen`
    // are deps so the effect re-runs the moment the block resolves — on CE it
    // bails and clears any state a paid install's earlier read left behind.
    if (ceEdition || !gateOpen) return;

    (async () => {
      try {
        const [settingsResp, workflowResp] = await Promise.all([
          authenticatedFetch(`${apiUrl}/burst/connect/settings`),
          authenticatedFetch(`${apiUrl}/road/${workflowId}`),
        ]);
        if (cancelled) return;

        // A tenant that has never configured burst gets a lazily-created row
        // with connected=false, so a 200 here does not imply a pairing.
        const isConnected =
          settingsResp?.ok === true &&
          Boolean((await settingsResp.json())?.connected);
        if (cancelled) return;
        setConnected(isConnected);
        if (!isConnected) return;

        if (workflowResp?.ok) {
          const wf = await workflowResp.json();
          if (cancelled) return;
          setEligible(Boolean(wf?.burst_eligible));
          setEnabledAt(wf?.burst_enabled_at ?? null);
        }
      } catch {
        // Treat an unreadable state as not connected. Failing closed keeps a
        // control that governs data leaving the premises from appearing when
        // we cannot say what it currently is.
        if (!cancelled) setConnected(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, workflowId, ceEdition, gateOpen]);

  async function toggle() {
    if (saving) return;
    const next = !eligible;
    setSaving(true);
    setError(null);
    // Optimistic: the menu stays open on toggle (unlike its siblings, which
    // navigate away), so a revert is visible where it happens.
    setEligible(next);
    try {
      const resp = await authenticatedFetch(
        `${apiUrl}/burst/connect/workflows/${workflowId}/eligibility`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ eligible: next }),
        },
      );
      if (!resp?.ok) throw new Error(String(resp?.status ?? "failed"));
      const body = await resp.json();
      setEligible(Boolean(body?.burst_eligible));
      setEnabledAt(body?.enabled_at ?? null);
      onChanged?.(Boolean(body?.burst_eligible));
    } catch {
      setEligible(!next);
      setError("Could not save. Nothing changed.");
    } finally {
      setSaving(false);
    }
  }

  if (connected !== true) return null;

  const Box = eligible ? CheckSquare : Square;
  const stateLine = eligible
    ? enabledAt
      ? `on · enabled ${new Date(enabledAt).toLocaleDateString()}`
      : "on · may run on MOD when the queue backs up"
    : "off · never sent off-premises";

  return (
    <>
      <div className="border-t my-1" />
      <button
        onClick={toggle}
        disabled={saving}
        aria-pressed={eligible}
        className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 disabled:cursor-not-allowed flex items-start"
        title="Allow this workflow's runs to spill to MOD when the local queue backs up"
      >
        {saving ? (
          <Loader2 className="h-4 w-4 mr-2 mt-0.5 shrink-0 animate-spin" />
        ) : (
          <Box className="h-4 w-4 mr-2 mt-0.5 shrink-0" />
        )}
        <span className="flex flex-col">
          <span className="flex items-center">
            <Cloud className="h-3.5 w-3.5 mr-1.5 shrink-0" />
            Allow this workflow to burst
          </span>
          <span className={error ? "text-xs text-red-600" : "text-xs text-gray-500"}>
            {error ?? stateLine}
          </span>
        </span>
      </button>
    </>
  );
}
