import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

let requests: Array<{ url: string; body: any; method?: string }> = [];
let mockResponses: Array<{ status?: number; body: any }> = [];

function mockFetch() {
  return jest.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(init.body as string) : null;
    requests.push({ url, body, method: init?.method });
    const next = mockResponses.shift();
    if (!next) {
      return { ok: true, status: 200, json: async () => ({}), text: async () => "" } as Response;
    }
    const st = next.status ?? 200;
    return {
      ok: st >= 200 && st < 300,
      status: st,
      json: async () => next.body,
      text: async () => JSON.stringify(next.body),
    } as Response;
  });
}

beforeEach(() => {
  requests = [];
  mockResponses = [];
  // @ts-ignore
  global.fetch = mockFetch();
});

function setupWithProposal() {
  mockResponses.push({
    body: {
      session_id: "s1",
      response: "Here is a workflow.",
      intent: "from_scratch",
      proposal: {
        name: "Test Pipeline",
        nodes: [
          { id: "n1", data: { nodeDefinitionName: "file_input" } },
          { id: "n2", data: { nodeDefinitionName: "image_converter" } },
        ],
        edges: [{ id: "e1", source: "n1", target: "n2" }],
      },
    },
  });
}

async function renderAndGenerate() {
  render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "build a pipeline" } });
  fireEvent.submit(input.closest("form")!);
  await screen.findByText(/Test Pipeline/);
}

describe("§4.1 — Test this workflow button", () => {
  test("button renders on proposals", async () => {
    setupWithProposal();
    await renderAndGenerate();
    const btn = screen.getByTitle(/Run this workflow against a sample input/i);
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
    expect(btn.textContent).toMatch(/Test this workflow/i);
  });

  test("clicking dispatches POST to /test-run", async () => {
    setupWithProposal();
    mockResponses.push({
      status: 202,
      body: { workflow_run_id: 42, test_input_tier: "tenant_recent", wizard_test_run_id: 7 },
    });
    await renderAndGenerate();

    const btn = screen.getByTitle(/Run this workflow against a sample input/i);
    fireEvent.click(btn);

    await waitFor(() => {
      const req = requests.find(r => r.url.includes("/test-run") && r.method === "POST");
      expect(req).toBeDefined();
      expect(req!.body.session_id).toBe("s1");
    });
  });

  test("button disables after dispatch", async () => {
    setupWithProposal();
    mockResponses.push({
      status: 202,
      body: { workflow_run_id: 42, test_input_tier: "tenant_recent", wizard_test_run_id: 7 },
    });
    await renderAndGenerate();

    const btn = screen.getByTitle(/Run this workflow against a sample input/i);
    fireEvent.click(btn);

    await waitFor(() => {
      expect(btn).toBeDisabled();
    });
  });
});

describe("§4.2 — Test-run status rendering", () => {
  test("shows queued status after dispatch", async () => {
    setupWithProposal();
    mockResponses.push({
      status: 202,
      body: { workflow_run_id: 42, test_input_tier: "tenant_recent" },
    });
    await renderAndGenerate();

    fireEvent.click(screen.getByTitle(/Run this workflow against a sample input/i));

    await screen.findByText(/queued|waiting for a worker/i);
  });

  test("shows error on dispatch failure", async () => {
    setupWithProposal();
    mockResponses.push({
      status: 422,
      body: { detail: "no proposal found in this session" },
    });
    await renderAndGenerate();

    fireEvent.click(screen.getByTitle(/Run this workflow against a sample input/i));

    await screen.findByText(/no proposal found/i);
  });
});
