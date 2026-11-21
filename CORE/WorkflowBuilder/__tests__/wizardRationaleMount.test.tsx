import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

// EXPR 5.7 — Mount WizardRationale inside the wizard panel.
// SSE/stream mocking harness: jsdom ships no TextEncoder/TextDecoder and no
// ReadableStream on mocked Responses, so we fake the reader ourselves.
Object.assign(global, { TextEncoder, TextDecoder });

let requests: Array<{ url: string; body: any; method?: string }> = [];
let mockResponses: Array<{ status?: number; body?: any; events?: any[] }> = [];

function sseResponse(events: any[]) {
  const encoder = new TextEncoder();
  let i = 0;
  const reader = {
    read: async () => {
      if (i < events.length) {
        const s = `data: ${JSON.stringify(events[i])}\n\n`;
        i += 1;
        return { value: encoder.encode(s), done: false };
      }
      return { value: undefined, done: true };
    },
    cancel: async () => { /* no-op */ },
    releaseLock: () => { /* no-op */ },
  };
  return {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as Response;
}

function mockFetch() {
  return jest.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(init.body as string) : null;
    requests.push({ url, body, method: init?.method });
    const next = mockResponses.shift();
    if (!next) return { ok: true, status: 200, json: async () => ({}) } as Response;
    if (next.events) return sseResponse(next.events);
    const st = next.status ?? 200;
    return {
      ok: st >= 200 && st < 300,
      status: st,
      json: async () => next.body,
      text: async () => JSON.stringify(next.body ?? {}),
    } as Response;
  });
}

const FINAL = {
  type: "final",
  session_id: "s1",
  response: "The run died at the upscale node.",
  proposal: null,
  intent: null,
  sources: [],
  missing_nodes: [],
  node_build_handoffs: [],
  audit: null,
};

const BACKEND_ENTRY = { step: "critic-iter-1", rationale: "Approved: licence-compatible for on-prem." };
const DOC_ENTRY = {
  decision_point: "Upscale model",
  chosen_option: "Real-ESRGAN",
  alternatives_considered: ["4x-UltraSharp", "Topaz"],
  reason: "Best licence for on-prem tenants.",
};

function rationaleEvents(rationale: any): any[] {
  return [
    { type: "builder_started", step: "builder" },
    { type: "retry", step: "retry" },
    { type: "final", response: "Done", rationale },
  ];
}

beforeEach(() => {
  requests = [];
  mockResponses = [];
  // @ts-ignore
  global.fetch = mockFetch();
});

describe("EXPR 5.7 — WizardRationale mounted in wizard panel", () => {
  test("final event with backend-shape rationale → Why this workflow? toggle present", async () => {
    mockResponses.push({ events: rationaleEvents([BACKEND_ENTRY]) });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.getByTestId("wizard-rationale-toggle")).toHaveTextContent("Why this workflow?");
  });

  test("no rationale → toggle absent", async () => {
    mockResponses.push({ events: [
      { type: "builder_started", step: "builder" },
      { type: "final", response: "Done" },
    ] });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByTestId("wizard-rationale-toggle")).toBeNull();
  });

  test("non-array rationale garbage → absent, no crash", async () => {
    mockResponses.push({ events: [
      { type: "builder_started", step: "builder" },
      { type: "final", response: "Done", rationale: "not-an-array" },
    ] });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByTestId("wizard-rationale-toggle")).toBeNull();
  });

  test("rationale array capped at 50 by panel, 30 by component", async () => {
    const many: any[] = [];
    for (let i = 0; i < 60; i++) {
      many.push({ step: `step-${i}`, rationale: `reason-${i}` });
    }
    mockResponses.push({ events: rationaleEvents(many) });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    const toggle = screen.getByTestId("wizard-rationale-toggle");
    fireEvent.click(toggle);
    const rows = screen.queryAllByTestId(/^wizard-rationale-entry-/);
    // Panel caps at 50, component (MAX_ENTRIES=30) renders at most 30.
    expect(rows).toHaveLength(30);
  });

  test("clicking an alternative (doc-shape) calls send with the prompt", async () => {
    // We track calls to the fetch request body to verify the send.
    // onTryDifferent → sendRef.current(prompt) which POSTs to the wizard stream.
    mockResponses.push({ events: rationaleEvents([DOC_ENTRY]) });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    const toggle = screen.getByTestId("wizard-rationale-toggle");
    fireEvent.click(toggle);

    // Find the "4x-UltraSharp" alternative button
    const altBtn = screen.getByTestId("wizard-rationale-alt-0-0");
    fireEvent.click(altBtn);

    // After clicking, the send triggers a new fetch call.
    // Wait for the request to be made.
    await new Promise((resolve) => setTimeout(resolve, 50));

    // The second request should be a POST with the follow-up prompt
    const secondReq = requests.find((r, idx) => {
      return idx > 0 && r.body?.message?.includes('For "Upscale model"') && r.body?.message?.includes('4x-UltraSharp');
    });
    expect(secondReq).toBeDefined();
  });

  test("backend-shaped entry renders without 'Chose:' line", async () => {
    mockResponses.push({ events: rationaleEvents([BACKEND_ENTRY]) });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    const toggle = screen.getByTestId("wizard-rationale-toggle");
    fireEvent.click(toggle);
    const row = screen.getByTestId("wizard-rationale-entry-0");
    expect(row).toHaveTextContent("critic-iter-1");
    expect(row).toHaveTextContent("Approved: licence-compatible for on-prem.");
    expect(row.textContent).not.toMatch(/Chose:/);
  });

  test("expanded list shows rationale content", async () => {
    mockResponses.push({ events: rationaleEvents([DOC_ENTRY]) });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await new Promise((resolve) => setTimeout(resolve, 50));

    const toggle = screen.getByTestId("wizard-rationale-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("wizard-rationale-list")).toBeInTheDocument();
  });
});
