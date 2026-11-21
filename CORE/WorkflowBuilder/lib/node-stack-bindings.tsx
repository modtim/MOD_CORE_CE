"use client";

// generalized-stack-wake D12 — canvas overlay data model.
//
// A managed ("persistent") stack (a NIM/VLM/SDXL/LLM service or a generic
// long-lived container group) can back one or more node kinds. This module
// carries the API's node_kind -> stack binding map down to the canvas nodes
// via React context, and maps the stack's live wake-state to a badge colour.
//
// The map is fetched by hooks/useNodeStackBindings.ts (through the shared
// poller) and provided at the WorkflowBuilder level; each node reads only its
// own binding via useNodeStackBinding(node.data.node_kind). A node whose kind
// is not in the map gets no binding and therefore no overlay.

import { createContext, useContext, type ReactNode } from "react";

export type StackFamily = "nim" | "vlm" | "sdxl" | "llm" | "generic";
export type StackState = "running" | "asleep" | "waking" | "waiting" | "unknown";

export interface StackBinding {
  stack_name: string;
  family: StackFamily;
  state: StackState;
}

// node_kind -> the stack that backs it. Mirrors the API contract at
// GET /persistent-stacks/node-bindings { bindings: { <node_kind>: {...} } }.
export type NodeBindingsMap = Record<string, StackBinding>;

// stack_name -> the stack's family + live wake-state. Mirrors the `stacks` key
// of the same node-bindings response { stacks: { <stack_name>: {family,state} } }.
// Used to resolve a per-instance data.requires_stack assignment to a live state
// (the assignment names a stack, not a node_kind, so it's looked up here).
export interface StackInfo {
  family: StackFamily;
  state: StackState;
}
export type StackInfoMap = Record<string, StackInfo>;

export interface StackStateStyle {
  // Tailwind classes for the small state dot + text, and whether it pulses.
  dotClass: string;
  textClass: string;
  pulse: boolean;
  label: string;
}

// State -> colour. running=green, waking/waiting=amber (waiting pulses to read
// as "still coming up"), asleep=grey, unknown=muted. Anything unrecognised
// falls through to the muted "unknown" style so a new server state can't throw.
const STATE_STYLES: Record<StackState, StackStateStyle> = {
  running: { dotClass: "bg-green-500", textClass: "text-green-600", pulse: false, label: "running" },
  waking: { dotClass: "bg-amber-500", textClass: "text-amber-600", pulse: true, label: "waking" },
  waiting: { dotClass: "bg-amber-500", textClass: "text-amber-600", pulse: true, label: "waiting" },
  asleep: { dotClass: "bg-gray-400", textClass: "text-gray-500", pulse: false, label: "asleep" },
  unknown: { dotClass: "bg-gray-300", textClass: "text-gray-400", pulse: false, label: "unknown" },
};

export function stackStateStyle(state: StackState | string | undefined): StackStateStyle {
  return STATE_STYLES[(state as StackState)] ?? STATE_STYLES.unknown;
}

interface NodeStackContextValue {
  bindings: NodeBindingsMap;
  stacks: StackInfoMap;
}

const NodeStackBindingsContext = createContext<NodeStackContextValue>({
  bindings: {},
  stacks: {},
});

export function NodeStackBindingsProvider({
  value,
  stacks,
  children,
}: {
  // The node_kind -> binding map (auto-bindings). Named `value` for back-compat
  // with existing callers/tests that provided just the bindings map.
  value: NodeBindingsMap;
  // The stack_name -> {family,state} map, so a per-instance requires_stack
  // assignment can be resolved to a live state. Optional — absent = {}.
  stacks?: StackInfoMap;
  children: ReactNode;
}) {
  return (
    <NodeStackBindingsContext.Provider
      value={{ bindings: value ?? {}, stacks: stacks ?? {} }}
    >
      {children}
    </NodeStackBindingsContext.Provider>
  );
}

// Resolves the effective managed-stack binding for a node, or null when the node
// is neither assigned to a stack nor its kind auto-bound (the common case — most
// nodes get the muted overlay).
//
// Precedence: a per-instance `requiresStack` assignment (from the config panel)
// wins over the node_kind auto-binding. The assignment names a stack, so its
// live family/state are looked up in the `stacks` map; when the stack isn't in
// that map yet (not deployed, or a poll hasn't landed) the badge still shows the
// assigned name with an "unknown" state rather than disappearing.
export function useNodeStackBinding(
  nodeKind: string | undefined,
  requiresStack?: string,
): StackBinding | null {
  const { bindings, stacks } = useContext(NodeStackBindingsContext);
  if (requiresStack) {
    const info = stacks[requiresStack];
    return {
      stack_name: requiresStack,
      family: info?.family ?? "generic",
      state: info?.state ?? "unknown",
    };
  }
  if (!nodeKind) return null;
  return bindings[nodeKind] ?? null;
}
