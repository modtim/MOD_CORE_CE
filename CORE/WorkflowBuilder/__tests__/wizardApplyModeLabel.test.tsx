/**
 * Apply-button labelling by `proposal.apply_mode`
 * (canvas-aware-workflow-wizard §10.2).
 *
 * Salvaged from `wizardIntentButtons.test.tsx`, which was deleted 2026-08-12
 * because the §9 intent buttons it covered were deliberately removed by the
 * zero-thought wizard (`2ce8d0ce`). This case did NOT go with them: the
 * apply-mode labelling survived at `workflow-wizard-panel.tsx:1666,1690`, and
 * §10.2 named the deleted file as its only verifier. Deleting the suite
 * wholesale would have silently dropped it.
 *
 * Why it earns its own file rather than being dropped: "Apply to canvas"
 * REPLACES the canvas and "Merge into canvas" ADDS to it. That is a
 * destructive-versus-additive distinction carried entirely by the button's
 * words, and the two paths also send different request bodies — a merge
 * carries workflow_id + version_id so the server knows what to merge into.
 * If the label stops tracking the mode, the button lies about which one is
 * about to happen.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { WorkflowWizardPanel } from '@/components/workflow-wizard-panel';
import type { WizardCanvasPayload } from '@/lib/wizard-canvas';

let mockResponses: Array<{ status?: number; body: any }> = [];

function mockFetch() {
  return jest.fn(async (_url: string, _init?: RequestInit) => {
    const next = mockResponses.shift();
    if (!next) {
      return {
        ok: true, status: 200,
        json: async () => ({ session_id: "stub", response: "ok" }),
        text: async () => "ok",
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
  // @ts-ignore
  global.fetch = mockFetch();
});

function buildCanvas(opts: { nodes: number; selected?: number }): WizardCanvasPayload | null {
  if (opts.nodes === 0) return null;
  const nodes = Array.from({ length: opts.nodes }, (_, i) => ({
    id: `c${i}`,
    node_definition_name: "Resize",
    label: "Resize",
    config: null,
    position: null,
  }));
  return {
    selection: {
      node_ids: Array.from({ length: opts.selected ?? 0 }, (_, i) => `c${i}`),
      edge_ids: [],
    },
    graph: { nodes, edges: [] },
  };
}

function proposalTurn(applyMode?: "replace" | "merge") {
  return {
    body: {
      session_id: "s1",
      response: "Here.",
      intent: "suggest_next",
      proposal: {
        name: "P",
        ...(applyMode ? { apply_mode: applyMode } : {}),
        nodes: [{ id: "new1", type: "process", data: { nodeDefinitionName: "X" } }],
        edges: [],
      },
    },
  };
}

async function renderTurn(applyMode?: "replace" | "merge") {
  mockResponses.push(proposalTurn(applyMode));
  render(
    <WorkflowWizardPanel
      open={true}
      onClose={() => {}}
      accessToken="t"
      getCanvas={() => buildCanvas({ nodes: 2, selected: 1 })}
    />,
  );
  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "extend" } });
  fireEvent.submit(input.closest("form")!);
}

describe("apply button label tracks apply_mode", () => {
  test("merge proposals offer 'Merge into canvas' and never 'Apply to canvas'", async () => {
    await renderTurn("merge");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Merge into canvas/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /^Apply to canvas$/i })).toBeNull();
  });

  test("replace proposals offer 'Apply to canvas'", async () => {
    await renderTurn("replace");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Apply to canvas/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /Merge into canvas/i })).toBeNull();
  });

  test("a proposal with no apply_mode defaults to replace, not merge", async () => {
    // The panel reads `apply_mode || "replace"`. Defaulting the other way
    // would make an older or partial server response silently additive.
    await renderTurn(undefined);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Apply to canvas/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /Merge into canvas/i })).toBeNull();
  });
});
