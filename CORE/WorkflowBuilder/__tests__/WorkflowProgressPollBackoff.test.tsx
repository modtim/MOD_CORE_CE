/**
 * Task 4.11 — representative component integration test (WS1.17).
 *
 * the internal design notes. WorkflowProgress polls
 * `/road/workflow-runs/{id}/progress` (then best-effort `/node-status`)
 * every 3s through the shared `usePollingBackoff` hook (which wraps `Poller`).
 * This renders the REAL component with only global `fetch` mocked (the network
 * boundary) and fake timers, and proves the progress poll inherits: 3s base
 * cadence when healthy, enable gating on runId + accessToken (and stop on a
 * terminal status), exponential backoff on repeated 5xx, the self-heal contract
 * (banner once, keep probing at maxMs — no dead-stop), and reset to the 3s base
 * on the first success.
 */

import React from "react";
import { act, render, screen } from "@testing-library/react";
import { WorkflowProgress } from "@/components/workflow-progress";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const RUN_ID = 42;
const PROGRESS_PATH = `/road/workflow-runs/${RUN_ID}/progress`;
const NODE_STATUS_PATH = `/road/workflow-runs/${RUN_ID}/node-status`;

function progressBody(pct: number, status = "running") {
  return {
    workflow_run_id: RUN_ID,
    status,
    progress_percentage: pct,
    completed_nodes: 1,
    total_nodes: 4,
    current_node: null,
    total_cost: 0,
    started_at: "2026-09-24T00:00:00Z",
    estimated_seconds_remaining: null,
  };
}

describe("WorkflowProgress poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;
  let progressStatus: number;
  let progressPct: number;
  let runStatus: string;
  let now: number;

  const progressCalls = () =>
    fetchMock.mock.calls.filter((c) => String(c[0]).includes(PROGRESS_PATH)).length;
  const nodeStatusCalls = () =>
    fetchMock.mock.calls.filter((c) => String(c[0]).includes(NODE_STATUS_PATH)).length;

  async function advanceTo(t: number): Promise<void> {
    const delta = t - now;
    now = t;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(delta);
    });
  }

  beforeEach(() => {
    jest.useFakeTimers();
    now = 0;
    progressStatus = 200;
    progressPct = 25;
    runStatus = "running";
    fetchMock = jest.fn(async (url: string) => {
      if (String(url).includes(PROGRESS_PATH)) {
        return progressStatus === 200
          ? makeResponse(200, progressBody(progressPct, runStatus))
          : makeResponse(progressStatus);
      }
      if (String(url).includes(NODE_STATUS_PATH)) {
        return makeResponse(200, { nodes: [] });
      }
      return makeResponse(404);
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("polls the run's progress at the 3s base interval while the run is active", async () => {
    const { unmount } = render(<WorkflowProgress workflowRunId={RUN_ID} accessToken="tok" />);
    await advanceTo(1);
    // Immediate tick on mount (t=0): progress, then best-effort node-status.
    expect(progressCalls()).toBe(1);
    expect(nodeStatusCalls()).toBe(1);
    expect(await screen.findByText("25%")).toBeInTheDocument();

    await advanceTo(2999); // just before the 3s edge (first tick fired at t=0)
    expect(progressCalls()).toBe(1);
    await advanceTo(3000); // cross the edge
    expect(progressCalls()).toBe(2);
    await advanceTo(5999);
    expect(progressCalls()).toBe(2);
    await advanceTo(6000);
    expect(progressCalls()).toBe(3);
    unmount();
  });

  it("does not poll until both runId and accessToken are present (enable gating)", async () => {
    const { unmount, rerender } = render(<WorkflowProgress workflowRunId={RUN_ID} accessToken={null} />);
    await advanceTo(30000);
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(<WorkflowProgress workflowRunId={0} accessToken="tok" />);
    await advanceTo(60000);
    expect(fetchMock).not.toHaveBeenCalled();

    // Both present flips `enabled` true → the poller starts with an immediate tick.
    rerender(<WorkflowProgress workflowRunId={RUN_ID} accessToken="tok" />);
    await advanceTo(60001);
    expect(progressCalls()).toBe(1);
    await advanceTo(63001);
    expect(progressCalls()).toBe(2);
    unmount();
  });

  it("stops polling once the run reaches a terminal status", async () => {
    const onComplete = jest.fn();
    const { unmount } = render(
      <WorkflowProgress workflowRunId={RUN_ID} accessToken="tok" onComplete={onComplete} />,
    );
    await advanceTo(1);
    expect(progressCalls()).toBe(1);

    runStatus = "success";
    progressPct = 100;
    await advanceTo(3001); // terminal response -> isPolling false -> poller torn down
    expect(progressCalls()).toBe(2);
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("100%")).toBeInTheDocument();

    await advanceTo(60000);
    expect(progressCalls()).toBe(2);
    unmount();
  });

  it("backs off on repeated 5xx, keeps probing after the banner (no dead-stop) and resets to the 3s base on success", async () => {
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (6s, 12s, 24s, 48s, then capped at the 60s default maxMs).
    jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const { unmount } = render(<WorkflowProgress workflowRunId={RUN_ID} accessToken="tok" />);

    await advanceTo(1); // t=0 success -> progress renders
    expect(progressCalls()).toBe(1);
    expect(await screen.findByText("25%")).toBeInTheDocument();

    progressStatus = 502;
    await advanceTo(3001); // t=3s: failure 1
    expect(progressCalls()).toBe(2);
    // A failed progress tick throws before the secondary node-status fetch.
    expect(nodeStatusCalls()).toBe(1);

    // Backoff: the next probe is NOT at the 3s base cadence...
    await advanceTo(6001);
    expect(progressCalls()).toBe(2);
    // ...but ~6s after the failure (doubled).
    await advanceTo(9001); // failure 2 (next ~12s later)
    expect(progressCalls()).toBe(3);
    await advanceTo(20000);
    expect(progressCalls()).toBe(3);
    await advanceTo(21001); // failure 3 (next ~24s later)
    expect(progressCalls()).toBe(4);
    await advanceTo(44000);
    expect(progressCalls()).toBe(4);
    await advanceTo(45001); // failure 4 (next ~48s later)
    expect(progressCalls()).toBe(5);
    expect(lost).not.toHaveBeenCalled();
    await advanceTo(92000);
    expect(progressCalls()).toBe(5);
    await advanceTo(93001); // failure 5 -> connection-lost banner, probe at maxMs
    expect(progressCalls()).toBe(6);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at ~maxMs, banner fires once.
    await advanceTo(150000);
    expect(progressCalls()).toBe(6);
    await advanceTo(153001); // failure 6
    expect(progressCalls()).toBe(7);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good progress is retained on transient errors.
    expect(screen.getByText("25%")).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 3s base.
    progressStatus = 200;
    progressPct = 75;
    await advanceTo(210000);
    expect(progressCalls()).toBe(7);
    await advanceTo(213001); // success
    expect(progressCalls()).toBe(8);
    expect(await screen.findByText("75%")).toBeInTheDocument();
    await advanceTo(216001); // 3s base again
    expect(progressCalls()).toBe(9);
    await advanceTo(219001);
    expect(progressCalls()).toBe(10);
    expect(lost).toHaveBeenCalledTimes(1);

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  it("stops polling after unmount (no further fetches)", async () => {
    const { unmount } = render(<WorkflowProgress workflowRunId={RUN_ID} accessToken="tok" />);
    await advanceTo(1);
    expect(progressCalls()).toBe(1);
    unmount();
    await advanceTo(30001); // well past several 3s intervals: nothing new
    expect(progressCalls()).toBe(1);
  });
});
