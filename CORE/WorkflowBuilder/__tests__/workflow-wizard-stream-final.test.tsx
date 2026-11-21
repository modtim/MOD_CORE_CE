import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

// jsdom ships without TextEncoder/TextDecoder; the panel's stream path
// decodes with TextDecoder, so polyfill both from Node's util.
Object.assign(global, { TextEncoder, TextDecoder });

// Regression: e2e-full 2026-10-03 row 3 (workflow-wizard.spec.ts:36).
// The Workflow Wizard composer (#wizard-chat-input, disabled while `busy`)
// stayed disabled past the 90s budget. Two contributing defects:
//   1. Server: no bounded per-call LLM wait — a hung model held the SSE
//      stream (and `busy`) with no terminal event (fixed in the API).
//   2. Client: the panel waited for the stream to CLOSE before clearing
//      `busy`, instead of breaking on the terminal `final` event — so a
//      socket that lingers after `final` froze the composer even when a
//      final had already arrived.
//
// This test drives a stream that emits `started` + `final` and then
// NEVER closes (the next reader.read() hangs forever). With the fix, the
// panel breaks on `final`, cancels the reader, hydrates the reply, and
// re-enables the composer. Without the fix the read loop blocks on the
// hung read and the composer never re-enables.

let fetchImpl: any;

function installFetch() {
  fetchImpl = jest.fn();
  // @ts-ignore
  global.fetch = fetchImpl;
}

// Build a response whose .body is a fake ReadableStream: `getReader().read()`
// returns each SSE event in order, then the FINAL read never resolves —
// simulating a socket that stays open after the terminal event.
function hangAfterFinal(events: Array<Record<string, unknown>>) {
  const encoder = new TextEncoder();
  let i = 0;
  const reader = {
    read: async () => {
      if (i < events.length) {
        const s = `data: ${JSON.stringify(events[i])}\n\n`;
        i += 1;
        return { value: encoder.encode(s), done: false };
      }
      // Socket lingers: this read never settles.
      return new Promise(() => {});
    },
    cancel: async () => { /* no-op */ },
    releaseLock: () => { /* no-op */ },
  };
  return {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  };
}

beforeEach(() => {
  installFetch();
});

test("composer re-enables on the `final` event even if the stream never closes", async () => {
  fetchImpl.mockReturnValueOnce(
    hangAfterFinal([
      { type: "started", intent: "from_scratch", session_id: "s1" },
      {
        type: "final",
        session_id: "s1",
        response: "Done.",
        proposal: null,
        intent: null,
        sources: [],
        missing_nodes: [],
        node_build_handoffs: [],
        audit: null,
      },
    ]),
  );

  render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
  const input = screen.getByPlaceholderText(/Describe the workflow/i);

  fireEvent.change(input, { target: { value: "resize an image" } });
  fireEvent.submit(input.closest("form")!);

  // The reply hydrated from the `final` event lands…
  await screen.findByText("Done.", {}, { timeout: 20000 });
  // …AND the composer re-enables (busy cleared) even though the fake
  // stream never closed. This is the assertion that fails without the
  // client-side break-on-final fix: without it the read loop blocks on the
  // hung `reader.read()` and `busy` never clears, so this times out.
  await new Promise<void>((resolve, reject) => {
    const deadline = Date.now() + 4000;
    const check = () => {
      if ((input as HTMLInputElement).disabled === false) return resolve();
      if (Date.now() > deadline) return reject(new Error("composer never re-enabled"));
      setTimeout(check, 10);
    };
    check();
  });
  expect((input as HTMLInputElement).disabled).toBe(false);
}, 30000);
