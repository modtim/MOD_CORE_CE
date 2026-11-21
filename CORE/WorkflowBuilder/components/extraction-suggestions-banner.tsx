"use client";

// expr 3.6c — extraction-suggestion banner (read-only info + one-click
// "Extract as new workflow" per suggestion).
//
// The API (CORE/API/routers/workflow_extraction_suggestions.py, committed,
// mount pending) reports same-tenant equivalence classes: node groups in the
// open workflow that also appear in other workflows. When any exist, this
// compact info bar suggests extracting a reusable sub-workflow. There is NO
// extract action here — the banner only informs.
//
// Read path: GET {api}/road/{workflow_id}/extraction-suggestions →
// {items: [{canonical_hash, hash_kind, node_ids, node_count,
//           other_workflow_count, other_workflows: [{id, name}]}]}.
//
// Extract path (expr 3.6c): POST {api}/road/{workflow_id}/extraction-
// suggestions/{canonical_hash}/extract → 201 {workflow_id, name, node_count}
// (success line "Created “{name}”" linking /?workflow={workflow_id}, the
// same deep-link useOpenWorkflowFromQuery consumes) or 404 {detail:"not_found"}
// → inline "Extract failed". One request per item per render; the button is
// disabled + "Extracting…" while in flight. Response bodies are never logged.
//
// Behaviour contract:
//  - no workflow open / no items / non-OK (404 unmounted, CE carve,
//   cross-tenant) / fetch failure → NOTHING renders, editor never blocked;
//  - dismiss (×) is remembered in sessionStorage per workflow_id — a fresh
//   visit to the same workflow in the same session stays silent;
//  - "Details" toggle lists at most 10 items (node_count + other-workflow
//   names as plain text — every server string reaches the DOM as a React
//   text node, never HTML);
//  - role="note" + aria-label; blue info family keeps contrast well above
//   4.5:1 (same class vocabulary as data-marking-banner.tsx).

import { apiUrl as resolveApiUrl } from "@/lib/runtime-config";
import React, { useCallback, useEffect, useState } from "react";
import { getAccessToken } from "@/lib/api-client";

const DISMISS_PREFIX = "EXTRACTION_SUGGESTIONS_DISMISSED_";
const MAX_DETAIL_ROWS = 10;
const MAX_OTHER_NAMES = 10;

interface ExtractionSuggestionItem {
  canonical_hash: string;
  hash_kind: string;
  node_ids: string[];
  node_count: number;
  other_workflow_count: number;
  other_workflows: Array<{ id: number; name: string }>;
}

type ExtractOutcome =
  { status: "pending" | "failed" } | { status: "created"; name: string; workflowId: number };

function dismissedInSession(storageKey: string | null): boolean {
  if (!storageKey) return false;
  try {
    return sessionStorage.getItem(storageKey) === "1";
  } catch {
    return false;
  }
}

function rememberDismissal(storageKey: string | null): void {
  if (!storageKey) return;
  try {
    sessionStorage.setItem(storageKey, "1");
  } catch {
    // sessionStorage unavailable (private mode / non-browser): the dismiss
    // still hides the bar for this render, it just is not remembered.
  }
}

export interface ExtractionSuggestionsBannerProps {
  /** The open workflow's numeric id; null/undefined → no fetch, no bar. */
  workflowId: number | null;
  /** Test overrides — production callers omit both and get the runtime
   *  config URL plus the stored Keycloak access token (same helper pair
   *  data-marking-banner.tsx uses). */
  apiUrl?: string;
  accessToken?: string | null;
}

export function ExtractionSuggestionsBanner({
  workflowId,
  apiUrl = resolveApiUrl(),
  accessToken,
}: ExtractionSuggestionsBannerProps) {
  const [items, setItems] = useState<ExtractionSuggestionItem[]>([]);
  const [dismissed, setDismissed] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [extractOutcomes, setExtractOutcomes] = useState<Record<string, ExtractOutcome>>({});
  const [extractBusy, setExtractBusy] = useState<Record<string, boolean>>({});

  const id = workflowId == null ? null : String(workflowId);
  const storageKey = id ? `${DISMISS_PREFIX}${id}` : null;

  // Fetch once per workflow — the equivalence index only changes when
  // workflows change, and this view re-mounts on workflow switch.
  const load = useCallback(
    async (): Promise<void> => {
      if (!id) return;
      try {
        const token = accessToken ?? getAccessToken();
        if (!token) return;
        const resp = await fetch(
          `${apiUrl}/road/${encodeURIComponent(id)}/extraction-suggestions`,
          { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } },
        );
        // !ok covers unmounted (404), CE carve, cross-tenant (404). All of
        // them mean "show nothing" — not an error state.
        if (!resp.ok) return;
        const data = await resp.json();
        const rows = data?.items;
        if (Array.isArray(rows)) {
          setItems(
            rows
              .filter((row: unknown) => typeof row === "object")
              .map((row) => row as ExtractionSuggestionItem),
          );
        }
      } catch {
        // Silent fallback: a fetch/parse hiccup never blocks the editor.
      }
    },
    [id, apiUrl, accessToken],
  );

  const extract = useCallback(
    async (canonicalHash: string): Promise<void> => {
      const failed = (prev: Record<string, ExtractOutcome>) => ({
        ...prev,
        [canonicalHash]: { status: "failed" as "failed" },
      });
      if (!id) {
        setExtractOutcomes(failed);
        return;
      }
      try {
        const token = accessToken ?? getAccessToken();
        if (!token) {
          setExtractOutcomes(failed);
          return;
        }
        const resp = await fetch(
          `${apiUrl}/road/${encodeURIComponent(id)}/extraction-suggestions/${encodeURIComponent(canonicalHash)}/extract`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          },
        );
        if (resp.status === 201) {
          const data = await resp.json();
          const name = String(data?.name ?? "");
          const newId = Number(data?.workflow_id);
          if (name && Number.isFinite(newId)) {
            setExtractOutcomes((prev) => ({
              ...prev,
              [canonicalHash]: { status: "created", name, workflowId: newId },
            }));
            return;
          }
        }
        setExtractOutcomes(failed);
      } catch {
        // A fetch/parse hiccup is just "Extract failed" — never logged.
        setExtractOutcomes(failed);
      }
    },
    [id, apiUrl, accessToken],
  );

  useEffect(() => {
    setItems([]);
    setExtractOutcomes({});
    setExtractBusy({});
    if (!id) return;
    if (dismissedInSession(storageKey)) {
      setDismissed(true);
      return;
    }
    void load();
  }, [load, id, storageKey]);

  if (!id || dismissed || items.length === 0) return null;

  const groupCount = items.length;
  const otherCount = items.reduce(
    (sum, item) => sum + (Number(item.other_workflow_count) || 0),
    0,
  );
  const summary =
    `${groupCount} repeated node groups also appear in ` +
    `${otherCount} other workflows — consider extracting a reusable sub-workflow`;

  // Detail rows: server strings only ever reach the DOM as React text.
  const detailRows = items.slice(0, MAX_DETAIL_ROWS).map((item) => {
    const nodeCount = Number(item.node_count) || 0;
    const names = (item.other_workflows ?? [])
      .slice(0, MAX_OTHER_NAMES)
      .map((other) => String(other?.name ?? other?.id ?? ""))
      .filter(Boolean);
    return names.length > 0
      ? `${nodeCount} nodes — ${names.join(", ")}`
      : `${nodeCount} nodes`;
  });

  return (
    <div
      role="note"
      aria-label={summary}
      data-extraction-suggestions-banner="1"
      className="border-b w-full bg-blue-900 text-blue-50 border-blue-950 px-4 py-2 text-sm shrink-0"
    >
      {summary}
      <button
        type="button"
        data-testid="extraction-dismiss"
        aria-label="Dismiss extraction suggestion"
        className="ml-3 inline-block rounded bg-white/10 px-2 py-0.5 text-xs font-medium text-white hover:bg-white/20 active:bg-white/30"
        onClick={() => {
          setDismissed(true);
          rememberDismissal(storageKey);
        }}
      >
        ×
      </button>
      <button
        type="button"
        data-testid="extraction-details-toggle"
        aria-label="Show extraction suggestion details"
        className="ml-3 inline-block rounded bg-white/10 px-2 py-0.5 text-xs font-medium text-white hover:bg-white/20 active:bg-white/30"
        onClick={() => setDetailsOpen((open) => !open)}
      >
        Details
      </button>
      {items.slice(0, MAX_DETAIL_ROWS).map((item, index) => {
        const hash = String(item?.canonical_hash ?? "");
        const outcome = hash ? extractOutcomes[hash] : undefined;
        const busy = hash ? extractBusy[hash] === true : false;
        return (
          <div
            key={`extraction-extract-${index}`}
            data-extraction-extract-row="1"
            className="ml-3 inline-block text-xs"
          >
            <button
              type="button"
              data-testid="extraction-extract"
              aria-label="Extract as new workflow"
              disabled={busy || outcome != null}
              onClick={() => {
                if (!hash || busy || outcome != null) return;
                setExtractBusy((prev) => ({ ...prev, [hash]: true }));
                void extract(hash);
              }}
              className="ml-3 inline-block rounded bg-white/10 px-2 py-0.5 text-xs font-medium text-white hover:bg-white/20 active:bg-white/30"
            >
              Extract as new workflow
            </button>
            {busy ? (
              <span data-extraction-extract-status="pending" className="ml-2 text-blue-50">
                Extracting…
              </span>
            ) : null}
            {outcome?.status === "created" ? (
              <span data-extraction-extract-status="created" className="ml-2 text-blue-50">
                Created “{(outcome as { name: string }).name}”{" "}
                <a
                  href={`/?workflow=${(outcome as { workflowId: number }).workflowId}`}
                  data-extraction-open-link="1"
                  className="underline text-blue-50 hover:underline"
                >
                  Open
                </a>
              </span>
            ) : null}
            {outcome?.status === "failed" ? (
              <span data-extraction-extract-status="failed" className="ml-2 text-blue-50">
                Extract failed
              </span>
            ) : null}
          </div>
        );
      })}
      {detailsOpen ? (
        <ul data-extraction-details className="ml-3 text-xs text-blue-50">
          {detailRows.map((row, index) => (
            <li key={`extraction-detail-${index}`} data-extraction-detail-row="1" className="py-0.5">
              {row}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export default ExtractionSuggestionsBanner;
