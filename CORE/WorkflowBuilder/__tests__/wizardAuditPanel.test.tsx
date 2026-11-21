/**
 * Tests for the wizard's Audit panel mode
 * (canvas-aware-workflow-wizard §12).
 *
 * Covers:
 *   §12.1 — Chat / Audit tabs surface only after the first audit;
 *           switching tabs preserves state (transcript still there
 *           when returning from Audit; findings still there when
 *           returning from Chat)
 *   §12.2 — findings render grouped by severity (error → warning →
 *           info); each gets its action buttons (Ignore, Fix via
 *           wizard, Open node) per suggested_action
 *   §12.3 — Fix via wizard switches to Chat tab + prefills composer
 *           with fix_prompt + fires a turn
 *   §12.4 — audit findings persist across panel close + reopen
 *           within a session (panel state survives because the
 *           parent only toggles the open prop)
 *   §12.5 — ignored finding stays hidden; clean-graph audit shows
 *           "no findings"
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

const sampleAuditResponse = {
  session_id: "s1",
  response: "Found 2 issues.",
  intent: "audit_graph",
  audit: {
    summary: "Graph has type mismatches and an unreachable node.",
    findings: [
      {
        id: "f-error-1",
        severity: "error",
        title: "type mismatch",
        detail: "n1 produces image but n3 accepts mask",
        affected_node_ids: ["c1"],
        affected_edge_ids: ["e2"],
        suggested_action: "fix_via_wizard",
        fix_prompt: "convert image to mask between c1 and c2",
      },
      {
        id: "f-warning-1",
        severity: "warning",
        title: "unreachable node",
        detail: "c2 has no incoming edge",
        affected_node_ids: ["c2"],
        affected_edge_ids: [],
        suggested_action: "open_node",
        fix_prompt: null,
      },
    ],
  },
};

async function runAudit(opts: { onSelectCanvasNode?: jest.Mock; onClose?: jest.Mock } = {}) {
  mockResponses.push({ body: sampleAuditResponse });
  const onClose = opts.onClose || jest.fn();
  const onSelectCanvasNode = opts.onSelectCanvasNode || jest.fn();
  const result = render(
    <WorkflowWizardPanel
      open={true}
      onClose={onClose}
      accessToken="t"
      getCanvas={() => buildCanvas()}
      onSelectCanvasNode={onSelectCanvasNode}
    />,
  );
  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "audit this" } });
  fireEvent.submit(input.closest("form")!);
  await waitFor(() => {
    // Audit tab should appear once findings arrive.
    expect(screen.getByRole("tab", { name: /Audit/i })).toBeInTheDocument();
  });
  return { ...result, onClose, onSelectCanvasNode };
}


describe("§12.1 — tabs and state preservation", () => {
  test("tabs are hidden until the first audit response arrives", () => {
    render(
      <WorkflowWizardPanel
        open={true}
        onClose={() => {}}
        accessToken="t"
        getCanvas={() => buildCanvas()}
      />,
    );
    expect(screen.queryByRole("tab", { name: /Audit/i })).toBeNull();
  });

  test("audit response auto-switches into Audit mode and shows findings", async () => {
    await runAudit();
    expect(screen.getByTestId("audit-mode-panel")).toBeInTheDocument();
    expect(screen.getAllByTestId("audit-finding").length).toBe(2);
  });

  test("switching back to Chat preserves the transcript", async () => {
    await runAudit();
    fireEvent.click(screen.getByRole("tab", { name: /Chat/i }));
    // Transcript still shows the user turn ("audit this") and the
    // assistant turn ("Found 2 issues.") from the audit run.
    expect(screen.getByText(/Found 2 issues/i)).toBeInTheDocument();
    expect(screen.queryByTestId("audit-mode-panel")).toBeNull();
  });

  test("switching back to Audit preserves the findings", async () => {
    await runAudit();
    fireEvent.click(screen.getByRole("tab", { name: /Chat/i }));
    fireEvent.click(screen.getByRole("tab", { name: /Audit/i }));
    expect(screen.getAllByTestId("audit-finding").length).toBe(2);
  });
});


describe("§12.2 — findings grouped by severity with per-action buttons", () => {
  test("findings render grouped under their severity headers", async () => {
    await runAudit();
    expect(screen.getByTestId("audit-section-error")).toBeInTheDocument();
    expect(screen.getByTestId("audit-section-warning")).toBeInTheDocument();
  });

  test("fix_via_wizard finding gets a 'Fix via wizard' button", async () => {
    await runAudit();
    expect(screen.getByTestId("audit-action-fix-f-error-1")).toBeInTheDocument();
  });

  test("open_node finding gets an 'Open node' button when handler present", async () => {
    await runAudit();
    expect(screen.getByTestId("audit-action-open-f-warning-1")).toBeInTheDocument();
  });

  test("every finding gets an Ignore button regardless of suggested_action", async () => {
    await runAudit();
    expect(screen.getByTestId("audit-action-ignore-f-error-1")).toBeInTheDocument();
    expect(screen.getByTestId("audit-action-ignore-f-warning-1")).toBeInTheDocument();
  });
});


describe("§12.3 — Fix via wizard action", () => {
  test("clicking 'Fix via wizard' switches to Chat tab and fires a turn", async () => {
    await runAudit();
    // Queue the next response so the auto-fired turn doesn't 200 with empty body.
    mockResponses.push({
      body: { session_id: "s1", response: "On it.", intent: "fill_gap" },
    });
    fireEvent.click(screen.getByTestId("audit-action-fix-f-error-1"));

    // Switched to Chat tab.
    await waitFor(() => {
      expect(screen.queryByTestId("audit-mode-panel")).toBeNull();
    });
    // Request fired with the fix_prompt as message.
    await waitFor(() => {
      expect(lastRequest).not.toBeNull();
    });
    expect(lastRequest!.body.message).toBe("convert image to mask between c1 and c2");
  });
});


describe("§12.4 — close + reopen preserves findings", () => {
  test("audit findings persist when the parent closes and reopens the panel", async () => {
    const { rerender } = await runAudit();
    rerender(
      <WorkflowWizardPanel open={false} onClose={() => {}} accessToken="t" getCanvas={() => buildCanvas()} />,
    );
    rerender(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" getCanvas={() => buildCanvas()} />,
    );
    // Audit tab still visible — findings preserved on the panel's
    // state (Dialog visibility doesn't unmount the children).
    expect(screen.getByRole("tab", { name: /Audit/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Audit/i }));
    expect(screen.getAllByTestId("audit-finding").length).toBe(2);
  });
});


describe("§12.5 — ignore + clean graph + open_node", () => {
  test("clicking Ignore hides that finding only", async () => {
    await runAudit();
    fireEvent.click(screen.getByTestId("audit-action-ignore-f-error-1"));
    expect(screen.queryByTestId("audit-action-ignore-f-error-1")).toBeNull();
    // Other finding still visible.
    expect(screen.getByTestId("audit-action-ignore-f-warning-1")).toBeInTheDocument();
  });

  test("clean graph audit shows 'No findings to review'", async () => {
    mockResponses.push({
      body: {
        session_id: "s1",
        response: "Clean.",
        intent: "audit_graph",
        audit: { summary: "No issues found.", findings: [] },
      },
    });
    render(
      <WorkflowWizardPanel
        open={true}
        onClose={() => {}}
        accessToken="t"
        getCanvas={() => buildCanvas()}
      />,
    );
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "audit" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => {
      expect(screen.getByTestId("audit-empty")).toBeInTheDocument();
    });
  });

  test("Open node calls onSelectCanvasNode with affected id and closes panel", async () => {
    const { onClose, onSelectCanvasNode } = await runAudit();
    fireEvent.click(screen.getByTestId("audit-action-open-f-warning-1"));
    expect(onSelectCanvasNode).toHaveBeenCalledWith("c2");
    expect(onClose).toHaveBeenCalled();
  });

  test("Ignored findings reset when 'New conversation' clears state", async () => {
    await runAudit();
    fireEvent.click(screen.getByTestId("audit-action-ignore-f-error-1"));
    expect(screen.queryByTestId("audit-action-ignore-f-error-1")).toBeNull();
    // Click "New conversation".
    fireEvent.click(screen.getByText(/New conversation/i));
    // Audit tab should be gone (latestAudit cleared).
    expect(screen.queryByRole("tab", { name: /Audit/i })).toBeNull();
  });
});
