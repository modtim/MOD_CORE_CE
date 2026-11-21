"use client";

/**
 * sz-2.10a — live-debugger frame inspector support (see
 * CORE/API/routers/workflows_debug.py).
 *
 * GET {apiUrl}/road/workflow-runs/{runId}/debug/frame/{nodeId} →
 * DebugFrameResponse. A foreign run / missing node is a generic 404;
 * 401/403 are auth refusals. Values the API masks arrive as the literal
 * string "***" — we never try to unmask, only render them as a pill.
 */

export interface DebugFrame {
  run_id: number;
  node_id: string;
  paused: boolean;
  node_kind?: string | null;
  node_definition_id?: number | null;
  config?: Record<string, unknown>;
  inputs?: Record<string, unknown>;
  upstream_outputs?: Array<Record<string, unknown>>;
  partial_output?: unknown;
  worker_context?: Record<string, unknown> | null;
}

export function debugFrameUrl(
  apiUrl: string,
  runId: number,
  nodeId: string
): string {
  return `${apiUrl}/road/workflow-runs/${encodeURIComponent(String(runId))}/debug/frame/${encodeURIComponent(nodeId)}`;
}

/**
 * Fetch one paused node's frame. `null` on 404 (generic not-found — a
 * foreign run is indistinguishable from a missing one). Throws a plain
 * Error with a SHORT safe message on any other non-OK: the token and the
 * response body never leave this function, so neither can leak into the
 * DOM through an error string.
 */
export async function fetchDebugFrame(
  apiUrl: string,
  accessToken: string,
  runId: number,
  nodeId: string
): Promise<DebugFrame | null> {
  const res = await fetch(
    debugFrameUrl(apiUrl, runId, nodeId),
    { headers: { Authorization: `Bearer ${accessToken}` } }
  );
  if (res.ok) return (await res.json()) as DebugFrame;
  if (res.status === 404) return null;
  throw new Error(`debug frame unavailable (HTTP ${res.status})`);
}

/** True when the API masked a value — render as a pill, never unmask. */
export function isMaskedValue(value: unknown): boolean {
  return typeof value === "string" && value === "***";
}

/** Display form: JSON values pretty-printed, everything else stringified. */
export function formatFrameValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return value === null ? "null" : "";
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * Copy of `obj` with every masked value removed, recursively (plain objects
 * and arrays only). The API masks secrets as the literal "***"; sending those
 * placeholders back would overwrite real values with the mask, so a hot-swap
 * payload is always built through this.
 */
export function stripMaskedKeys(obj: unknown): unknown {
  if (isMaskedValue(obj)) return undefined;
  if (Array.isArray(obj)) {
    return obj.map((item) => stripMaskedKeys(item)).filter((item) => item !== undefined);
  }
  if (obj && typeof obj === "object") {
    const out: Record<string, unknown> = {};
    Object.entries(obj).forEach(([k, v]) => {
      const kept = stripMaskedKeys(v);
      if (kept !== undefined) out[k] = kept;
    });
    return out;
  }
  return obj;
}

/** Truncate a formatted value to `max` chars, marking the cut with "…". */
export function truncateFrameValue(value: string, max = 500): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

export function isFrameValueLong(value: unknown, max = 500): boolean {
  return formatFrameValue(value).length > max;
}

/**
 * POST {apiUrl}/road/workflow-runs/{runId}/debug/hot-swap/{nodeId}
 * (see CORE/API/routers/workflows_debug.py HotSwapRequest).
 */
export function debugHotSwapUrl(
  apiUrl: string,
  runId: number,
  nodeId: string
): string {
  return `${apiUrl}/road/workflow-runs/${encodeURIComponent(String(runId))}/debug/hot-swap/${encodeURIComponent(nodeId)}`;
}

export type HotSwapResult =
  { ok: true; override: Record<string, unknown> } |
  { ok: false; status: number; detail: string };

/**
 * Store a per-run override on a paused, not-yet-dispatched node.
 *
 * Never throws on an HTTP error: 404 (generic not-found), 409 (node not
 * paused / already dispatched) and 422 (nothing to swap / bad config) all
 * resolve to `{ok: false, status, detail}` carrying the API's own curated
 * `detail` text. A transport failure reports status 0.
 */
export async function hotSwapDebugNode(
  apiUrl: string,
  accessToken: string,
  runId: number,
  nodeId: string,
  body: Record<string, unknown>
): Promise<HotSwapResult> {
  let res: Response;
  try {
    res = await fetch(
      debugHotSwapUrl(apiUrl, runId, nodeId),
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${accessToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      }
    );
  } catch {
    return { ok: false, status: 0, detail: "hot-swap request failed" };
  }
  if (res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }
    const override =
      payload && typeof payload === "object"
        ? (payload as { override?: Record<string, unknown> }).override
        : undefined;
    return { ok: true, override: override ?? {} };
  }
  return { ok: false, status: res.status, detail: await detailFromResponse(res) };
}

/** The API's curated refusal text, or a short safe fallback. */
async function detailFromResponse(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body === "object" && typeof (body as any).detail === "string") {
      return (body as any).detail;
    }
  } catch {
    // fall through
  }
  return `hot-swap refused (HTTP ${res.status})`;
}
