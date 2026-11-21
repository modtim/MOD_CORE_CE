/**
 * 23.1a.s3 — the two per-variant actions the verdict list offers.
 *
 * Routes (confirmed in CORE/API/routers, mounted under /v1 by main.py):
 *
 *  POST /v1/node-definitions/{node_id}/variants/{variant}/patch
 *      routers/variant_patch_dispatch.py:70 (prefix "/node-definitions")
 *      + :161 (@router.post "/{node_id}/variants/{variant}/patch"),
 *      re-registered at routers/nodes.py:295. Body = PatchDispatchRequest
 *      (variant_patch_dispatch.py:85): stage (required, one of "build",
 *      "smoke_drift", "smoke_crash"), log_tail (required), source_files
 *      (required, [{filename, body}]), port_report / prior_explanation /
 *      rebuild_env optional. Response is either
 *      { dispatched: true, task_id, attempts_used, attempts_remaining,
 *        token_total, token_remaining, diff_hash, confidence, source,
 *        target_files } or { dispatched: false, reason_code, refuse_reason, … }
 *      — a refusal is a 200, so "dispatched" is the gate, not the status.
 *
 *  POST /v1/nodes/{node_id}/rebuild  (200)
 *      routers/node_code_edit.py:327 (no prefix; re-registered by routers/nodes.py
 *      include_router at :279). Body = { scope: "single_variant", variant }
 *      (RebuildRequest, node_code_edit.py:119). Seeds a BuildSpec from the
 *      node's latest saved NodeSourceRevision — including
 *      source_overrides_by_variant[variant], i.e. the code the code editor
 *      just saved — and dispatches build → push → sample-test for THAT variant
 *      only (node_builder/build_dag/generator.py:93 omits generate/ingest and
 *      register). Returns { workflow_run_id, workflow_id, scope, critic_warnings }.
 *      409 {code:"must_fork"} on a platform node, 402 {code:"entitlement_required"}
 *      without the custom-nodes entitlement, 409 {code:"registry_not_isolated"}.
 *      (The old call, POST /nodes/{id}/build-variant/{tag}, builds a NEW hardware
 *      tag from the node's existing image and 409s when the tag already exists —
 *      it never re-ran parity on edited code, so it was wrong for re-verify.)
 *
 * Neither response carries a verdict row, so the caller re-reads
 * GET /v1/nodes/{id}/variant-verification after a dispatch to update the row.
 */

import { authenticatedFetch } from "@/lib/api-client";

export type PatchStage = "build" | "smoke_drift" | "smoke_crash";

export interface VariantActionResult {
  dispatched: boolean;
  task_id: number | null;
  /** Human line for the row: what the platform did or why it refused. */
  note: string | null;
  attempts_used: number | null;
  attempts_remaining: number | null;
  token_remaining: number | null;
}

export interface VariantReverifyResult {
  dispatched: boolean;
  task_id: number | null;
  note: string | null;
}

function readDetail(raw: string): string {
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw);
    const detail = parsed?.detail;
    if (typeof detail === "string") return detail;
    if (detail === null || detail === undefined) return "";
    const nested = (detail as Record<string, unknown>)?.detail;
    return typeof nested === "string" ? nested : raw;
  } catch {
    return raw;
  }
}

/** Verdict → patch stage. The verdict list only knows the outcome, so the
 * stage is chosen from it: a drift goes to the smoke-drift loop, a hard
 * failure to the smoke-crash loop, everything else to the build loop. */
export function stageForVerdict(verdict: string): PatchStage {
  if (verdict === "drift") return "smoke_drift";
  if (verdict === "failed" || verdict === "incompatible") return "smoke_crash";
  return "build";
}

/** The variant's current post-port source tree, straight from
 * GET /v1/nodes/{id}/source (the same document variant-code-editor reads).
 * The patch router needs it to propose against, and it is the only place
 * the WB keeps that tree, so we read it rather than reconstruct it. */
async function readVariantSourceFiles(
  apiUrl: string,
  nodeId: number,
  variant: string,
): Promise<{ files: { filename: string; body: string }[]; origin: string }> {
  const resp = await authenticatedFetch(`${apiUrl}/nodes/${nodeId}/source`);
  if (!resp?.ok) {
    throw new Error(`Could not read the ${variant} source: ${readDetail(await resp.text().catch(() => "")) || `HTTP ${resp?.status ?? "failed"}`}`);
  }
  const body = (await resp.json()) as Record<string, unknown>;
  const variants = (body.variants ?? {}) as Record<string, unknown>;
  const entry = (variants[variant] ?? {}) as Record<string, unknown>;
  const rawFiles = Array.isArray(entry.files) ? entry.files : [];
  return {
    files: rawFiles.map((f: unknown) => {
      const file = (f ?? {}) as Record<string, unknown>;
      return { filename: String(file.filename ?? ""), body: String(file.content ?? "") };
    }),
    origin: String(entry.origin ?? ""),
  };
}

export async function dispatchVariantRepair({
  apiUrl,
  nodeId,
  variant,
  verdict,
  reason,
}: {
  apiUrl: string;
  nodeId: number;
  variant: string;
  verdict: string;
  reason: string;
}): Promise<VariantActionResult> {
  const source = await readVariantSourceFiles(apiUrl, nodeId, variant);
  if (source.files.length === 0) {
    throw new Error(`No ${variant} source files on file — nothing for the wizard to repair.`);
  }

  const resp = await authenticatedFetch(
    `${apiUrl}/node-definitions/${nodeId}/variants/${variant}/patch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stage: stageForVerdict(verdict),
        log_tail: reason || `verdict ${verdict} for variant ${variant}`,
        source_files: source.files,
      }),
    },
  );
  if (!resp?.ok) {
    const detail = readDetail(await resp.text().catch(() => ""));
    throw new Error(detail || `Patch dispatch failed: HTTP ${resp?.status ?? "failed"}`);
  }

  const body = await resp.json();
  const dispatched = body?.dispatched === true;
  return {
    dispatched,
    task_id: body?.task_id === null || body?.task_id === undefined ? null : Number(body.task_id),
    note: dispatched
      ? `patch proposed (${String(body?.source ?? "wizard")}, confidence ${Number(body?.confidence ?? 0).toFixed(2)})`
      : String(body?.refuse_reason ?? "the platform refused to patch this variant"),
    attempts_used: body?.attempts_used === undefined ? null : Number(body.attempts_used),
    attempts_remaining: body?.attempts_remaining === undefined ? null : Number(body.attempts_remaining),
    token_remaining: body?.token_remaining === undefined ? null : Number(body.token_remaining),
  };
}

export async function requestVariantReverify({
  apiUrl,
  nodeId,
  variant,
}: {
  apiUrl: string;
  nodeId: number;
  variant: string;
}): Promise<VariantReverifyResult> {
  // 23.1a — "fix my code": re-verify must re-run parity for THIS variant
  // against the code the editor just saved. POST /nodes/{id}/build-variant/{tag}
  // is the wrong call here: it builds a NEW hardware tag from the node's
  // existing image and 409s when that tag already has a variant — so re-
  // verifying the red rocm variant always refused, and it never looked at the
  // saved source override at all. The right runner is the single-variant
  // rebuild (routers/node_code_edit.py:327, mounted at /v1/nodes/{id}/rebuild):
  // it seeds a BuildSpec from the node's LATEST saved revision (which carries
  // source_overrides_by_variant[variant]), dispatches build → push → sample-
  // test for that one variant only, and the sample-test posts its verdict to
  // the same in-place verification writer the full build uses.
  const resp = await authenticatedFetch(
    `${apiUrl}/nodes/${nodeId}/rebuild`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "single_variant", variant }),
    },
  );
  if (!resp?.ok) {
    const raw = await resp.text().catch(() => "");
    const code = (() => {
      try {
        const parsed = JSON.parse(raw);
        const detail = parsed?.detail;
        return typeof detail === "object" ? String(detail.code ?? "") : "";
      } catch {
        return "";
      }
    })();
    if (code === "entitlement_required") {
      throw new Error("Rebuilding needs the custom-nodes entitlement for this tenant.");
    }
    if (code === "registry_not_isolated") {
      throw new Error("This node has no tenant registry to rebuild into — copy it first.");
    }
    throw new Error(readDetail(raw) || `Re-verify failed: HTTP ${resp?.status ?? "failed"}`);
  }
  const body = await resp.json();
  const runId = body?.workflow_run_id;
  return {
    dispatched: true,
    task_id: runId === null || runId === undefined ? null : Number(runId),
    note: `re-verifying ${variant} with your saved code`,
  };
}
