// Canvas serialisation for the workflow wizard
// (canvas-aware-workflow-wizard §8.1).
//
// Pure function: takes the WorkflowBuilder's React-Flow `nodes` and
// `edges` arrays and returns the server-shaped `canvas` payload the
// wizard endpoint expects. Selection is read from each node's `selected`
// boolean (ReactFlow sets it on click / shift-click / lasso). Edges
// don't carry a selected flag in our usage — UI selects nodes only —
// so `selection.edge_ids` is always empty. The server tolerates an
// empty edge_ids list.
//
// Returns `null` when the canvas is empty (no nodes). The panel passes
// `null` as "omit the canvas field" in the request body, which is how
// the from-scratch path is preserved.

import type { Node, Edge } from "reactflow";

/**
 * Graph-level node kinds the platform recognises (sz-expressiveness §1.1).
 * Anything outside this list is a palette NodeDefinition name and is passed
 * through unchanged; these are accepted on import/save instead of rejected.
 * Mirrors schemas.EXPRESSIVENESS_NODE_KINDS on the API side.
 */
export const EXPRESSIVENESS_NODE_KINDS = [
  "sub_workflow",
  "agent",
  "human_approval",
  "trigger",
  "memory_write_kv",
  "memory_read_kv",
  "memory_embed_and_index",
  "memory_vector_search",
  "memory_timeseries_append",
  "memory_timeseries_query",
  "retry_with_params",
  "conditional",
  "fan_out_over_list",
];

export type ExpressivenessNodeType =
  "sub_workflow" |
  "agent" |
  "human_approval" |
  "trigger" |
  "memory_write_kv" |
  "memory_read_kv" |
  "memory_embed_and_index" |
  "memory_vector_search" |
  "memory_timeseries_append" |
  "memory_timeseries_query" |
  "retry_with_params" |
  "conditional" |
  "fan_out_over_list";

export interface WizardCanvasNodePayload {
  id: string;
  node_definition_name: string | null;
  /** Graph primitive kind (sub_workflow / agent / memory_* / retry_with_params /
   *  ...); null for ordinary palette nodes. */
  node_kind: ExpressivenessNodeType | null;
  label: string | null;
  config: Record<string, unknown> | null;
  position: { x: number; y: number } | null;
}

export interface WizardCanvasEdgePayload {
  id: string;
  source_node_id: string;
  source_port: string | null;
  target_node_id: string;
  target_port: string | null;
}

export interface WizardCanvasPayload {
  selection: {
    node_ids: string[];
    edge_ids: string[];
  };
  graph: {
    nodes: WizardCanvasNodePayload[];
    edges: WizardCanvasEdgePayload[];
  };
}

/**
 * Build the wizard `canvas` payload from a ReactFlow nodes/edges pair.
 * Returns `null` when there are no nodes — the panel uses that to
 * decide whether to ship a `canvas` field at all (omitting preserves
 * the legacy from-scratch path on the server).
 */
export function serialiseCanvasForWizard(
  nodes: ReadonlyArray<Node>,
  edges: ReadonlyArray<Edge>,
): WizardCanvasPayload | null {
  if (!nodes || nodes.length === 0) {
    return null;
  }

  const nodePayload: WizardCanvasNodePayload[] = nodes.map((n) => {
    const data = (n.data || {}) as Record<string, unknown>;
    // The canvas stores nodeDefinitionName on data.label OR
    // data.nodeDefinitionName depending on how the node was created
    // (drag from palette vs. wizard-pasted). Prefer the explicit one.
    const defName =
      (data.nodeDefinitionName as string | undefined) ??
      (typeof data.label === "string" ? data.label : undefined) ??
      null;
    const label = (data.label as string | undefined) ?? null;
    // Stash any user-supplied node configuration under `config`. The
    // canonical key on the WorkflowBuilder side is `data.config`, but
    // we also fall back to data.configuration if a node uses that
    // legacy spelling. Empty objects → null (saves prompt tokens).
    const cfg =
      (data.config as Record<string, unknown> | undefined) ??
      (data.configuration as Record<string, unknown> | undefined) ??
      null;
    const config = cfg && Object.keys(cfg).length > 0 ? cfg : null;
    const position =
      n.position && typeof n.position.x === "number"
        ? { x: n.position.x, y: n.position.y }
        : null;
    const kindRaw =
      (data.nodeKind as string | undefined) ??
      (data.node_kind as string | undefined) ??
      null;
    const nodeKind: ExpressivenessNodeType | null =
      kindRaw && EXPRESSIVENESS_NODE_KINDS.includes(kindRaw)
        ? kindRaw as ExpressivenessNodeType
        : null;
    return {
      id: n.id,
      node_definition_name: defName,
      node_kind: nodeKind,
      label: label,
      config,
      position,
    };
  });

  const edgePayload: WizardCanvasEdgePayload[] = (edges || []).map((e) => ({
    id: e.id,
    source_node_id: e.source,
    source_port: (e.sourceHandle as string | null | undefined) ?? null,
    target_node_id: e.target,
    target_port: (e.targetHandle as string | null | undefined) ?? null,
  }));

  // Selection: ReactFlow sets node.selected on click / shift-click /
  // lasso. Edges have a selected flag too but our UI doesn't expose
  // edge-only selection, so we keep edge_ids empty for now.
  const selectedNodeIds = nodes
    .filter((n) => n.selected === true)
    .map((n) => n.id);

  return {
    selection: {
      node_ids: selectedNodeIds,
      edge_ids: [],
    },
    graph: {
      nodes: nodePayload,
      edges: edgePayload,
    },
  };
}


// ---------------------------------------------------------------------
// Multi-edit component detection (canvas-aware-workflow-wizard §11)
// ---------------------------------------------------------------------
//
// A merge proposal can describe multiple disjoint edits (e.g. an audit
// fix that touches three unrelated regions). To let the user accept or
// reject each region independently, we partition the proposal into
// connected components: nodes connected via non-anchor edges form one
// group; anchored edges + standalone removes are folded into the
// component containing their new-node end.

export interface ProposalNodeForGrouping {
  id: string;
  type?: string;
  data?: { label?: string; nodeDefinitionName?: string; [k: string]: unknown };
}

export interface ProposalEdgeForGrouping {
  id: string;
  source: string;
  target: string;
  anchor_source?: boolean;
  anchor_target?: boolean;
}

export interface ProposalForGrouping {
  apply_mode?: "replace" | "merge";
  nodes: ProposalNodeForGrouping[];
  edges: ProposalEdgeForGrouping[];
  removes?: { node_ids: string[]; edge_ids: string[] } | null;
}

export interface ProposalComponent {
  /** Stable id derived from the sorted node ids in the component, or
   *  from the canvas-removes list when the component is a pure-remove
   *  edit with no new nodes. */
  id: string;
  /** Proposal-local node ids in this component. */
  node_ids: string[];
  /** Proposal-local edge ids in this component. An edge belongs to a
   *  component when AT LEAST ONE of its endpoints is a node in that
   *  component (so anchored edges land in the component containing the
   *  new-node end). Pure anchor-to-anchor edges (both ends are canvas
   *  ids) land in their own component since they have no new nodes. */
  edge_ids: string[];
  /** Canvas node ids the component proposes to remove. We assign all
   *  removes to the FIRST component for now — the LLM rarely emits a
   *  multi-region refactor with more than one removes block, and a
   *  proper assignment would require a tighter contract from the LLM. */
  remove_node_ids: string[];
  remove_edge_ids: string[];
}

/**
 * Partition a merge proposal into connected sub-edits. Returns a single
 * "component" wrapping the whole proposal when apply_mode !== "merge"
 * (replace proposals are atomic) or when the proposal contains a single
 * component (no UI value in toggling a single thing).
 */
export function componentiseProposal(
  proposal: ProposalForGrouping,
): ProposalComponent[] {
  if ((proposal.apply_mode || "replace") !== "merge") {
    return [
      {
        id: "all",
        node_ids: proposal.nodes.map(n => n.id),
        edge_ids: proposal.edges.map(e => e.id),
        remove_node_ids: proposal.removes?.node_ids || [],
        remove_edge_ids: proposal.removes?.edge_ids || [],
      },
    ];
  }

  // Build the proposal's internal graph: edges that connect TWO
  // proposal-local nodes (i.e. not anchored on either end). Anchored
  // edges connect a proposal node to a canvas node; for grouping
  // purposes they're "owned" by the proposal-node end (assigned in
  // pass 2 below). Pure anchor-to-anchor edges (both ends in canvas)
  // are their own component.
  const proposalNodeIds = new Set(proposal.nodes.map(n => n.id));
  const adjacency = new Map<string, Set<string>>();
  for (const n of proposal.nodes) {
    adjacency.set(n.id, new Set());
  }
  for (const e of proposal.edges) {
    const srcInProposal = !e.anchor_source && proposalNodeIds.has(e.source);
    const tgtInProposal = !e.anchor_target && proposalNodeIds.has(e.target);
    if (srcInProposal && tgtInProposal) {
      adjacency.get(e.source)!.add(e.target);
      adjacency.get(e.target)!.add(e.source);
    }
  }

  // Union-find via DFS over node-only adjacency.
  const visited = new Set<string>();
  const components: string[][] = [];
  for (const startId of proposalNodeIds) {
    if (visited.has(startId)) continue;
    const stack = [startId];
    const group: string[] = [];
    while (stack.length) {
      const id = stack.pop()!;
      if (visited.has(id)) continue;
      visited.add(id);
      group.push(id);
      for (const neighbour of adjacency.get(id) || []) {
        if (!visited.has(neighbour)) stack.push(neighbour);
      }
    }
    components.push(group);
  }

  // Assign each edge to a component:
  //   - both proposal-local endpoints → the (single) component owning them
  //   - one anchor side → the component containing the proposal-node end
  //   - both anchor sides → its own component (rare — pure rewire)
  const nodeToComponentIdx = new Map<string, number>();
  components.forEach((g, i) => g.forEach(id => nodeToComponentIdx.set(id, i)));

  const componentEdges: string[][] = components.map(() => []);
  const pureAnchorEdges: ProposalEdgeForGrouping[] = [];
  for (const e of proposal.edges) {
    const srcInProposal = !e.anchor_source && proposalNodeIds.has(e.source);
    const tgtInProposal = !e.anchor_target && proposalNodeIds.has(e.target);
    if (srcInProposal && tgtInProposal) {
      componentEdges[nodeToComponentIdx.get(e.source)!].push(e.id);
    } else if (srcInProposal) {
      componentEdges[nodeToComponentIdx.get(e.source)!].push(e.id);
    } else if (tgtInProposal) {
      componentEdges[nodeToComponentIdx.get(e.target)!].push(e.id);
    } else {
      // Both anchors → its own component.
      pureAnchorEdges.push(e);
    }
  }

  const result: ProposalComponent[] = components.map((nodeIds, i) => ({
    id: nodeIds.slice().sort().join(","),
    node_ids: nodeIds,
    edge_ids: componentEdges[i],
    // First component carries any proposal-level removes — see comment
    // on ProposalComponent.remove_node_ids.
    remove_node_ids: i === 0 ? (proposal.removes?.node_ids || []) : [],
    remove_edge_ids: i === 0 ? (proposal.removes?.edge_ids || []) : [],
  }));

  for (const e of pureAnchorEdges) {
    result.push({
      id: `rewire-${e.id}`,
      node_ids: [],
      edge_ids: [e.id],
      remove_node_ids: [],
      remove_edge_ids: [],
    });
  }

  // If the proposal has removes but no new nodes (pure-remove
  // refactor), components is empty — synthesise a single component
  // for it.
  if (result.length === 0) {
    result.push({
      id: "removes-only",
      node_ids: [],
      edge_ids: [],
      remove_node_ids: proposal.removes?.node_ids || [],
      remove_edge_ids: proposal.removes?.edge_ids || [],
    });
  }

  return result;
}


/**
 * Apply a sub-edit selection to a proposal: returns a NEW proposal with
 * only the accepted components' nodes / edges / removes. Used by the
 * panel before POST /apply to ship a trimmed proposal.
 */
export function applyComponentSelection(
  proposal: ProposalForGrouping,
  components: ProposalComponent[],
  acceptedComponentIds: ReadonlySet<string>,
): ProposalForGrouping {
  const acceptedNodeIds = new Set<string>();
  const acceptedEdgeIds = new Set<string>();
  const acceptedRemoveNodeIds = new Set<string>();
  const acceptedRemoveEdgeIds = new Set<string>();
  for (const c of components) {
    if (!acceptedComponentIds.has(c.id)) continue;
    for (const id of c.node_ids) acceptedNodeIds.add(id);
    for (const id of c.edge_ids) acceptedEdgeIds.add(id);
    for (const id of c.remove_node_ids) acceptedRemoveNodeIds.add(id);
    for (const id of c.remove_edge_ids) acceptedRemoveEdgeIds.add(id);
  }
  return {
    ...proposal,
    nodes: proposal.nodes.filter(n => acceptedNodeIds.has(n.id)),
    edges: proposal.edges.filter(e => acceptedEdgeIds.has(e.id)),
    removes:
      acceptedRemoveNodeIds.size > 0 || acceptedRemoveEdgeIds.size > 0
        ? {
            node_ids: Array.from(acceptedRemoveNodeIds),
            edge_ids: Array.from(acceptedRemoveEdgeIds),
          }
        : null,
  };
}
