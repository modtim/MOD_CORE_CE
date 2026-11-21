/**
 * sz-2.10c — WorkflowBuilder breakpoints API client + state helpers.
 *
 * API contract (routers/workflow_breakpoints.py):
 *   GET  /road/{workflow_id}/versions/{version_id}/breakpoints
 *        -> { workflow_id, version_id, breakpoints: Breakpoint[] }
 *   PUT  /road/{workflow_id}/versions/{version_id}/breakpoints/{node_id} -> Breakpoint
 *   DELETE /road/{workflow_id}/versions/{version_id}/breakpoints/{node_id} -> 204
 *
 * `apiUrl` already carries the /v1 mount (build-and-deploy.sh appends /v1
 * once to MOD_API_URL); paths here are unprefixed — a literal /v1 here would
 * produce /v1/v1 and 404.
 *
 * Errors never leak the access token or the response body into their message.
 */

export interface Breakpoint {
  id: number;
  node_id: string;
  enabled: boolean;
  condition: Record<string, unknown> | null;
  hit_count: number;
  last_hit_at: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Build the GET /breakpoints URL. Every path segment is encoded.
 */
function breakpointsUrl(
  apiUrl: string,
  workflowId: string,
  versionId: string
): string {
  return `${apiUrl}/road/${encodeURIComponent(workflowId)}/versions/${encodeURIComponent(versionId)}/breakpoints`;
}

/**
 * Build the PUT /breakpoints/{nodeId} URL.
 */
function breakpointNodeUrl(
  apiUrl: string,
  workflowId: string,
  versionId: string,
  nodeId: string
): string {
  return `${apiUrl}/road/${encodeURIComponent(workflowId)}/versions/${encodeURIComponent(versionId)}/breakpoints/${encodeURIComponent(nodeId)}`;
}

/**
 * List all breakpoints for a workflow version.
 *
 * Returns an empty array on 404 (the API may return 404 when no breakpoints
 * exist yet — the caller should treat it the same as an empty list).
 */
export async function listBreakpoints(
  apiUrl: string,
  accessToken: string,
  workflowId: string,
  versionId: string
): Promise<Breakpoint[]> {
  const res = await fetch(breakpointsUrl(apiUrl, workflowId, versionId), {
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
  });
  if (res.ok) {
    const body = (await res.json()) as { breakpoints: Breakpoint[] };
    return body.breakpoints;
  }
  if (res.status === 404) return [];
  throw new Error(`breakpoints request failed (${res.status})`);
}

/**
 * Create or update a breakpoint on a node.
 *
 * `opts.condition` — pass `null` to clear the condition entirely.
 */
export async function setBreakpoint(
  apiUrl: string,
  accessToken: string,
  workflowId: string,
  versionId: string,
  nodeId: string,
  opts?: { enabled?: boolean; condition?: Record<string, unknown> | null }
): Promise<Breakpoint> {
  const body: Record<string, unknown> = {};
  if (opts?.enabled !== undefined) body.enabled = opts.enabled;
  if (opts?.condition !== undefined) body.condition = opts.condition;

  const res = await fetch(breakpointNodeUrl(apiUrl, workflowId, versionId, nodeId), {
    method: "PUT",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) return (await res.json()) as Breakpoint;
  throw new Error(`breakpoints request failed (${res.status})`);
}

/**
 * Remove a breakpoint. 204 or 404 both resolve (idempotent).
 */
export async function removeBreakpoint(
  apiUrl: string,
  accessToken: string,
  workflowId: string,
  versionId: string,
  nodeId: string
): Promise<void> {
  const res = await fetch(breakpointNodeUrl(apiUrl, workflowId, versionId, nodeId), {
    method: "DELETE",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (res.ok || res.status === 404) return;
  throw new Error(`breakpoints request failed (${res.status})`);
}

/**
 * Toggle a breakpoint on/off. Returns a NEW map (input is never mutated).
 *
 * When the node has no existing entry in the map, a breakpoint is created
 * with `enabled: true` and a default `condition: null`.  When it is present,
 * the existing enabled state is flipped and the PUT request is sent to the
 * API — the returned map reflects the new state regardless of success.
 */
export async function toggleBreakpoint(
  current: Map<string, Breakpoint>,
  nodeId: string,
  apiUrl: string,
  accessToken: string,
  workflowId: string,
  versionId: string
): Promise<Map<string, Breakpoint>> {
  // Always build a fresh map — never mutate the caller's reference.
  const next = new Map(current);

  const existing = next.get(nodeId);
  if (existing === undefined) {
    // Absent → enable.
    const created = await setBreakpoint(apiUrl, accessToken, workflowId, versionId, nodeId, {
      enabled: true,
      condition: null,
    });
    next.set(nodeId, created);
  } else {
    // Present → flip.
    const newEnabled = !existing.enabled;
    const updated = await setBreakpoint(apiUrl, accessToken, workflowId, versionId, nodeId, {
      enabled: newEnabled,
      condition: existing.condition,
    });
    next.set(nodeId, updated);
  }

  return next;
}

/**
 * Pure: build a Map from a list of breakpoints, keyed by node_id.
 */
export function breakpointMap(list: Breakpoint[]): Map<string, Breakpoint> {
  const m = new Map<string, Breakpoint>();
  for (const bp of list) {
    m.set(bp.node_id, bp);
  }
  return m;
}

/**
 * Pure: true when the node has an entry AND it is enabled.
 */
export function isBreakpointActive(map: Map<string, Breakpoint>, nodeId: string): boolean {
  const bp = map.get(nodeId);
  return bp !== undefined && bp.enabled;
}
