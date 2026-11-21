"use client";

// generalized-stack-wake D12 — the on-node overlay badge.
//
// Rendered on EVERY canvas node (all node types). When the node's node_kind is
// tied to a managed stack it shows a stack icon + a state dot coloured by the
// stack's live wake-state, with a tooltip naming the stack + state. When the node
// is NOT stack-backed it shows a MUTED indicator ("no managed stack") so the
// stack status is visible platform-wide — the user asked for it on every node,
// not only the stack-backed ones.
//
// Placement mirrors the existing per-node badges: an absolutely-positioned
// corner element on the node card. The corner status dot lives top-right, so
// this sits top-left to avoid collision.

import { Layers } from "lucide-react";
import { useNodeStackBinding, stackStateStyle } from "@/lib/node-stack-bindings";

export function NodeStackOverlay({
  nodeKind,
  requiresStack,
}: {
  nodeKind?: string;
  // Per-instance assignment (data.requires_stack). Takes precedence over the
  // node_kind auto-binding so the badge reflects what the user tied THIS node to.
  requiresStack?: string;
}) {
  const binding = useNodeStackBinding(nodeKind, requiresStack);

  // Unbound: still render a muted badge so EVERY node shows its stack status.
  if (!binding) {
    return (
      <div
        data-testid="node-stack-overlay"
        data-stack-state="none"
        title="No managed stack — self-contained node"
        className="absolute top-1 left-1 z-10 flex items-center gap-1 rounded-full bg-white/70 border border-gray-200 px-1.5 py-0.5 pointer-events-auto opacity-50"
        tabIndex={-1}
      >
        <Layers className="h-3 w-3 text-gray-300" />
      </div>
    );
  }

  const style = stackStateStyle(binding.state);

  return (
    <div
      data-testid="node-stack-overlay"
      data-stack-name={binding.stack_name}
      data-stack-state={binding.state}
      title={`Requires stack: ${binding.stack_name} · ${style.label}`}
      className="absolute top-1 left-1 z-10 flex items-center gap-1 rounded-full bg-white/90 border border-gray-200 shadow-sm px-1.5 py-0.5 pointer-events-auto"
    >
      <Layers className="h-3 w-3 text-gray-500" />
      <span
        data-testid="node-stack-dot"
        className={`inline-block h-2 w-2 rounded-full ${style.dotClass} ${style.pulse ? "animate-pulse" : ""}`}
      />
    </div>
  );
}

export default NodeStackOverlay;
