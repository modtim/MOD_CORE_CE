import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

// WSIP 4.6a — DebugOutputCompare rendering inside the wizard panel.
// SSE/stream mocking harness copied from wizardDebugMode.test.tsx (that
// file is NOT edited): jsdom ships no TextEncoder/TextDecoder and no
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

const VALID_ORIG = [
  { label: "stdout", kind: "text", value: "bad frame" },
  { label: "report", kind: "json", value: '{"k":"v"}' },
];
const VALID_CAND = [
  { label: "stdout", kind: "text", value: "good frame" },
  { label: "preview", kind: "image", value: "https://example.com/a.png" },
];

function debugEventsWith(outputs: any): any[] {
  return [
    { type: "debug_started", bound_run_id: 7 },
    {
      type: "fix_verification_succeeded",
      ...(outputs as any),
    },
    FINAL,
  ];
}

describe("WSIP 4.6a — fix_verification_succeeded outputs render a compare card", () => {
  test("debug + both valid arrays → compare renders with both sides' labels", async () => {
    mockResponses.push({
      events: debugEventsWith({
        original_outputs: VALID_ORIG,
        candidate_outputs: VALID_CAND,
      }),
    });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Debugging run #7/);
    await new Promise((resolve) => setTimeout(resolve, 50));

    await screen.findByText("Original vs fixed output");
    // Labels from BOTH sides appear (report only on original, preview only
    // on candidate, stdout on both).
    // "stdout" appears on both sides (label spans), so count, don't find.
    expect(screen.getAllByText("stdout")).toHaveLength(2);
    expect(screen.getAllByText("report")).toHaveLength(2);
    expect(screen.getAllByText("preview")).toHaveLength(2);
  });

  test("debug + missing arrays → no compare card", async () => {
    mockResponses.push({ events: debugEventsWith({}) });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Debugging run #7/);
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByText("Original vs fixed output")).toBeNull();
  });

  test("debug + only one side present → no compare card", async () => {
    mockResponses.push({
      events: debugEventsWith({ original_outputs: VALID_ORIG }),
    });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Debugging run #7/);
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByText("Original vs fixed output")).toBeNull();
  });

  test("non-debug + arrays → no compare card", async () => {
    mockResponses.push({
      events: [
        {
          type: "fix_verification_succeeded",
          original_outputs: VALID_ORIG,
          candidate_outputs: VALID_CAND,
        },
        FINAL,
      ],
    });
    render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
    const input = screen.getByPlaceholderText(/Describe the workflow/i);
    fireEvent.change(input, { target: { value: "build a pipeline" } });
    fireEvent.submit(input.closest("form")!);
    await screen.findByText(/upscale node/);

    const req = requests.find((r) => r.url.includes("/workflow-wizard/stream"));
    expect(req).toBeDefined();
    expect(req!.body.mode).toBeUndefined();
    expect(screen.queryByText("Original vs fixed output")).toBeNull();
  });

  test("invalid items filtered: bad kind + non-string value dropped", async () => {
    mockResponses.push({
      events: debugEventsWith({
        original_outputs: [
          { label: "stdout", kind: "text", value: "ok" },
          { label: "weird", kind: "video", value: "x" },
          { label: "novalue", kind: "text" },
          { label: "numval", kind: "text", value: 3 },
        ],
        candidate_outputs: [
          { label: "stdout", kind: "text", value: "fixed" },
          { label: "url-ok", kind: "url", value: "https://example.com/r" },
        ],
      }),
    });
    render(
      <WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" debugRunId={7} />,
    );
    await screen.findByText(/Debugging run #7/);
    await new Promise((resolve) => setTimeout(resolve, 50));

    await screen.findByText("Original vs fixed output");
    expect(screen.getAllByText("stdout")).toHaveLength(2);
    expect(screen.getAllByText("url-ok")).toHaveLength(2);
    // Invalid items never reach the card.
    expect(screen.queryByText("weird")).toBeNull();
    expect(screen.queryByText("novalue")).toBeNull();
    expect(screen.queryByText("numval")).toBeNull();
  });
});
