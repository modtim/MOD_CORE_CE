"use client";

// FX.79 p3a — persistent CUI/ITAR data-marking banner.
//
// Shown at the TOP and BOTTOM of the WorkflowBuilder view (the
// classification-banner convention) whenever the open resource — the
// workflow, or an asset/named-artifact detail view — carries a live data
// marking (GSEC FX.79, owner Q62=A). The banner text is the canonical
// stamp wording from `CORE/API/services/marking_stamps.py` (FX.79 p3b),
// mirrored in lib/data-markings.ts: worst-ranked label wins,
// `CONTROLLED: <LABEL>[/<DISSEMINATION>] — handle per policy`.
//
// Read path: GET {api}/tenant/data-markings/{resource_type}/{resource_id}
// (CORE/API/routers/tenant/data_markings.py, mounted at /v1/tenant) →
// {items: [{label, dissemination, ...}], total}.
//
// Behaviour contract:
//  - no live marking / unmarked resource / CE carve (no marking store) /
//    capability denied (403) / fetch failure → NOTHING renders and the
//    editor is never blocked (silent fallback, no console noise);
//  - the banner is NOT dismissible (a data marking is never something you
//    can opt out of looking at);
//  - role="note" + aria-label so screen readers announce the marking;
//    bg-red-900 / text-red-50 keeps the contrast ratio well above 4.5:1.

import { apiUrl as resolveApiUrl } from "@/lib/runtime-config";
import React, { useCallback, useEffect, useState } from "react";
import { getAccessToken } from "@/lib/api-client";
import { useEntitlements } from "@/hooks/use-entitlements";
import {
  markingBannerText,
  worstMarking,
  type DataMarkingRow,
  type MarkingStamp,
} from "@/lib/data-markings";

export interface DataMarkingBannerProps {
  /** One of the API's DATA_MARKING_RESOURCE_TYPES ("workflow", "asset",
   *  "node_definition", "named_artifact", "dataset"). */
  resourceType: string;
  /** The resource's id as a string/number; null/undefined → no fetch. */
  resourceId: string | number | null;
  /** "top" or "bottom" placement inside the view (both stay visible —
   *  the banner is persistent, never auto-hidden). */
  position?: "top" | "bottom";
  /** Test overrides — production callers omit both and get the runtime
   *  config URL plus the stored Keycloak access token. */
  apiUrl?: string;
  accessToken?: string | null;
  /** FX.163 p3b4b — the caller's tenant-admin / superadmin verdict. When
   *  true, every label the banner carries gets a "Clear" button (the API's
   *  DELETE route requires manage_settings, so a non-admin would only get a
   *  403). Default false: a caller that cannot know never shows the button. */
  canClear?: boolean;
}

export function DataMarkingBanner({
  resourceType,
  resourceId,
  position = "top",
  apiUrl = resolveApiUrl(),
  accessToken,
  canClear = false,
}: DataMarkingBannerProps) {
  const [stamp, setStamp] = useState<MarkingStamp | null>(null);
  // Inline status line (awaiting-approval notice or a clear failure). Text
  // only — every server-supplied string reaches the DOM as a React text
  // node, never as HTML.
  const [note, setNote] = useState<string | null>(null);

  const id = resourceId == null ? null : String(resourceId);

  // 2400UI120 — CE ships no data-markings store: services/data_markings.py and
  // its router are DENIED in open-core/manifest.yaml, so the banner must not
  // even fetch there (the 404 trips assertClean on unrelated page loads in the
  // CE e2e). SAME mechanism as the 2400UI16 assistant-panel CE fix
  // (components/assistant-panel.tsx:221-222 + its CE edition watcher at :271-291,
  // components/assistant-launcher.tsx:48-49): the entitlements block's
  // positively-resolved edition. FAIL-OPEN: hidden ONLY when edition is exactly
  // "ce"; unresolved/errored → edition null → paid behaviour (same calls, same
  // timing) is untouched. The fetch effect additionally waits for the FIRST
  // entitlements resolution (`loading` flips false) so a CE install never issues
  // a data-markings request in the null-edition window; the bounded 5s fallback
  // mirrors assistant-panel.tsx:283-289 (window.fetch has no timeout, so a hung
  // /platform/features must not leave PAID without its banner).
  const { edition, loading } = useEntitlements();
  const ceEdition = edition === "ce";
  const [gateOpen, setGateOpen] = useState(false);

  // Fetch-once per (resource, token), no polling — a marking only changes
  // when an admin sets/clears it (manage_settings), and this view already
  // re-mounts on workflow switch. Re-called after a clear so whatever the
  // server kept (the other labels, or the label a second approver has not
  // approved yet) is what the user sees.
  const load = useCallback(
    async (isCurrent: () => boolean = () => true): Promise<void> => {
      if (!id) return;
      try {
        const token = accessToken ?? getAccessToken();
        if (!token) return;
        const resp = await fetch(
          `${apiUrl}/tenant/data-markings/${encodeURIComponent(resourceType)}/${encodeURIComponent(id)}`,
          { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } },
        );
        // !ok covers: CE carve (store missing), resource type unknown
        // (404), no view_settings capability (403), cross-tenant (404).
        // All of them mean "show no banner" — not an error state.
        if (!resp.ok) return;
        const data = await resp.json();
        const rows: DataMarkingRow[] = Array.isArray(data) ? data : (data?.items ?? []);
        const worst = worstMarking(rows);
        if (isCurrent()) setStamp(worst);
      } catch {
        // Silent fallback: a fetch/parse hiccup never blocks the editor.
      }
    },
    [id, resourceType, apiUrl, accessToken],
  );

  // ---- 2400UI120 CE edition watcher --------------------------------------
  // Opens the fetch gate only once the entitlements block has resolved, so a
  // CE install never issues a data-markings request inside the null-edition
  // window. On CE the gate is never opened (and the fetch effect bails on
  // ceEdition anyway). Bounded 5s fallback mirrors assistant-panel.tsx:283-289.
  useEffect(() => {
    if (ceEdition) return;
    if (!loading) setGateOpen(true);
    const fallback = setTimeout(() => setGateOpen(true), 5000);
    return () => clearTimeout(fallback);
  }, [loading, ceEdition]);

  useEffect(() => {
    let cancelled = false;
    setStamp(null);
    setNote(null);
    // 2400UI120 — CE: no fetch at all (see the gate note above). ceEdition is a
    // dep so the effect re-runs the moment the block resolves to "ce" and
    // clears any stamp a paid install's earlier read left behind.
    if (!id || ceEdition || !gateOpen) return;
    void load(() => !cancelled);
    return () => { cancelled = true; };
  }, [load, id, ceEdition, gateOpen]);

  // FX.163 p3b4b — clear ONE label (soft delete, audit-logged), mirroring
  // CORE/Dashboard/js/data-marking-banner.js (p3b4a). Same fetch/auth helper
  // as the GET, so the /v1 prefix comes from apiUrl exactly once.
  const clearLabel = async (label: string): Promise<void> => {
    if (!id) return;
    if (!window.confirm(`Clear the data marking "${label}" on this resource?`)) return;
    const token = accessToken ?? getAccessToken();
    if (!token) {
      setNote("You are not signed in — the data marking was not cleared.");
      return;
    }
    try {
      const resp = await fetch(
        `${apiUrl}/tenant/data-markings/${encodeURIComponent(resourceType)}/${encodeURIComponent(id)}/${encodeURIComponent(label)}`,
        { method: "DELETE", headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } },
      );
      // Raw fetch hands us the Response, so the 202's {status, approval_id}
      // body has to be parsed before the checks see it (204 has no body).
      let payload: Record<string, unknown> | null = null;
      try {
        payload = await resp.json();
      } catch {
        payload = null;
      }
      if (resp.status === 202 || payload?.status === "awaiting_approval") {
        // GSEC FX.163 (Q150=A): the clear did NOT run — a second approver has
        // to approve it, so the marking stays shown with the shared notice.
        const approvalId = payload?.approval_id ?? payload?.id ?? "?";
        setNote(`Clear data marking needs a second approver (request #${approvalId})`);
        return;
      }
      if (!resp.ok) {
        setNote(`Could not clear "${label}" — the server rejected the request.`);
        return;
      }
      setNote(null);
      await load();
    } catch (err) {
      setNote(`Could not clear "${label}": ${(err as Error)?.message || "Unknown error"}`);
    }
  };

  if (!stamp) return null;
  const banner = markingBannerText(stamp);
  return (
    <div
      role="note"
      aria-label={banner}
      data-marking-banner={position}
      className={`${
        position === "bottom" ? "border-t" : "border-b"
      } w-full bg-red-900 text-red-50 border-red-950 px-4 py-2 text-sm font-semibold text-center shrink-0`}
    >
      {banner}
      {canClear ? (
        <span data-marking-clear-actions>
          {stamp.labels.map((label) => (
            <button
              key={label}
              type="button"
              data-marking-clear-label={label}
              className="ml-3 inline-block rounded bg-white/10 px-2 py-0.5 text-xs font-medium text-white hover:bg-white/20 active:bg-white/30"
              onClick={() => void clearLabel(label)}
            >
              Clear
            </button>
          ))}
        </span>
      ) : null}
      {note ? <p data-marking-note>{note}</p> : null}
    </div>
  );
}

export default DataMarkingBanner;
