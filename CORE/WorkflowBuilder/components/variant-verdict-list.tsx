"use client";

/**
 * 23.1a.s1 + s3 — per-variant verdict list for one node, with the two
 * per-row actions.
 *
 * Reads GET /v1/nodes/{id}/variant-verification and shows, per variant
 * (cpu / cuda / rocm / …): the verdict, why the platform thinks that
 * (score + what it was scored against), and when it was last scored.
 *
 * The response also carries `pending_variants` — variants the platform
 * expects but has not finished verifying yet — and `awaiting_drift_ruling`
 * rows, which are verdicts the platform is deliberately withholding from
 * selection until someone rules on the drift. Both get their own visual
 * state so "not verified yet" never reads the same as "verified".
 *
 * s3 adds two buttons per verdict row (lib/variant-actions.ts):
 *   "Ask the wizard to repair"  → POST /v1/node-definitions/{id}/variants/{v}/patch
 *   "Re-verify this variant only" → POST /v1/nodes/{id}/build-variant/{tag}
 * Neither response carries a verdict row, so after either dispatch the list
 * re-reads its own verification document and the row updates in place.
 * Busy and error state are per row, never global: one variant being repaired
 * must not make the other rows look unavailable.
 */

import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, RotateCcw, Wrench } from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import {
  dispatchVariantRepair,
  requestVariantReverify,
  type VariantActionResult,
  type VariantReverifyResult,
} from "@/lib/variant-actions";

export interface VariantVerdictRow {
  variant: string;
  verdict: string;
  score: number | null;
  scored_against_variant: string | null;
  output_type_at_test: string | null;
  scored_at: string | null;
  attempt_count: number;
  best_score: number | null;
  drift_ruling: string | null;
  awaiting_drift_ruling: boolean;
}

interface VariantVerdictListProps {
  nodeId: number;
  apiUrl: string;
  /** Fired after the list is (re)read so the parent can chain its own refresh. */
  onLoaded?: (rows: VariantVerdictRow[]) => void;
  /** Fired when a row's "Edit code" is clicked — the parent owns the editor modal. */
  onEditCode?: (variant: string) => void;
  /** Bump to force a fresh read (e.g. after a source save). */
  reloadKey?: string;
}

export interface VariantActionState {
  busy: "repair" | "reverify" | null;
  error: string | null;
  note: string | null;
  busyVariant: string | null;
}

function verdictStyle(verdict: string): string {
  if (verdict === "verified") return "bg-emerald-100 text-emerald-800";
  if (verdict === "marginal") return "bg-amber-100 text-amber-800";
  if (verdict === "drift") return "bg-violet-100 text-violet-800";
  if (verdict === "failed" || verdict === "incompatible") return "bg-red-100 text-red-800";
  return "bg-slate-100 text-slate-700";
}

function reasonText(row: VariantVerdictRow): string {
  const parts: string[] = [];
  if (row.score !== null) parts.push(`score ${row.score.toFixed(2)}`);
  if (row.best_score !== null && row.score !== null && row.best_score !== row.score) {
    parts.push(`best ${row.best_score.toFixed(2)}`);
  }
  if (row.scored_against_variant) parts.push(`scored against ${row.scored_against_variant}`);
  if (row.output_type_at_test) parts.push(`output type ${row.output_type_at_test}`);
  if (row.attempt_count > 1) parts.push(`${row.attempt_count} attempts`);
  if (row.drift_ruling) parts.push(`drift ruled ${row.drift_ruling}`);
  return parts.length > 0 ? parts.join(" · ") : "no measurement recorded";
}

function timestampText(value: string | null): string {
  if (!value) return "never scored";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

export default function VariantVerdictList(props: VariantVerdictListProps) {
  const { nodeId, apiUrl, onLoaded, onEditCode, reloadKey } = props;
  const [rows, setRows] = useState<VariantVerdictRow[] | null>(null);
  const [pending, setPending] = useState<string[]>([]);
  const [referenceVariant, setReferenceVariant] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actions, setActions] = useState<Record<string, VariantActionState>>({});

  const readOnce = async () => {
    const resp = await authenticatedFetch(`${apiUrl}/nodes/${nodeId}/variant-verification`);
    if (!resp?.ok) throw new Error(String(resp?.status ?? "failed"));

    const body = await resp.json();
    const next: VariantVerdictRow[] = (body?.variants ?? []).map((r: unknown) => {
      const row = r as Record<string, unknown>;
      return {
        variant: String(row.variant ?? ""),
        verdict: String(row.verdict ?? ""),
        score: row.score === null || row.score === undefined ? null : Number(row.score),
        scored_against_variant:
          row.scored_against_variant === null || row.scored_against_variant === undefined
            ? null
            : String(row.scored_against_variant),
        output_type_at_test:
          row.output_type_at_test === null || row.output_type_at_test === undefined
            ? null
            : String(row.output_type_at_test),
        scored_at: row.scored_at === null ? null : String(row.scored_at ?? ""),
        attempt_count: Number(row.attempt_count ?? 1),
        best_score: row.best_score === null || row.best_score === undefined
          ? null
          : Number(row.best_score),
        drift_ruling: row.drift_ruling === null ? null : String(row.drift_ruling ?? ""),
        awaiting_drift_ruling: Boolean(row.awaiting_drift_ruling),
      };
    });

    setRows(next);
    setPending((body?.pending_variants ?? []).map((v: unknown) => String(v)));
    setReferenceVariant(body?.reference_variant === null ? null : String(body?.reference_variant ?? ""));
    setError(null);
    onLoaded?.(next);
  };

  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (cancelled) return;
      try {
        await readOnce();
      } catch (e) {
        if (cancelled) return;
        // An unreadable verdict list is shown as unreadable, never as an
        // empty list — "no verdicts" would read as "nothing is verified"
        // and invite someone to ship a variant the platform never checked.
        setRows(null);
        setPending([]);
        setError(e instanceof Error ? e.message : "Could not read verdicts.");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, nodeId, reloadKey]);

  const rowFor = (variant: string): VariantVerdictRow | null =>
    (rows ?? []).find((r) => r.variant === variant) ?? null;

  const runAction = async (variant: string, kind: "repair" | "reverify") => {
    if (!variant || (actions[variant]?.busy)) return;
    setActions((prev) => ({ ...prev, [variant]: { busy: kind, error: null, note: null, busyVariant: variant } }));

    let result: VariantActionResult | VariantReverifyResult | null = null;
    let failure: string | null = null;
    try {
      if (kind === "repair") {
        const row = rowFor(variant);
        result = await dispatchVariantRepair({
          apiUrl,
          nodeId,
          variant,
          verdict: row?.verdict ?? "build",
          reason: row ? reasonText(row) : "",
        });
      } else {
        result = await requestVariantReverify({ apiUrl, nodeId, variant });
      }
    } catch (e) {
      failure = e instanceof Error ? e.message : "Action failed";
    }

    // The dispatch responses carry no verdict row, so the row is refreshed
    // by re-reading this node's verification. A failed re-read must not wipe
    // the row we already had, so its error is kept on the action, not the list.
    try {
      await readOnce();
    } catch {
      /* keep the rows we have */
    }

    setActions((prev) => ({
      ...prev,
      [variant]: {
        busy: null,
        error: failure,
        note: result ? String(result.note ?? "") : null,
        busyVariant: null,
      },
    }));
  };

  if (error) {
    return (
      <div className="bg-red-100 text-red-800 rounded p-2 flex items-center gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span>{`Verdicts unavailable: ${error}`}</span>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 mr-2 shrink-0 animate-spin" />
        <span>Reading variant verdicts…</span>
      </div>
    );
  }

  if (rows.length === 0 && pending.length === 0) {
    return (
      <div className="text-muted-foreground flex items-center gap-2">
        <RotateCcw className="h-4 w-4 shrink-0" />
        <span>No variants verified yet for this node.</span>
      </div>
    );
  }

  const pendingText = pending.length > 0 ? `verifying ${pending.join(", ")}` : "";
  const refText = referenceVariant ? `reference: ${referenceVariant}` : "";

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between border-b pb-2">
        <span className="font-medium">{`Variant verdicts (${rows.length})`}</span>
        {pendingText ? <span className="text-muted-foreground">{pendingText}</span> : null}
        {refText ? <span className="text-muted-foreground">{refText}</span> : null}
      </div>

      {rows.map((row) => {
        const state = actions[row.variant] ?? { busy: null, error: null, note: null, busyVariant: null };
        const busy = state.busy !== null;
        return (
          <div key={row.variant} className="flex items-center gap-2 py-1 rounded">
            <span
              className={`inline-flex items-center rounded px-2 py-0.5 ${verdictStyle(row.verdict)}`}
              data-variant={row.variant}
              data-verdict={row.verdict}
            >
              {row.verdict}
            </span>
            <span className="font-mono shrink-0">{row.variant}</span>
            <div className="flex flex-col text-xs text-muted-foreground">
              <span>{reasonText(row)}</span>
              <span>{timestampText(row.scored_at)}</span>
              {row.awaiting_drift_ruling ? (
                <span className="text-violet-800">
                  diverged — waiting on you to rule (publish / drop / keep fixing)
                </span>
              ) : null}
              {state.error ? <span className="text-red-700" data-testid={`variant-action-error-${row.variant}`}>{state.error}</span> : null}
              {state.note ? <span className="text-slate-600" data-testid={`variant-action-note-${row.variant}`}>{state.note}</span> : null}
              {busy ? (
                <span className="flex items-center gap-1 text-muted-foreground" data-testid={`variant-action-busy-${row.variant}`}>
                  {state.busy === "repair" ? <Wrench className="h-3.5 w-3.5 shrink-0" /> : <Loader2 className="h-3.5 w-3.5 mr-2 shrink-0 animate-spin" />}
                  {state.busy === "repair" ? "asking the wizard to repair…" : "re-verifying…"}
                </span>
              ) : null}
            </div>

            {!busy ? (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  data-testid={`variant-repair-${row.variant}`}
                  onClick={() => void runAction(row.variant, "repair")}
                  className="inline-flex items-center rounded px-2 py-0.5 border border-violet-300 text-violet-700"
                >
                  <Wrench className="h-3.5 w-3.5 shrink-0" />
                  Ask the wizard to repair
                </button>
                <button
                  type="button"
                  data-testid={`variant-reverify-${row.variant}`}
                  onClick={() => void runAction(row.variant, "reverify")}
                  className="inline-flex items-center rounded px-2 py-0.5 border border-slate-300 text-slate-700"
                >
                  <RotateCcw className="h-3.5 w-3.5 shrink-0" />
                  Re-verify this variant only
                </button>
                {onEditCode ? (
                  <button
                    type="button"
                    data-testid={`variant-edit-code-${row.variant}`}
                    onClick={() => onEditCode(row.variant)}
                    className="inline-flex items-center rounded px-2 py-0.5 border border-slate-300 text-slate-700"
                  >
                    Edit code
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        );
      })}

      {pending.map((variant) => (
        <div key={variant} className="flex items-center gap-2 py-1 rounded">
          <span className="inline-flex items-center rounded px-2 py-0.5 bg-slate-100 text-slate-600" data-variant={variant}>
            pending
          </span>
          <span className="font-mono shrink-0">{variant}</span>
          <span className="text-xs text-muted-foreground">no verdict yet</span>
        </div>
      ))}
    </div>
  );
}
