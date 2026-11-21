/**
 * Tests for the wizard panel's apply-error surfaces
 * (canvas-aware-workflow-wizard §13).
 *
 * Covers:
 *   §13.1 — stale_canvas 422 surfaces a clear "canvas changed" message
 *           and DOES NOT load anything onto the canvas (onApplied not
 *           called); the missing_anchor_node_ids list is mentioned
 *           when present
 *   §13.2 — dangling_reference 422 surfaces a clear "would orphan
 *           edges" message; lists the dangling edge ids; doesn't load
 *   §13.3 — missing_nodes still triggers the recovery UI in merge
 *           mode (back-compat path still works)
 *   §13.x — successful merge passes apply_mode=='merge' to onApplied
 *           so the parent can keep the panel open
 */

import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";
import type { WizardCanvasPayload } from "@/lib/wizard-canvas";


let mockResponses: Array<{ status?: number; body: any }> = [];
let lastRequest: { url: string; body: any } | null = null;

function mockFetch() {
  return jest.fn(async (url: string, init?: RequestInit) => {
    const next = mockResponses.shift();
    const body = init?.body ? JSON.parse(init.body as string) : null;
    lastRequest = { url, body };
    if (!next) {
      return {
        ok: true, status: 200,
        json: async () => ({}),
        text: async () => "",
      } as Response;
    }
    const status = next.status ?? 200;
    return {
      ok: status >= 200 && status < 300, status,
      json: async () => next.body,
      text: async () => JSON.stringify(next.body),
    } as Response;
  });
}

beforeEach(() => {
  mockResponses = [];
  lastRequest = null;
  // @ts-ignore
  global.fetch = mockFetch();
});


function buildCanvas(): WizardCanvasPayload {
  return {
    selection: { node_ids: ["c1"], edge_ids: [] },
    graph: {
      nodes: [
        { id: "c1", node_definition_name: "Resize", label: "Resize", config: null, position: null },
        { id: "c2", node_definition_name: "MakeMask", label: "MakeMask", config: null, position: null },
      ],
      edges: [],
    },
  };
}

async function generateAndApply(
  rest: { onApplied?: jest.Mock; proposal: any; onAllMissingDispatched?: jest.Mock },
) {
  // First mock response = generate; second = apply.
  const onApplied = rest.onApplied || jest.fn();
  const onAllMissingDispatched = rest.onAllMissingDispatched || jest.fn();
  mockResponses.push({
    body: {
      session_id: "s1",
      response: "Here you go.",
      intent: "suggest_next",
      proposal: rest.proposal,
    },
  });

  render(
    <WorkflowWizardPanel
      open={true}
      onClose={() => {}}
      accessToken="t"
      getCanvas={() => buildCanvas()}
      currentWorkflowId={42}
      currentVersionId={7}
      onApplied={onApplied}
      onAllMissingDispatched={onAllMissingDispatched}
    />,
  );

  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "extend" } });
  fireEvent.submit(input.closest("form")!);
  await waitFor(
    () => {
      expect(screen.getByRole("button", { name: /Merge into canvas|Apply to canvas/i })).toBeInTheDocument();
    },
    { timeout: 20000 },
  );

  return onApplied;
}


describe("§13.1 — stale_canvas error surface", () => {
  test(
    "surfaces clear 'canvas changed' message and lists missing anchors",
    async () => {
    const onApplied = await generateAndApply({
      proposal: {
        name: "ext", apply_mode: "merge",
        nodes: [{ id: "new1", type: "process",
                  data: { nodeDefinitionName: "MakeMask" } }],
        edges: [{ id: "e1", source: "ghost", target: "new1",
                   anchor_source: true }],
      },
    });
    // Apply call returns 422 stale_canvas.
    mockResponses.push({
      status: 422,
      body: {
        detail: {
          error: "stale_canvas",
          message: "stale",
          missing_anchor_node_ids: ["ghost"],
        },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Merge into canvas/i }));

    // Message renders both in the error banner AND as an assistant turn,
    // so use getAllByText. Both surfaces is intended (banner draws the
    // eye; turn is part of the persistent transcript).
    await waitFor(
      () => {
        expect(screen.getAllByText(/canvas changed/i).length).toBeGreaterThan(0);
      },
      { timeout: 20000 },
    );
    expect(screen.getAllByText(/ghost/i).length).toBeGreaterThan(0);
    // No load: onApplied not called.
    expect(onApplied).not.toHaveBeenCalled();
    },
    60000,
  );
});


describe("§13.2 — dangling_reference error surface", () => {
  test("surfaces clear 'would orphan' message and lists dangling edge ids", async () => {
    const onApplied = await generateAndApply({
      proposal: {
        name: "refactor", apply_mode: "merge",
        nodes: [], edges: [],
        removes: { node_ids: ["c2"], edge_ids: [] },
      },
    });
    mockResponses.push({
      status: 422,
      body: {
        detail: {
          error: "dangling_reference",
          message: "x",
          edges: [{ edge_id: "ce1", source: "c1", target: "c2" }],
        },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Merge into canvas/i }));

    await waitFor(
      () => {
        expect(screen.getAllByText(/orphan canvas edge/i).length).toBeGreaterThan(0);
      },
      { timeout: 20000 },
    );
    expect(screen.getAllByText(/ce1/).length).toBeGreaterThan(0);
    expect(onApplied).not.toHaveBeenCalled();
  });
});


describe("§13.3 — successful merge calls onApplied with apply_mode='merge'", () => {
  test("merge success forwards apply_mode so parent can keep panel open", async () => {
    const onApplied = await generateAndApply({
      proposal: {
        name: "ext", apply_mode: "merge",
        nodes: [{ id: "new1", type: "process",
                  data: { nodeDefinitionName: "MakeMask" } }],
        edges: [{ id: "e1", source: "c1", target: "new1",
                   anchor_source: true }],
      },
    });
    mockResponses.push({
      body: {
        workflow_id: 42,
        version_id: 99,
        name: "ext",
        apply_mode: "merge",
        applied: {
          added_node_ids: ["new1"],
          added_edge_ids: ["e1"],
          removed_node_ids: [],
          removed_edge_ids: [],
        },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Merge into canvas/i }));

    await waitFor(() => expect(onApplied).toHaveBeenCalled(), { timeout: 20000 });
    // 4th arg is apply_mode.
    expect(onApplied).toHaveBeenCalledWith(42, 99, "ext", "merge");
    // Apply request body included workflow_id+version_id (§8.3).
    expect(lastRequest!.body).toMatchObject({
      workflow_id: 42,
      version_id: 7,
    });
  });

  test("replace success defaults apply_mode='replace' (back-compat)", async () => {
    const onApplied = await generateAndApply({
      proposal: {
        name: "p", apply_mode: "replace",
        nodes: [
          { id: "n1", type: "input", data: { label: "in" } },
          { id: "n2", type: "process", data: { nodeDefinitionName: "X" } },
          { id: "n3", type: "output", data: { label: "out" } },
        ],
        edges: [],
      },
    });
    mockResponses.push({
      body: {
        workflow_id: 1, version_id: 1, name: "p",
        // No apply_mode in response — old server. Client must default to 'replace'.
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Apply to canvas/i }));
    await waitFor(() => expect(onApplied).toHaveBeenCalled(), { timeout: 20000 });
    expect(onApplied).toHaveBeenCalledWith(1, 1, "p", "replace");
  });
});


describe("§13.x — missing_nodes still triggers recovery UI", () => {
  test("missing_nodes 422 in merge proposal shows recovery panel", async () => {
    await generateAndApply({
      proposal: {
        name: "ext", apply_mode: "merge",
        nodes: [{ id: "new1", type: "process",
                  data: { nodeDefinitionName: "Hallucinated" } }],
        edges: [{ id: "e1", source: "c1", target: "new1",
                   anchor_source: true }],
      },
    });
    mockResponses.push({
      status: 422,
      body: {
        detail: {
          error: "missing_nodes",
          missing: ["Hallucinated"],
        },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Merge into canvas/i }));
    await waitFor(
      () => {
        // Recovery UI surfaces with the "Build all missing nodes" affordance.
        expect(screen.getByRole("button", { name: /Build all missing nodes/i })).toBeInTheDocument();
      },
      { timeout: 20000 },
    );
    // Name appears in the recovery panel AND in the diff preview's
    // Added section (since the proposal still references it).
    expect(screen.getAllByText(/Hallucinated/).length).toBeGreaterThan(0);
  });
});
