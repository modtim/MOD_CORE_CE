import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

// WSIP 4.5/4.6 — conversational debugging in the Workflow Wizard panel.
// Setup copied from wizardTestRun.test.tsx (mock fetch recorder + response
// queue); the SSE-driven case copies the fake-reader helper from
// workflow-wizard-stream-final.test.tsx because jsdom ships no
// TextEncoder/TextDecoder and no ReadableStream on mocked Responses.
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

beforeEach(() => {
  requests = [];
  mockResponses = [];
  // @ts-ignore
  global.fetch = mockFetch();
});

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

const DEBUG_EVENTS = [
  { type: "debug_started", bound_run_id: 7 },
  { type: "context_bundle_assembled", run_id: 7, node_count: 3, artifact_count: 2, lineage_truncated: false },
  { type: "root_cause_identified" },
  { type: "fix_proposed" },
  { type: "fix_verification_started" },
  { type: "fix_verification_succeeded" },
  { type: "apply_card_ready" },
  FINAL,
];

describe("§4.5 — debug mode is bound to the run", () => {
  test("debugRunId set → stream body carries mode=debug + bound_run_id", async () => {
    mockResponses.push({ events: DEBUG_EVENTS });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    // Header names the bound run so the user knows which failure they're on.
    await screen.findByText(/Debugging run #7/);

    await new Promise((resolve) => setTimeout(resolve, 50));
    const req = requests.find((r) => r.url.includes("/workflow-wizard/stream"));
    expect(req).toBeDefined();
    expect(req!.body.mode).toBe("debug");
    expect(req!.body.bound_run_id).toBe(7);
    // The default opening question was auto-sent once.
    expect(req!.body.message).toBe("Why did this run fail?");
  });

  test("debugRunId absent → mode is never debug", async () => {
    mockResponses.push({ events: [{ type: "started" }, FINAL] });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await screen.findByText(/upscale node|Done\./);

    const req = requests.find((r) => r.url.includes("/workflow-wizard/stream"));
    expect(req).toBeDefined();
    expect(req!.body.mode).toBeUndefined();
    expect(req!.body.bound_run_id).toBeUndefined();
  });

  test("404 feature_disabled → friendly message, no crash", async () => {
    mockResponses.push({ status: 404, body: { detail: { error: "feature_disabled" } } });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    // The friendly line is the assistant turn that replaced the streaming
    // placeholder — it must render exactly once, not also duplicated into
    // the panel's generic red error banner.
    const nodes = await screen.findAllByText(/Conversational debugging isn't enabled/i);
    expect(nodes.length).toBe(1);
    const req = requests.find((r) => r.url.includes("/workflow-wizard/stream"));
    expect(req).toBeDefined();
    expect(req!.body.mode).toBe("debug");
  });

  test("debug SSE events render readable progress lines", async () => {
    mockResponses.push({ events: DEBUG_EVENTS });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    await screen.findByText(/Debugging run #7…/);
    await screen.findByText(/Collected the run's story/i);
    await screen.findByText(/Found the node that went wrong first/i);
    await screen.findByText(/Proposing a fix/i);
    await screen.findByText(/Re-running the fixed workflow/i);
    await screen.findByText(/Fix verified/i);
    await screen.findByText(/Fix is ready to apply/i);
  });
});

// WSIP 4.4 — quality-signal: debug_fix_applied on Apply in debug mode.

const APPLY_PROPOSAL = {
  name: "fix-oversample",
  nodes: [{ id: "n1", type: "core", data: { label: "Oversample" } }],
  edges: [],
};

const DEBUG_WITH_PROPOSAL = [
  { type: "debug_started", bound_run_id: 7 },
  { type: "context_bundle_assembled", run_id: 7, node_count: 3, artifact_count: 2, lineage_truncated: false },
  { type: "root_cause_identified" },
  { type: "fix_proposed" },
  { type: "fix_verification_started" },
  { type: "fix_verification_succeeded" },
  { type: "apply_card_ready" },
  {
    type: "final",
    session_id: "s1",
    response: "The fix is applied.",
    proposal: APPLY_PROPOSAL,
    intent: null,
    sources: [],
    missing_nodes: [],
    node_build_handoffs: [],
    audit: null,
    message_id: 42,
  },
];

describe("§4.4 — debug_fix_applied quality-signal", () => {
  beforeEach(() => {
    requests = [];
    mockResponses = [];
    // @ts-ignore
    global.fetch = mockFetch();
  });

  test("debug thread + successful Apply → one quality-signal POST", async () => {
    mockResponses.push({ events: DEBUG_WITH_PROPOSAL });
    mockResponses.push({
      status: 200,
      body: { workflow_id: 1, version_id: 5, name: "fix-oversample", apply_mode: "replace" },
    });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Fix is ready to apply/);
    await screen.findByText(/Proposed: fix-oversample/);
    fireEvent.click(screen.getByText("Apply to canvas"));
    await new Promise((resolve) => setTimeout(resolve, 50));

    const signalReq = requests.find((r) => r.url.includes("/workflow-wizard/quality-signal"));
    expect(signalReq).toBeDefined();
    expect(signalReq!.body.session_id).toBe("s1");
    expect(signalReq!.body.event_type).toBe("debug_fix_applied");
    expect(signalReq!.body.proposal_id).toBe(42);
    expect(signalReq!.body.applied_workflow_version_id).toBe(5);
    expect(signalReq!.body.feedback_event_metadata).toEqual({ bound_run_id: 7 });
  });

  test("non-debug thread Apply → no quality-signal call", async () => {
    mockResponses.push({ events: [{ type: "started" }, { type: "final", session_id: "s1", response: "done", proposal: { ...APPLY_PROPOSAL, name: "test" }, intent: null, sources: [], missing_nodes: [], node_build_handoffs: [], audit: null, message_id: 99 }] });
    mockResponses.push({
      status: 200,
      body: { workflow_id: 1, version_id: 10, name: "test", apply_mode: "replace" },
    });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await screen.findByText(/Proposed: test/);
    fireEvent.click(screen.getByText("Apply to canvas"));
    await new Promise((resolve) => setTimeout(resolve, 50));

    const signalReq = requests.find((r) => r.url.includes("/workflow-wizard/quality-signal"));
    expect(signalReq).toBeUndefined();
  });

  test("quality-signal POST rejecting → Apply UI still shows success", async () => {
    mockResponses.push({ events: DEBUG_WITH_PROPOSAL });
    mockResponses.push({
      status: 200,
      body: { workflow_id: 1, version_id: 5, name: "fix-oversample", apply_mode: "replace" },
    });
    mockResponses.push({ status: 500, body: { detail: "server error" } });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Fix is ready to apply/);
    await screen.findByText(/Proposed: fix-oversample/);
    fireEvent.click(screen.getByText("Apply to canvas"));
    await new Promise((resolve) => setTimeout(resolve, 50));

    const applyReq = requests.find((r) => r.url.includes("/workflow-wizard/apply"));
    expect(applyReq).toBeDefined();
    const signalReq = requests.find((r) => r.url.includes("/workflow-wizard/quality-signal"));
    expect(signalReq).toBeDefined();
  });

  test("second Apply of same proposal → no second signal", async () => {
    mockResponses.push({ events: DEBUG_WITH_PROPOSAL });
    mockResponses.push({
      status: 200,
      body: { workflow_id: 1, version_id: 5, name: "fix-oversample", apply_mode: "replace" },
    });
    mockResponses.push({
      status: 200,
      body: { workflow_id: 1, version_id: 6, name: "fix-oversample", apply_mode: "replace" },
    });
    mockResponses.push({ status: 200, body: { action: "debug_fix_applied" } });

    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Fix is ready to apply/);
    await screen.findByText(/Proposed: fix-oversample/);

    fireEvent.click(screen.getByText("Apply to canvas"));
    await new Promise((resolve) => setTimeout(resolve, 50));
    fireEvent.click(screen.getByText("Apply to canvas"));
    await new Promise((resolve) => setTimeout(resolve, 50));

    const signalReqs = requests.filter((r) => r.url.includes("/workflow-wizard/quality-signal"));
    expect(signalReqs.length).toBe(1);
  });
});
