"use client";
import { apiUrl } from "@/lib/runtime-config";

// GE.WQR.13 — Wizard Queue client (wizard-queue-redesign / launch-completion
// megaplan). Single source of truth for the WorkflowBuilder panel's wire
// contract against `CORE/API/routers/wizard_queue.py`, mirrored (field-for-
// field) by the Dashboard's `js/wizard-queue.js` vanilla port (GE.WQR.22).
//
// Every request goes through `authenticatedFetch` (@/lib/api-client) so
// silent token-refresh / connection-lost banner behaviour is shared with
// the rest of the app — no raw `fetch()` here except inside the SSE
// EventSource, which cannot carry an Authorization header (the API accepts
// `?access_token=` on the stream endpoint for exactly that reason).
//
// Types below mirror `schemas.WizardRunResponse` / `WizardRunListResponse`
// / `WizardRunLogLine` / `WizardRunLogsResponse` in CORE/API/schemas.py —
// read there, don't guess, if the backend contract changes.

import { authenticatedFetch, mintStreamTicket } from "@/lib/api-client";
import { pollErrorForResponse } from "@/lib/poller";

const DEFAULT_API_URL =
  apiUrl();

// ---------------------------------------------------------------------
// Wire types (models.WizardRunType / WizardRunStatus / WizardRunOrigin —
// values are the kebab-case wire contract per models.py's own comment).
// ---------------------------------------------------------------------
export type WizardRunType =
  | "node"
  | "stack"
  | "agent-project"
  | "build-repair"
  | "container-adapter";

export type WizardRunStatus =
  | "queued"
  | "waiting-for-worker"
  | "running"
  | "verifying"
  | "waiting-for-user"
  | "succeeded"
  | "failed"
  | "canceled";

export type WizardRunOrigin = "system" | "platform-admin" | "tenant-user";

// Mirrors models.WIZARD_TERMINAL_STATUSES / WIZARD_RETRYABLE_STATUSES.
// The row/section should prefer the server-computed `can_cancel` /
// `can_retry` flags on the run itself; these sets are exported for any
// caller that needs the raw classification (e.g. tests).
export const WIZARD_TERMINAL_STATUSES: ReadonlySet<WizardRunStatus> = new Set([
  "succeeded",
  "failed",
  "canceled",
]);
export const WIZARD_RETRYABLE_STATUSES: ReadonlySet<WizardRunStatus> = new Set([
  "failed",
  "canceled",
]);

// schemas.WizardRunResponse — the shared wire contract rendered
// identically by the WorkflowBuilder panel and the Dashboard Activity view.
export interface WizardRun {
  id: string;
  wizard_type: WizardRunType;
  target_name: string | null;
  // Artifact back-link ("open produced artifact"): a type + id pair, e.g.
  // target_ref_type="task"/"node"/"stack"/"workflow". There is no single
  // `artifact_ref` field on the wire — that's shorthand in the task spec
  // for this type/id pair (see models.py WizardRun comment "Artifact
  // back-link").
  target_ref_type: string | null;
  target_ref_id: string | null;
  status: WizardRunStatus;
  progress: number;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  error_message: string | null;
  owner_user_id: number | null;
  tenant_id: number | null;
  is_platform_level: boolean;
  origin: WizardRunOrigin;
  retry_of_id: string | null;
  parent_run_id: string | null;
  log_source: string | null;
  created_at: string;
  updated_at: string;
  // Defence-in-depth hints computed server-side per caller (router
  // `_to_response`); the server still enforces on the action endpoints.
  can_cancel: boolean;
  can_retry: boolean;
  // Client-side-only presentation hints the Wizard Queue section may stamp
  // onto a run before rendering (NOT part of the wire response, hence
  // optional): whether the `retry_of_id` back-link resolves to a run the
  // panel can reveal, and whether this row is currently highlighted
  // (deep-link / retry-of navigation). Absent in server data.
  originalRunAvailable?: boolean;
  highlighted?: boolean;
}

export interface WizardRunListResponse {
  runs: WizardRun[];
  total_count: number;
}

// schemas.WizardRunLogLine — a persisted (poll-fallback) log line.
export interface WizardRunLogLine {
  seq: number;
  level: string | null;
  message: string;
  created_at: string;
}

export interface WizardRunLogsResponse {
  run_id: string;
  lines: WizardRunLogLine[];
  last_seq: number;
}

// A live SSE line, published fire-and-forget by
// services/wizard_queue.py::_publish_log_event. Unlike the persisted
// WizardRunLogLine it carries no `created_at` (see router.py stream_logs /
// _publish_log_event — the payload is exactly {seq, level, message}).
export interface WizardRunStreamLine {
  seq: number;
  level: string | null;
  message: string;
}

// ---------------------------------------------------------------------
// Type / status label maps (shared by WorkflowBuilder row/section and,
// per the same field mapping, the Dashboard's js/wizard-queue.js port).
// ---------------------------------------------------------------------
export const WIZARD_TYPE_LABEL: Record<WizardRunType, string> = {
  node: "Node Wizard",
  stack: "Stack Wizard",
  "agent-project": "Agent / Project",
  "build-repair": "Build Repair",
  "container-adapter": "Container Adapter",
};

export const WIZARD_STATUS_LABEL: Record<WizardRunStatus, string> = {
  queued: "Queued",
  "waiting-for-worker": "Waiting for worker",
  running: "Running",
  verifying: "Verifying",
  "waiting-for-user": "Waiting for you",
  succeeded: "Succeeded",
  failed: "Failed",
  canceled: "Canceled",
};

// ---------------------------------------------------------------------
// Defensive scope filter (client-side belt-and-suspenders only — the
// REAL gate is server-side: services/wizard_queue.py::scope_query +
// can_access, D5/D15/D17). A platform admin sees platform-level runs
// ONLY (never a tenant's runs, per the "platform-level-only" rule); a
// tenant admin sees their tenant's non-platform-level runs; a plain
// user sees only their own runs.
//
// WorkflowBuilder has no local `/me`-style identity call today (it only
// ever decodes the JWT `sub` claim — see lib/build-queue-namespace.ts —
// which is a Keycloak UUID, not the DB `owner_user_id`/`tenant_id`/
// `is_platform_admin` this comparison actually needs). So `me` is
// OPTIONAL and best-effort: when the caller can't supply it, this
// returns true (trust the server's scoping) rather than hiding rows the
// server already decided the caller may see. It only actively hides a
// row when it has positive evidence of a scope mismatch.
export interface WizardQueueViewer {
  userId?: number | string | null;
  tenantId?: number | string | null;
  isPlatformAdmin?: boolean;
}

export function isOwnScope(
  run: WizardRun,
  me?: WizardQueueViewer | null,
): boolean {
  if (!me) return true;

  if (me.isPlatformAdmin) {
    // platform-level-only rule: even platform admins never see tenant
    // rows through this filter.
    return run.is_platform_level === true;
  }
  if (run.is_platform_level) {
    // Non-platform-admins never see platform-level runs.
    return false;
  }
  if (me.tenantId != null && run.tenant_id != null) {
    return String(me.tenantId) === String(run.tenant_id);
  }
  if (me.userId != null && run.owner_user_id != null) {
    return String(me.userId) === String(run.owner_user_id);
  }
  // Not enough information to compare — don't hide it.
  return true;
}

// ---------------------------------------------------------------------
// API
// ---------------------------------------------------------------------
const PAGE_SIZE = 20;

function url(apiUrl: string, path: string): string {
  return `${apiUrl.replace(/\/+$/, "")}${path}`;
}

/** Role-scoped, paginated list — GET /wizard-queue?limit=20&offset=. */
export async function listWizardRuns(
  page: number,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRunListResponse> {
  const offset = Math.max(0, page) * PAGE_SIZE;
  const resp = await authenticatedFetch(
    url(apiUrl, `/wizard-queue?limit=${PAGE_SIZE}&offset=${offset}`),
  );
  const perr = pollErrorForResponse(resp);
  if (perr) throw perr;
  return (await resp.json()) as WizardRunListResponse;
}

/** Single-run detail — GET /wizard-queue/{id}. 404s (not 403s) for a run
 *  the caller cannot access, per the router's `_require_access`. */
export async function getWizardRun(
  id: string,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRun> {
  const resp = await authenticatedFetch(
    url(apiUrl, `/wizard-queue/${encodeURIComponent(id)}`),
  );
  if (!resp.ok) throw new Error(`fetch run failed (${resp.status})`);
  return (await resp.json()) as WizardRun;
}

/**
 * Bell notification deep-links land as `#wizard-queue/<run id>`. Parse that
 * shape out of a window hash (e.g. `#wizard-queue/abc-123`) so the WB can open
 * the Wizard Queue panel and focus the run. Returns the run id, or null when
 * the hash is not a wizard-queue link.
 */
export function parseWizardQueueHash(hash: string): string | null {
  const m = /^#?wizard-queue\/([A-Za-z0-9._-]+)/.exec(hash || "");
  return m ? m[1] : null;
}

/**
 * A run's artifact back-link is a `(target_ref_type, target_ref_id)` pair.
 * Only some pairs point at a place the UI can actually navigate to today:
 *   - "project" (agent-project runs) → the in-app Project Wizard panel
 *     (target_ref_id is the session id).
 *   - "task" (container-adapter runs) → the Dashboard System → Workers tab,
 *     where ApiTaskQueue rows + their logs are viewable.
 * Every other ref type that appears on a non-node run (build_spec,
 * stack-prefill — whose ref id is null, node, workflow) has no dedicated view
 * to open, so it returns null and the row HIDES the Open button rather than
 * rendering a no-op affordance.
 */
export type WizardArtifactTarget =
  | { kind: "project"; id: string }
  | { kind: "worker-task"; id: string };

export function wizardArtifactTarget(
  refType: string | null,
  refId: string | null,
): WizardArtifactTarget | null {
  if (!refType || !refId) return null;
  // CE: no Project Wizard, so a project ref has no view to open ()
  if (refType === "task") return { kind: "worker-task", id: refId };
  return null;
}

/** Persisted logs (poll fallback) — GET /wizard-queue/{id}/logs. */
export async function getWizardRunLogs(
  id: string,
  after?: number,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRunLogsResponse> {
  const q = typeof after === "number" ? `?after_seq=${after}` : "";
  const resp = await authenticatedFetch(
    url(apiUrl, `/wizard-queue/${encodeURIComponent(id)}/logs${q}`),
  );
  const perr = pollErrorForResponse(resp);
  if (perr) throw perr;
  return (await resp.json()) as WizardRunLogsResponse;
}

/**
 * Live-tail a run's logs. Opens an EventSource against
 * `/wizard-queue/{id}/logs/stream` (auth via `?ticket=` — WS5.34b: a
 * short-lived, single-use ticket minted via `POST /stream-tickets` right
 * before connecting, since EventSource can't set headers and the old
 * `?access_token=<JWT>` scheme leaked the JWT into proxy access logs). On
 * any SSE error, falls back to polling `getWizardRunLogs` every 2s (self-
 * rescheduling via setTimeout, matching lib/poller.ts's own idiom — not
 * setInterval) picking up from the last seq seen. Returns a `close()` that
 * tears down whichever transport is active; callers MUST call it on
 * collapse/unmount.
 */
export function streamWizardRunLogs(
  id: string,
  onLine: (line: WizardRunStreamLine) => void,
  apiUrl: string = DEFAULT_API_URL,
): () => void {
  let closed = false;
  let es: EventSource | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let lastSeq = -1;

  const clearPollTimer = () => {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  };

  const pollTick = async () => {
    if (closed) return;
    try {
      const resp = await getWizardRunLogs(id, lastSeq, apiUrl);
      for (const line of resp.lines) {
        lastSeq = Math.max(lastSeq, line.seq);
        onLine({ seq: line.seq, level: line.level, message: line.message });
      }
    } catch {
      // best-effort — try again on the next tick
    }
    if (!closed) {
      pollTimer = setTimeout(pollTick, 2000);
    }
  };

  const fallBackToPolling = () => {
    if (closed || pollTimer) return;
    if (es) {
      try {
        es.close();
      } catch {
        /* ignore */
      }
      es = null;
    }
    pollTimer = setTimeout(pollTick, 0);
  };

  const connect = async () => {
    if (closed) return;
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      fallBackToPolling();
      return;
    }
    // Mint a fresh, single-use ticket for THIS connection attempt — it
    // cannot be reused if this EventSource later errors and we reconnect.
    const ticket = await mintStreamTicket(apiUrl, "wizard-run", id);
    if (closed) return;
    if (!ticket) {
      fallBackToPolling();
      return;
    }
    const streamUrl = url(
      apiUrl,
      `/wizard-queue/${encodeURIComponent(id)}/logs/stream?ticket=${encodeURIComponent(ticket)}`,
    );
    try {
      es = new EventSource(streamUrl);
      es.addEventListener("message", (evt) => {
        if (closed) return;
        try {
          const data = JSON.parse(
            (evt as MessageEvent).data,
          ) as WizardRunStreamLine;
          if (typeof data.seq === "number") lastSeq = Math.max(lastSeq, data.seq);
          onLine(data);
        } catch {
          /* ignore a malformed line */
        }
      });
      es.addEventListener("end", () => {
        try {
          es?.close();
        } catch {
          /* ignore */
        }
      });
      // A ticket is single-use — on error, don't let the browser's native
      // EventSource retry reuse the same (now-consumed) ticket. Falling
      // back to polling here (rather than minting a new ticket and
      // reopening immediately) also matches the existing backoff-free-zone
      // rule: one SSE failure retires this connect() call to the 2s poll,
      // never a tight EventSource reconnect loop against the API.
      es.addEventListener("error", fallBackToPolling);
    } catch {
      fallBackToPolling();
    }
  };

  connect();

  return () => {
    closed = true;
    clearPollTimer();
    if (es) {
      try {
        es.close();
      } catch {
        /* ignore */
      }
      es = null;
    }
  };
}

async function postRun(
  id: string,
  action: "cancel" | "retry" | "dismiss",
  apiUrl: string,
): Promise<WizardRun> {
  const resp = await authenticatedFetch(
    url(apiUrl, `/wizard-queue/${encodeURIComponent(id)}/${action}`),
    { method: "POST" },
  );
  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = body?.detail ? `: ${body.detail}` : "";
    } catch {
      /* ignore */
    }
    throw new Error(`${action} failed (${resp.status})${detail}`);
  }
  return (await resp.json()) as WizardRun;
}

/** HARD cancel (+ cascade to children) — POST /wizard-queue/{id}/cancel. */
export function cancelWizardRun(
  id: string,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRun> {
  return postRun(id, "cancel", apiUrl);
}

/** Retry as a NEW linked run — POST /wizard-queue/{id}/retry. */
export function retryWizardRun(
  id: string,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRun> {
  return postRun(id, "retry", apiUrl);
}

/** Soft-delete one terminal run — POST /wizard-queue/{id}/dismiss. */
export function dismissWizardRun(
  id: string,
  apiUrl: string = DEFAULT_API_URL,
): Promise<WizardRun> {
  return postRun(id, "dismiss", apiUrl);
}

/** Soft-delete every terminal, in-scope run — POST /wizard-queue/clear-completed. */
export async function clearCompletedWizardRuns(
  apiUrl: string = DEFAULT_API_URL,
): Promise<{ cleared: number }> {
  const resp = await authenticatedFetch(
    url(apiUrl, "/wizard-queue/clear-completed"),
    { method: "POST" },
  );
  if (!resp.ok) {
    throw new Error(`clear-completed failed (${resp.status})`);
  }
  return (await resp.json()) as { cleared: number };
}
