/**
 * Tests for componentiseProposal + applyComponentSelection
 * (canvas-aware-workflow-wizard §11).
 *
 * Component detection covers:
 *   - replace mode → single 'all' component (atomic)
 *   - merge mode with 1 connected component → 1 component
 *   - merge mode with 2 disjoint components → 2 components
 *   - anchored edge belongs to its proposal-node end's component
 *   - pure anchor-to-anchor edge becomes its own component (rewire)
 *   - pure-removes proposal (no new nodes) becomes one synthesised component
 *
 * Apply-selection covers:
 *   - all components accepted → identity
 *   - one component rejected → that component's nodes/edges/removes stripped
 *   - all components rejected → empty proposal
 */

import {
  componentiseProposal,
  applyComponentSelection,
  type ProposalForGrouping,
} from "@/lib/wizard-canvas";


describe("§11.1 — componentiseProposal", () => {
  test("replace mode is a single atomic component", () => {
    const p: ProposalForGrouping = {
      apply_mode: "replace",
      nodes: [{ id: "a" }, { id: "b" }, { id: "c" }],
      edges: [{ id: "e1", source: "a", target: "b" }],
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(1);
    expect(comps[0].id).toBe("all");
    expect(comps[0].node_ids).toEqual(["a", "b", "c"]);
  });

  test("merge mode with one connected component", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [{ id: "n1" }, { id: "n2" }],
      edges: [{ id: "e1", source: "n1", target: "n2" }],
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(1);
    expect(new Set(comps[0].node_ids)).toEqual(new Set(["n1", "n2"]));
  });

  test("merge mode with two disjoint components", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [
        { id: "a1" }, { id: "a2" }, // component A
        { id: "b1" }, { id: "b2" }, // component B
      ],
      edges: [
        { id: "ea", source: "a1", target: "a2" },
        { id: "eb", source: "b1", target: "b2" },
      ],
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(2);
    const aComp = comps.find(c => c.node_ids.includes("a1"))!;
    const bComp = comps.find(c => c.node_ids.includes("b1"))!;
    expect(new Set(aComp.node_ids)).toEqual(new Set(["a1", "a2"]));
    expect(new Set(bComp.node_ids)).toEqual(new Set(["b1", "b2"]));
    expect(aComp.edge_ids).toContain("ea");
    expect(bComp.edge_ids).toContain("eb");
  });

  test("anchored edge belongs to its proposal-node end's component", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [{ id: "n1" }],
      edges: [
        // c2 is a canvas anchor; n1 is in the proposal.
        { id: "e1", source: "c2", target: "n1", anchor_source: true },
      ],
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(1);
    expect(comps[0].node_ids).toContain("n1");
    expect(comps[0].edge_ids).toContain("e1");
  });

  test("pure anchor-to-anchor edge is its own rewire component", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [{ id: "n1" }],
      edges: [
        { id: "e1", source: "n1", target: "n1" }, // self-edge to keep n1 referenced
        // Pure rewire: both ends anchor to canvas.
        { id: "rewire", source: "c1", target: "c2",
          anchor_source: true, anchor_target: true },
      ],
    };
    const comps = componentiseProposal(p);
    // 2 components: { n1 + e1 } and { rewire only }
    expect(comps).toHaveLength(2);
    const rewireComp = comps.find(c => c.id.startsWith("rewire-"))!;
    expect(rewireComp.node_ids).toEqual([]);
    expect(rewireComp.edge_ids).toEqual(["rewire"]);
  });

  test("pure-removes proposal (no new nodes) becomes one synthesised component", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [],
      edges: [],
      removes: { node_ids: ["c1"], edge_ids: ["ce1"] },
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(1);
    expect(comps[0].remove_node_ids).toEqual(["c1"]);
    expect(comps[0].remove_edge_ids).toEqual(["ce1"]);
  });

  test("removes ride on the FIRST component when multiple components exist", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [
        { id: "a1" }, { id: "a2" },
        { id: "b1" },
      ],
      edges: [{ id: "ea", source: "a1", target: "a2" }],
      removes: { node_ids: ["c-removed"], edge_ids: [] },
    };
    const comps = componentiseProposal(p);
    expect(comps).toHaveLength(2);
    expect(comps[0].remove_node_ids).toEqual(["c-removed"]);
    expect(comps[1].remove_node_ids).toEqual([]);
  });
});


describe("§11.3 — applyComponentSelection", () => {
  test("all components accepted → identity", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [{ id: "a1" }, { id: "b1" }],
      edges: [],
      removes: { node_ids: ["c-removed"], edge_ids: [] },
    };
    const comps = componentiseProposal(p);
    const accepted = new Set(comps.map(c => c.id));
    const trimmed = applyComponentSelection(p, comps, accepted);
    expect(trimmed.nodes.map(n => n.id)).toEqual(["a1", "b1"]);
    expect(trimmed.removes?.node_ids).toEqual(["c-removed"]);
  });

  test("one component rejected → its nodes/edges stripped", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [
        { id: "a1" }, { id: "a2" },
        { id: "b1" },
      ],
      edges: [
        { id: "ea", source: "a1", target: "a2" },
        { id: "eb", source: "c-anchor", target: "b1", anchor_source: true },
      ],
    };
    const comps = componentiseProposal(p);
    const aComp = comps.find(c => c.node_ids.includes("a1"))!;
    const bComp = comps.find(c => c.node_ids.includes("b1"))!;
    // Reject A.
    const accepted = new Set([bComp.id]);
    const trimmed = applyComponentSelection(p, comps, accepted);
    expect(trimmed.nodes.map(n => n.id)).toEqual(["b1"]);
    expect(trimmed.edges.map(e => e.id)).toEqual(["eb"]);
  });

  test("all components rejected → empty proposal", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [{ id: "a1" }],
      edges: [],
    };
    const comps = componentiseProposal(p);
    const trimmed = applyComponentSelection(p, comps, new Set());
    expect(trimmed.nodes).toEqual([]);
    expect(trimmed.edges).toEqual([]);
  });

  test("rejecting first component drops its removes block", () => {
    const p: ProposalForGrouping = {
      apply_mode: "merge",
      nodes: [
        { id: "a1" },
        { id: "b1" },
      ],
      edges: [],
      removes: { node_ids: ["c-removed"], edge_ids: [] },
    };
    const comps = componentiseProposal(p);
    // First component carries the removes — reject it.
    const firstId = comps[0].id;
    const accepted = new Set(comps.filter(c => c.id !== firstId).map(c => c.id));
    const trimmed = applyComponentSelection(p, comps, accepted);
    expect(trimmed.removes).toBeNull();
  });
});
