import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

Object.assign(global, { TextEncoder, TextDecoder });

// 2400LR-wb12f — when the wizard stream carries an `error` event with a
// message (model + host:port), the panel must show that message instead of
// the generic "wizard is unreachable" fallback.

function streamOf(events: Array<Record<string, unknown>>) {
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
    cancel: async () => {},
    releaseLock: () => {},
  };
  return { ok: true, status: 200, body: { getReader: () => reader } };
}

async function submit(events: Array<Record<string, unknown>>) {
  // @ts-ignore
  global.fetch = jest.fn().mockReturnValueOnce(streamOf(events));
  render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "resize an image" } });
  fireEvent.submit(input.closest("form")!);
}

test("stream error event with a message renders that message, not the generic fallback", async () => {
  const msg = "Wizard model qwen-coder at llm-host:8000 is unreachable.";
  await submit([
    { type: "started", intent: "from_scratch", session_id: "s1" },
    { type: "error", message: msg },
  ]);
  const hits = await screen.findAllByText(msg, {}, { timeout: 5000 });
  expect(hits.length).toBeGreaterThan(0);
  expect(screen.queryByText(/the wizard is unreachable/i)).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain(msg);
}, 15000);

test("no server message keeps the generic fallback", async () => {
  await submit([{ type: "started", intent: "from_scratch", session_id: "s1" }]);
  await screen.findByText(/the wizard is unreachable/i, {}, { timeout: 5000 });
}, 15000);
