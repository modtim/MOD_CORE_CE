/**
 * Tests for the WizardProposalDiff component
 * (canvas-aware-workflow-wizard §10).
 *
 * Covers:
 *   - replace mode renders a flat list (back-compat with today's UI)
 *   - merge mode renders three labelled sections: Added / Removed / Anchors
 *   - empty Removed section is hidden (no destructive content shown)
 *   - empty Anchors section is hidden
 *   - Removed items render with strikethrough + rose color (destructive)
 *   - Anchor labels resolve via the live canvas snapshot
 *   - missing canvas falls back to anchor id verbatim
 */

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { WizardProposalDiff, DiffProposal } from "@/components/wizard-proposal-diff";
import type { WizardCanvasPayload } from "@/lib/wizard-canvas";

function canvas(nodes: Array<{ id: string; name: string }>): WizardCanvasPayload {
  return {
    selection: { node_ids: [], edge_ids: [] },
    graph: {
      nodes: nodes.map(n => ({
        id: n.id,
        node_definition_name: n.name,
        label: n.name,
        config: null,
        position: null,
      })),
      edges: [],
    },
  };
}


describe("§10 — replace mode", () => {
  test("renders a flat list with node count + connections", () => {
    const proposal: DiffProposal = {
      name: "p",
      nodes: [
        { id: "n1", type: "input", data: { label: "in" } },
        { id: "n2", type: "process", data: { label: "Resize" } },
      ],
      edges: [{ id: "e1", source: "n1", target: "n2" }],
    };
    render(<WizardProposalDiff proposal={proposal} />);
    expect(screen.getByTestId("wizard-proposal-diff")).toHaveAttribute("data-mode", "replace");
    expect(screen.getByText(/2 nodes/)).toBeInTheDocument();
    expect(screen.getByText(/1 connections/)).toBeInTheDocument();
    expect(screen.getByText(/Resize/)).toBeInTheDocument();
  });

  test("explicit apply_mode='replace' also renders flat list", () => {
    const proposal: DiffProposal = {
      name: "p", apply_mode: "replace",
      nodes: [{ id: "n1", type: "input", data: { label: "in" } }],
      edges: [],
    };
    render(<WizardProposalDiff proposal={proposal} />);
    expect(screen.getByTestId("wizard-proposal-diff")).toHaveAttribute("data-mode", "replace");
  });
});


describe("§10 — merge mode sections", () => {
  test("Added section renders new nodes + edges", () => {
    const proposal: DiffProposal = {
      name: "extend", apply_mode: "merge",
      nodes: [
        { id: "new1", type: "process", data: { label: "MakeMask" } },
      ],
      edges: [
        { id: "newE1", source: "c2", target: "new1", anchor_source: true },
      ],
    };
    render(
      <WizardProposalDiff proposal={proposal} canvas={canvas([{ id: "c2", name: "Resize" }])} />,
    );
    const added = screen.getByTestId("diff-added");
    expect(added).toBeInTheDocument();
    expect(added).toHaveTextContent(/MakeMask/);
    // Anchor edge resolves canvas node id 'c2' to its display label 'Resize'.
    expect(added).toHaveTextContent(/Resize/);
    expect(added).toHaveTextContent(/anchor/);
  });

  test("Removed section renders only when removes block is non-empty", () => {
    const proposal: DiffProposal = {
      name: "refactor", apply_mode: "merge",
      nodes: [],
      edges: [],
      removes: { node_ids: ["c2"], edge_ids: ["ce1"] },
    };
    render(
      <WizardProposalDiff proposal={proposal} canvas={canvas([{ id: "c2", name: "Resize" }])} />,
    );
    const removed = screen.getByTestId("diff-removed");
    expect(removed).toBeInTheDocument();
    expect(removed).toHaveTextContent(/Resize/); // canvas label resolved
    expect(removed).toHaveTextContent(/edge ce1/);
  });

  test("Removed section is hidden when removes is empty", () => {
    const proposal: DiffProposal = {
      name: "ext", apply_mode: "merge",
      nodes: [{ id: "new1", type: "process", data: { label: "X" } }],
      edges: [],
      removes: null,
    };
    render(<WizardProposalDiff proposal={proposal} />);
    expect(screen.queryByTestId("diff-removed")).toBeNull();
  });

  test("Removed entries have strikethrough styling (destructive)", () => {
    const proposal: DiffProposal = {
      name: "refactor", apply_mode: "merge",
      nodes: [], edges: [],
      removes: { node_ids: ["c2"], edge_ids: [] },
    };
    const { container } = render(
      <WizardProposalDiff proposal={proposal} canvas={canvas([{ id: "c2", name: "Old" }])} />,
    );
    expect(container.querySelector(".line-through")).toBeInTheDocument();
  });

  test("Anchors section renders unique canvas ids referenced by anchored edges", () => {
    const proposal: DiffProposal = {
      name: "fill", apply_mode: "merge",
      nodes: [{ id: "new1", type: "process", data: { label: "Bridge" } }],
      edges: [
        { id: "e1", source: "c1", target: "new1", anchor_source: true },
        { id: "e2", source: "new1", target: "c2", anchor_target: true },
      ],
    };
    render(
      <WizardProposalDiff
        proposal={proposal}
        canvas={canvas([
          { id: "c1", name: "ImgIn" },
          { id: "c2", name: "ImgOut" },
        ])}
      />,
    );
    const anchors = screen.getByTestId("diff-anchors");
    expect(anchors).toHaveTextContent(/ImgIn/);
    expect(anchors).toHaveTextContent(/ImgOut/);
  });

  test("Anchors section is hidden when no edges are anchored", () => {
    const proposal: DiffProposal = {
      name: "p", apply_mode: "merge",
      nodes: [{ id: "new1", type: "process", data: { label: "X" } }],
      edges: [],
    };
    render(<WizardProposalDiff proposal={proposal} />);
    expect(screen.queryByTestId("diff-anchors")).toBeNull();
  });

  test("Anchor falls back to id when canvas not provided", () => {
    const proposal: DiffProposal = {
      name: "ext", apply_mode: "merge",
      nodes: [{ id: "new1", type: "process", data: { label: "X" } }],
      edges: [
        { id: "e1", source: "c-mystery", target: "new1", anchor_source: true },
      ],
    };
    render(<WizardProposalDiff proposal={proposal} canvas={null} />);
    const anchors = screen.getByTestId("diff-anchors");
    expect(anchors).toHaveTextContent(/c-mystery/);
  });

  test("multi-component proposal renders separately-toggleable sections when caller wires toggles", () => {
    const proposal: DiffProposal = {
      name: "two-component", apply_mode: "merge",
      nodes: [
        { id: "a1", type: "process", data: { label: "A1" } },
        { id: "b1", type: "process", data: { label: "B1" } },
      ],
      edges: [],  // no edges → two disjoint single-node components
    };
    const accepted = new Set(["a1", "b1"]);
    const onToggle = jest.fn();
    const { container } = render(
      <WizardProposalDiff
        proposal={proposal}
        acceptedComponentIds={accepted}
        onToggleComponent={onToggle}
      />,
    );
    // Multi-edit hint appears with the disjoint-count.
    expect(screen.getByTestId("multi-edit-hint")).toHaveTextContent(/2 disjoint/);
    // Two component sections rendered.
    expect(container.querySelectorAll("[data-testid='diff-component']").length).toBe(2);
  });

  test("multi-component renders flat (no toggles) when caller doesn't wire onToggleComponent", () => {
    const proposal: DiffProposal = {
      name: "two-component", apply_mode: "merge",
      nodes: [
        { id: "a1", type: "process", data: { label: "A1" } },
        { id: "b1", type: "process", data: { label: "B1" } },
      ],
      edges: [],
    };
    render(<WizardProposalDiff proposal={proposal} />);
    expect(screen.queryByTestId("multi-edit-hint")).toBeNull();
  });

  test("clicking a component toggle invokes onToggleComponent", () => {
    const proposal: DiffProposal = {
      name: "two", apply_mode: "merge",
      nodes: [
        { id: "a1", type: "process", data: { label: "A1" } },
        { id: "b1", type: "process", data: { label: "B1" } },
      ],
      edges: [],
    };
    const accepted = new Set(["a1", "b1"]);
    const onToggle = jest.fn();
    render(
      <WizardProposalDiff
        proposal={proposal}
        acceptedComponentIds={accepted}
        onToggleComponent={onToggle}
      />,
    );
    fireEvent.click(screen.getByTestId("component-toggle-a1"));
    expect(onToggle).toHaveBeenCalledWith("a1");
  });

  test("rejected component appears with reduced visual emphasis", () => {
    const proposal: DiffProposal = {
      name: "two", apply_mode: "merge",
      nodes: [
        { id: "a1", type: "process", data: { label: "A1" } },
        { id: "b1", type: "process", data: { label: "B1" } },
      ],
      edges: [],
    };
    // a1 accepted, b1 rejected (omitted from accepted set).
    const accepted = new Set(["a1"]);
    const { container } = render(
      <WizardProposalDiff
        proposal={proposal}
        acceptedComponentIds={accepted}
        onToggleComponent={jest.fn()}
      />,
    );
    const sections = container.querySelectorAll("[data-testid='diff-component']");
    const a1Section = Array.from(sections).find(s => s.getAttribute("data-component-id") === "a1");
    const b1Section = Array.from(sections).find(s => s.getAttribute("data-component-id") === "b1");
    expect(a1Section).toHaveAttribute("data-accepted", "true");
    expect(b1Section).toHaveAttribute("data-accepted", "false");
  });

  test("repeated anchor references deduplicate", () => {
    const proposal: DiffProposal = {
      name: "p", apply_mode: "merge",
      nodes: [
        { id: "new1", type: "process", data: { label: "A" } },
        { id: "new2", type: "process", data: { label: "B" } },
      ],
      edges: [
        { id: "e1", source: "c1", target: "new1", anchor_source: true },
        { id: "e2", source: "c1", target: "new2", anchor_source: true },
      ],
    };
    render(
      <WizardProposalDiff
        proposal={proposal}
        canvas={canvas([{ id: "c1", name: "Source" }])}
      />,
    );
    const anchors = screen.getByTestId("diff-anchors");
    // 'Source' appears exactly once in the Anchors section.
    const matches = anchors.querySelectorAll("li");
    expect(matches.length).toBe(1);
    expect(anchors).toHaveTextContent(/Source/);
  });
});
