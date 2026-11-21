// FX.20 R-038 — canvas accessibility announcements.
//
// The workflow canvas is visually driven (compose → run with no keyboard or
// screen-reader path). The builder renders a single visually-hidden
// `aria-live="polite"` region (id WB_CANVAS_LIVE_REGION_ID) and pushes short
// human-readable sentences into it when things happen: run lifecycle,
// per-node status transitions, and node add/delete/connect actions.
//
// Keep this module pure (no React) so the sentence builders are unit-testable
// without a canvas.

export const WB_CANVAS_LIVE_REGION_ID = "wb-canvas-live-region";

const RUN_STATUS_SENTENCES: Record<string, string> = {
  queued: "Run started, queued",
  running: "Run started",
  pending: "Run started, pending",
  success: "Run finished successfully",
  completed: "Run finished successfully",
  failure: "Run failed",
  failed: "Run failed",
  cancelled: "Run cancelled",
  canceled: "Run cancelled",
  blocked: "Run stopped — nodes are blocked",
};

/**
 * Sentence to announce when the active run's status changes to `status`.
 * Unknown statuses still get a sentence (the live region must say *something*
 * rather than stay silent on a lifecycle change).
 */
export function describeRunStatusChange(status: string | null | undefined): string {
  if (!status) return "Run state cleared";
  return RUN_STATUS_SENTENCES[status] ?? `Run status: ${status}`;
}

/**
 * Sentence for a single node's status transition, e.g.
 * `"Code: Upscale succeeded"` / `"Split: failed"`.
 */
export function describeNodeStatusChange(label: string | null | undefined, status: string | null | undefined): string {
  const name = label || "Node";
  return `${name}: ${status}`;
}

export function describeNodeAdded(label: string | null | undefined): string {
  return `Added node: ${label || "node"}`;
}

export function describeNodeDeleted(label: string | null | undefined): string {
  return `Deleted node: ${label || "node"}`;
}

export function describeNodesDeleted(count: number): string {
  return count === 1 ? "Deleted 1 node" : `Deleted ${count} nodes`;
}

export function describeEdgeConnected(sourceLabel: string | null | undefined, targetLabel: string | null | undefined): string {
  return `Connected ${sourceLabel || "node"} to ${targetLabel || "node"}`;
}

export function describeEdgeDisconnected(sourceLabel: string | null | undefined, targetLabel: string | null | undefined): string {
  return `Disconnected ${sourceLabel || "node"} from ${targetLabel || "node"}`;
}

// React Flow v11 gives a focusable node wrapper (nodesFocusable) a native
// role="button" + aria-label, so the canvas a11y contract for every custom
// node is: focusable wrapper from React Flow, and `role="group"` +
// aria-label="<type>: <label>" on the node card itself. These helpers build
// that label for each node type.
export function nodeAriaLabel(type: string | null | undefined, label: string | null | undefined): string {
  const name = label || type || "node";
  return type ? `${type}: ${name}` : name;
}

/**
 * Sentence for the run's overall result once every tracked node is terminal.
 * `nodeStatuses` are the per-node statuses the canvas is showing; `runStatus`
 * (the polled run record, when the caller has one) wins when it is terminal, so
 * a run the API settles as `cancelled` says so out loud rather than being
 * inferred from its nodes — a user-cancelled run stamps its in-flight nodes
 * FAILED with "Cancelled by user" (CORE/API/routers/workflows_domains/
 * workflows.py:2684-2690), which alone would read as a plain failure.
 * Returns "" when the run has not settled yet, so the caller stays silent.
 */
export function describeTerminalRunStatus(nodeStatuses: string[], runStatus: string | null | undefined): string {
  const TERMINAL_NODES = new Set(["success", "completed", "failure", "failed", "blocked", "cancelled", "canceled"]);
  if (nodeStatuses.length === 0 || !nodeStatuses.every((s) => TERMINAL_NODES.has(s))) return "";
  const WORDS: Record<string, string> = {
    success: "success", completed: "success",
    failure: "failed", failed: "failed",
    blocked: "blocked",
    cancelled: "cancelled", canceled: "cancelled",
  };
  const runWord = runStatus ? WORDS[runStatus] : undefined;
  const worst = runWord
    ?? (nodeStatuses.some((s) => s === "failure") ? "failed"
      : nodeStatuses.some((s) => s === "blocked") ? "blocked"
      : nodeStatuses.some((s) => s === "cancelled" || s === "canceled") ? "cancelled"
      : "success");
  if (worst === "success") return "Run finished successfully";
  return `Run finished with status ${worst}`;
}

/**
 * Track a set of (key → status) pairs and emit the sentences for every status
 * that newly appeared since the last call — used for per-node run statuses,
 * which are polled as a full list each tick. Pass a fresh snapshot each poll;
 * returns [] when nothing changed.
 */
export function diffStatuses(
  prev: Map<string, string> | undefined,
  next: Array<{ id: string; label: string; status: string }>,
): string[] {
  const out: string[] = [];
  const seen = new Map<string, string>();
  for (const item of next) {
    seen.set(item.id, item.status);
    const old = prev?.get(item.id);
    if (old !== item.status) {
      out.push(describeNodeStatusChange(item.label, item.status));
    }
  }
  return out;
}
