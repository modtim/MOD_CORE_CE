// Shared run-state → node border colour for canvas nodes (node-run-controls).
//
// Each node keeps its kind's default border (its `getStatusColor()`); this
// overlays the live RUN state on top: a paused breakpoint turns the node yellow,
// and the in-run action states get their own colour. Keyed off node.data
// (`paused` boolean + `status` string). Anything not a recognised run-state
// falls through to the node's default border, preserving the existing look for
// normal running/success/queued (the corner status dot already conveys those).

export function nodeStateBorderClass(
  data: { paused?: boolean; status?: string; control?: string } | undefined,
  fallback: string,
): string {
  // Operator CONTROL state (from /node-controls) wins over execution status —
  // that's why these paint even while the node's status dot says running/success.
  const control = (data?.control || "").toLowerCase();
  if (data?.paused || control === "paused") return "border-yellow-400";
  if (control === "force-failed" || control === "force_failed") return "border-red-500";
  if (control === "re-running" || control === "rerunning") return "border-blue-500";
  if (control === "draining") return "border-amber-500";
  if (control === "skipped") return "border-gray-400";

  // Execution status fallbacks (also accept these on `status` directly).
  const s = (data?.status || "").toLowerCase();
  switch (s) {
    case "re-running":
    case "rerunning":
      return "border-blue-500";
    case "draining":
      return "border-amber-500";
    case "skipped":
      return "border-gray-400";
    case "failure":
    case "failed":
    case "force-failed":
    case "force_failed":
      return "border-red-500";
    default:
      return fallback;
  }
}
