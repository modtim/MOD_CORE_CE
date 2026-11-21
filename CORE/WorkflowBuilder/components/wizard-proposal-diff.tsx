"use client";

// Diff preview for wizard proposals
// (canvas-aware-workflow-wizard §10).
//
// Two render modes branching on proposal.apply_mode:
//   - "replace" — today's behavior (single confirm-and-build card with
//     a flat node list).
//   - "merge"   — three labelled sections: Added (new nodes/edges),
//     Removed (proposal.removes — strikethrough red), Anchors (existing
//     canvas node ids the proposal connects to). Removed section is
//     visually distinct so destructive intent is unambiguous.
//
// Pure render component; no fetch / state. Caller (the panel) provides
// the proposal + a lookup of the live canvas so anchor ids can resolve
// to readable names. Apply / Revise buttons live in the panel; this
// component only renders the diff body.

import React from "react";
import type { WizardCanvasPayload } from "@/lib/wizard-canvas";
import { componentiseProposal, type ProposalComponent } from "@/lib/wizard-canvas";

export interface DiffProposalNode {
  id: string;
  type?: string;
  data?: {
    label?: string;
    nodeDefinitionName?: string;
    [k: string]: unknown;
  };
}

export interface DiffProposalEdge {
  id: string;
  source: string;
  target: string;
  anchor_source?: boolean;
  anchor_target?: boolean;
}

export interface DiffProposal {
  name: string;
  nodes: DiffProposalNode[];
  edges: DiffProposalEdge[];
  apply_mode?: "replace" | "merge";
  removes?: {
    node_ids: string[];
    edge_ids: string[];
  } | null;
}

interface Props {
  proposal: DiffProposal;
  /** Live canvas (or null when generating from scratch) — used to
   *  resolve anchor ids to readable names in the Anchors section. */
  canvas?: WizardCanvasPayload | null;
  /** Multi-edit accept/reject (canvas-aware-workflow-wizard §11). When
   *  the proposal partitions into 2+ disjoint components AND the
   *  caller passes both `acceptedComponentIds` + `onToggleComponent`,
   *  the diff renders each component as a separately-toggleable
   *  group. Otherwise renders the merge proposal as a single block.
   *  No-op for replace proposals (they're atomic). */
  acceptedComponentIds?: ReadonlySet<string>;
  onToggleComponent?: (componentId: string) => void;
}

/** Look up a canvas node's display label by id. Falls back to the
 *  node id when the canvas is not available or the id is unknown. */
function canvasNodeLabel(
  canvas: WizardCanvasPayload | null | undefined,
  id: string,
): string {
  if (!canvas) return id;
  const n = canvas.graph.nodes.find(x => x.id === id);
  if (!n) return id;
  return n.label || n.node_definition_name || n.id;
}

function nodeDisplayLabel(n: DiffProposalNode): string {
  return (
    (n.data?.label as string | undefined) ||
    (n.data?.nodeDefinitionName as string | undefined) ||
    n.type ||
    "node"
  );
}

export const WizardProposalDiff: React.FC<Props> = ({
  proposal, canvas, acceptedComponentIds, onToggleComponent,
}) => {
  const isMerge = proposal.apply_mode === "merge";

  if (!isMerge) {
    // Replace mode — today's flat list.
    return (
      <div data-testid="wizard-proposal-diff" data-mode="replace">
        <div className="text-xs text-gray-500 mb-2">
          {proposal.nodes.length} nodes ·{" "}
          {proposal.edges.length} connections
        </div>
        <ul className="text-xs text-gray-700 space-y-0.5 mb-2">
          {proposal.nodes.map(n => (
            <li key={n.id}>
              <span className="font-mono text-[11px] text-gray-400">{n.id}</span>{" "}
              {nodeDisplayLabel(n)}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  // Merge mode. If the caller wired multi-edit toggles AND the
  // proposal partitions into 2+ disjoint components, render each as
  // its own toggleable section (canvas-aware-workflow-wizard §11);
  // otherwise render the proposal as a single Added/Removed/Anchors
  // block.
  const components = (acceptedComponentIds && onToggleComponent)
    ? componentiseProposal(proposal)
    : null;
  const showMultiEdit = !!(components && components.length >= 2);

  if (showMultiEdit) {
    return (
      <div data-testid="wizard-proposal-diff" data-mode="merge" className="space-y-3">
        <div className="text-[11px] text-gray-600 italic" data-testid="multi-edit-hint">
          {components!.length} disjoint changes — toggle the ones you want to apply
        </div>
        {components!.map(c => {
          const accepted = acceptedComponentIds!.has(c.id);
          // Build a per-component sub-proposal for the inline diff
          // render. Removes only ride on the first component (see
          // componentiseProposal).
          const compNodes = proposal.nodes.filter(n => c.node_ids.includes(n.id));
          const compEdges = proposal.edges.filter(e => c.edge_ids.includes(e.id));
          const compProposal: DiffProposal = {
            name: proposal.name, apply_mode: "merge",
            nodes: compNodes, edges: compEdges,
            removes: (c.remove_node_ids.length || c.remove_edge_ids.length)
              ? { node_ids: c.remove_node_ids, edge_ids: c.remove_edge_ids }
              : null,
          };
          return (
            <section
              key={c.id}
              data-testid="diff-component"
              data-component-id={c.id}
              data-accepted={accepted}
              className={
                "border rounded-md p-2 " +
                (accepted ? "border-indigo-300 bg-indigo-50/30" : "border-gray-200 bg-gray-50 opacity-60")
              }
            >
              <label className="flex items-center gap-2 text-[11px] font-medium text-gray-700 mb-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={accepted}
                  onChange={() => onToggleComponent!(c.id)}
                  data-testid={`component-toggle-${c.id}`}
                  className="rounded"
                />
                <span>
                  Change {compNodes.length > 0
                    ? `(${compNodes.length} new node${compNodes.length === 1 ? "" : "s"})`
                    : c.remove_node_ids.length > 0
                      ? `(remove ${c.remove_node_ids.length})`
                      : ""}
                </span>
              </label>
              <WizardProposalDiff
                proposal={compProposal}
                canvas={canvas}
              />
            </section>
          );
        })}
      </div>
    );
  }

  const removes = proposal.removes ?? { node_ids: [], edge_ids: [] };

  // Anchors: every distinct canvas node id referenced by an anchored
  // edge. We deduplicate so each canvas node only shows once even if
  // many proposal edges connect to it.
  const anchorIds = new Set<string>();
  for (const e of proposal.edges) {
    if (e.anchor_source && e.source) anchorIds.add(e.source);
    if (e.anchor_target && e.target) anchorIds.add(e.target);
  }
  const anchorList = Array.from(anchorIds);

  // New edges that are NOT pure anchor-to-anchor (those are
  // visually represented by the Anchors section + the Added nodes
  // they connect; pure anchor-to-anchor with no new node is rare
  // but still valid — render it under Added with both endpoints
  // labelled as canvas nodes).
  const addedEdges = proposal.edges;

  return (
    <div data-testid="wizard-proposal-diff" data-mode="merge" className="space-y-3">
      {/* Added section */}
      {(proposal.nodes.length > 0 || addedEdges.length > 0) && (
        <section data-testid="diff-added">
          <div className="text-[11px] font-semibold text-emerald-700 uppercase tracking-wide mb-1">
            Added
          </div>
          {proposal.nodes.length > 0 && (
            <ul className="text-xs text-gray-700 space-y-0.5 mb-1">
              {proposal.nodes.map(n => (
                <li key={n.id} className="flex items-baseline gap-1.5">
                  <span className="text-emerald-600 select-none" aria-hidden>+</span>
                  <span className="font-mono text-[11px] text-gray-400">{n.id}</span>
                  <span>{nodeDisplayLabel(n)}</span>
                </li>
              ))}
            </ul>
          )}
          {addedEdges.length > 0 && (
            <ul className="text-xs text-gray-600 space-y-0.5 ml-4">
              {addedEdges.map(e => {
                const srcLabel = e.anchor_source ? canvasNodeLabel(canvas, e.source) : e.source;
                const tgtLabel = e.anchor_target ? canvasNodeLabel(canvas, e.target) : e.target;
                return (
                  <li key={e.id} className="font-mono text-[10.5px]">
                    <span className="text-emerald-600 select-none" aria-hidden>+</span>{" "}
                    {srcLabel} → {tgtLabel}
                    {e.anchor_source && (
                      <span className="ml-1 text-[10px] text-indigo-600">(anchor)</span>
                    )}
                    {e.anchor_target && !e.anchor_source && (
                      <span className="ml-1 text-[10px] text-indigo-600">(anchor)</span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}

      {/* Removed section — visually distinct so destructive intent
          is unambiguous (canvas-aware-workflow-wizard §10.1). */}
      {(removes.node_ids.length > 0 || removes.edge_ids.length > 0) && (
        <section data-testid="diff-removed">
          <div className="text-[11px] font-semibold text-rose-700 uppercase tracking-wide mb-1">
            Removed
          </div>
          {removes.node_ids.length > 0 && (
            <ul className="text-xs space-y-0.5">
              {removes.node_ids.map(id => (
                <li key={id} className="flex items-baseline gap-1.5">
                  <span className="text-rose-600 select-none" aria-hidden>−</span>
                  <span className="font-mono text-[11px] text-gray-400 line-through">{id}</span>
                  <span className="line-through text-rose-700">
                    {canvasNodeLabel(canvas, id)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {removes.edge_ids.length > 0 && (
            <ul className="text-xs ml-4 space-y-0.5">
              {removes.edge_ids.map(id => (
                <li key={id} className="font-mono text-[10.5px] text-rose-700 line-through">
                  <span className="text-rose-600 select-none" aria-hidden>−</span>{" "}
                  edge {id}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* Anchors section — references the user can see on the canvas. */}
      {anchorList.length > 0 && (
        <section data-testid="diff-anchors">
          <div className="text-[11px] font-semibold text-indigo-700 uppercase tracking-wide mb-1">
            Anchors (existing canvas nodes)
          </div>
          <ul className="text-xs text-gray-700 space-y-0.5">
            {anchorList.map(id => (
              <li key={id} className="flex items-baseline gap-1.5">
                <span className="text-indigo-600 select-none" aria-hidden>•</span>
                <span className="font-mono text-[11px] text-gray-400">{id}</span>
                <span>{canvasNodeLabel(canvas, id)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
};

export default WizardProposalDiff;
