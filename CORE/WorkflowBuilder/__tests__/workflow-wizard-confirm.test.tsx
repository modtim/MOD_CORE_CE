import React from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { TextEncoder, TextDecoder } from "util";
import { WorkflowWizardPanel } from "@/components/workflow-wizard-panel";

// jsdom ships without TextEncoder/TextDecoder; the panel's stream path
// decodes with TextDecoder, so polyfill both from Node's util.
Object.assign(global, { TextEncoder, TextDecoder });

// GSEC R-022 — wizard human confirm before exec. When the platform
// setting wizard.require_human_confirm_before_exec is on, the server
// stops the wizard BEFORE dispatching its test run and the terminal
// `final` event carries `needs_confirmation: true` with the full graph
// as `proposal`. The panel must surface an inline confirmation card
// ("Confirm & run" / "Not now") after the assistant reply:
//   1. a `final` with needs_confirmation renders the card
//   2. "Confirm & run" re-sends the same turn with confirmed: true
//   3. "Not now" dismisses the card.

let fetchImpl: any;

function installFetch() {
  fetchImpl = jest.fn();
  // @ts-ignore
  global.fetch = fetchImpl;
}

// Build a response whose .body is a fake ReadableStream that yields the
// given SSE events and then closes normally (done: true).
function streamEvents(events: Array<Record<string, unknown>>) {
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
  };
}

// The paused final: needs_confirmation true + the full graph as
// proposal (same shape the server ships on WizardResponse).
function pausedFinal() {
  return {
    type: "final",
    session_id: "s1",
    response: "Here's the workflow. I stopped before running it — confirm to continue.",
    proposal: {
      name: "Resize + Upscale",
      nodes: [
        {
          id: "n1",
          type: "image-resize",
          data: { label: "Resize" },
          position: { x: 0, y: 0 },
        },
        {
          id: "n2",
          type: "esrgan-upscale",
          data: { label: "Upscale" },
          position: { x: 220, y: 0 },
        },
      ],
      edges: [{ id: "e1", source: "n1", target: "n2" }],
    },
    intent: "from_scratch",
    sources: [],
    missing_nodes: [],
    node_build_handoffs: [],
    audit: null,
    needs_confirmation: true,
    graph_summary: {
      node_count: 2,
      edge_count: 1,
      node_names: ["Resize", "Upscale"],
      workflow_name: "Resize + Upscale",
    },
  };
}

beforeEach(() => {
  installFetch();
});

async function sendTurn() {
  render(<WorkflowWizardPanel open={true} onClose={() => {}} accessToken="t" />);
  const input = screen.getByPlaceholderText(/Describe the workflow/i);
  fireEvent.change(input, { target: { value: "resize then upscale an image" } });
  fireEvent.submit(input.closest("form")!);
  return input;
}

test("final with needs_confirmation renders the confirm card", async () => {
  fetchImpl.mockReturnValueOnce(
    streamEvents([
      { type: "started", intent: "from_scratch", session_id: "s1" },
      {
        type: "needs_confirmation",
        iteration: 1,
        reason: "wizard.require_human_confirm_before_exec",
        graph_summary: {
          node_count: 2,
          edge_count: 1,
          node_names: ["Resize", "Upscale"],
          workflow_name: "Resize + Upscale",
        },
      },
      pausedFinal(),
    ]),
  );

  await sendTurn();

  const card = await screen.findByRole("group", {
    name: /confirm workflow "resize \+ upscale" before running/i,
  });
  expect(card).toBeInTheDocument();
  // Summary: workflow name, node count, edge count, node names —
  // scoped to the card since the proposal diff below the reply
  // repeats the workflow name and a node list of its own.
  const inCard = within(card);
  expect(inCard.getByText(/Resize \+ Upscale/)).toBeInTheDocument();
  expect(inCard.getByText(/2 nodes/)).toBeInTheDocument();
  expect(inCard.getByText(/1 edge/)).toBeInTheDocument();
  expect(inCard.getByText("· Resize")).toBeInTheDocument();
  expect(inCard.getByText("· Upscale")).toBeInTheDocument();
  // Both action buttons are present and keyboard-reachable (real
  // <button> elements inside the card).
  expect(inCard.getByRole("button", { name: /confirm & run/i })).toBeEnabled();
  expect(inCard.getByRole("button", { name: /not now/i })).toBeEnabled();
}, 30000);

test("Confirm & run re-sends the turn with confirmed: true", async () => {
  fetchImpl
    .mockReturnValueOnce(streamEvents([pausedFinal()]))
    .mockReturnValueOnce(
      streamEvents([
        { type: "started", intent: "from_scratch", session_id: "s1" },
        {
          type: "final",
          session_id: "s1",
          response: "Running it now.",
          proposal: null,
          intent: null,
          sources: [],
          missing_nodes: [],
          node_build_handoffs: [],
          audit: null,
          needs_confirmation: false,
        },
      ]),
    );

  await sendTurn();
  await screen.findByText(/stopped before running it/i, {}, { timeout: 20000 });

  const confirmBtn = screen.getByRole("button", { name: /confirm & run/i });
  fireEvent.click(confirmBtn);

  // Wait for the second stream's reply to land, proving the re-send
  // went out and came back.
  await screen.findByText("Running it now.", {}, { timeout: 20000 });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
  const [, init] = fetchImpl.mock.calls[1];
  const body = JSON.parse(init.body as string);
  expect(body.confirmed).toBe(true);
  // Re-send of the SAME turn: same message, same session.
  expect(body.message).toBe("resize then upscale an image");
  expect(body.session_id).toBe("s1");
}, 30000);

test("Not now dismisses the card", async () => {
  fetchImpl.mockReturnValueOnce(streamEvents([pausedFinal()]));

  await sendTurn();
  await screen.findByRole("group", {
    name: /confirm workflow "resize \+ upscale" before running/i,
  });

  fireEvent.click(screen.getByRole("button", { name: /not now/i }));

  await new Promise<void>(resolve => setTimeout(resolve, 50));
  expect(screen.queryByTestId("wizard-confirm-card")).not.toBeInTheDocument();
  // No extra request was fired — dismissal is purely local.
  expect(fetchImpl).toHaveBeenCalledTimes(1);
}, 30000);
