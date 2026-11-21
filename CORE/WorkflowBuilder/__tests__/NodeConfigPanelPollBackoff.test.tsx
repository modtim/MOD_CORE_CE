/**
 * Task 4.11 — representative component integration test (WS1.21).
 *
 * the internal design notes. NodeConfigPanel polls the
 * selected run's `/road/workflow-runs/{runId}/detailed-log` every 3s while
 * the node is running, through the shared `usePollingBackoff` hook (which wraps
 * `Poller`). This renders the REAL panel with only global `fetch` mocked (the
 * network boundary; `authenticatedFetch` also lands there) and fake timers, and
 * proves the execution-info poll inherits: 3s base cadence when healthy, gating
 * on a running node, exponential backoff on repeated 5xx, the self-heal contract
 * (banner once, keep probing at maxMs — no dead-stop), and reset to the 3s base
 * on the first success.
 */

import React from "react";
import { act, render, screen } from "@testing-library/react";
import NodeConfigPanel from "@/components/node-config-panel";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const RUN_ID = 42;
const DETAIL_PATH = `/road/workflow-runs/${RUN_ID}/detailed-log`;

const makeNode = (data: any = {}) => ({
  id: "n1",
  type: "process",
  position: { x: 0, y: 0 },
  data: { label: "Resize", node_kind: "resize", status: "running", ...data },
});

function detailBody(durationS: number) {
  return { nodes: [{ node_id: "n1", duration_s: durationS }] };
}

describe("NodeConfigPanel execution-info poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;
  let detailStatus: number;
  let detailDuration: number;
  let now: number;

  const detailCalls = () =>
    fetchMock.mock.calls.filter((c) => String(c[0]).includes(DETAIL_PATH)).length;

  async function advanceTo(t: number): Promise<void> {
    const delta = t - now;
    now = t;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(delta);
    });
  }

  function renderPanel(data: any = {}) {
    return render(
      <NodeConfigPanel
        node={makeNode(data) as any}
        updateNodeData={() => {}}
        onClose={() => {}}
        accessToken="tok"
        pollingRunId={RUN_ID}
      />,
    );
  }

  beforeEach(() => {
    jest.useFakeTimers();
    now = 0;
    detailStatus = 200;
    detailDuration = 12;
    fetchMock = jest.fn(async (url: string) => {
      if (String(url).includes(DETAIL_PATH)) {
        return detailStatus === 200
          ? makeResponse(200, detailBody(detailDuration))
          : makeResponse(detailStatus);
      }
      // Every other panel fetch (managed stacks list, etc.) is healthy + empty.
      return makeResponse(200, []);
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

  it("polls the run's detailed-log at the 3s base interval while the node is running", async () => {
    const { unmount } = renderPanel();
    await advanceTo(1);
    // Mount: the one-shot fetch + the poller's immediate tick.
    expect(detailCalls()).toBe(2);
    await advanceTo(2999); // just before the 3s edge: not faster than base
    expect(detailCalls()).toBe(2);
    await advanceTo(3001);
    expect(detailCalls()).toBe(3);
    await advanceTo(6001);
    expect(detailCalls()).toBe(4);
    unmount();
  });

  it("does not poll when the node is not running (enable gating)", async () => {
    const { unmount } = renderPanel({ status: "success" });
    await advanceTo(30000);
    // Only the one-shot fetch; no 3s poll.
    expect(detailCalls()).toBe(1);
    unmount();
  });

  it("backs off on repeated 5xx, keeps probing after the banner (no dead-stop) and resets to the 3s base on success", async () => {
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (6s, 12s, 24s, 48s, then capped at the 60s default maxMs).
    jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const { unmount } = renderPanel();

    await advanceTo(1); // t=0 success -> execution info renders
    expect(detailCalls()).toBe(2);
    expect(await screen.findByText("12s")).toBeInTheDocument();

    detailStatus = 502;
    await advanceTo(3001); // t=3s: failure 1
    expect(detailCalls()).toBe(3);

    // Backoff: the next probe is NOT at the 3s base cadence...
    await advanceTo(6001);
    expect(detailCalls()).toBe(3);
    // ...but ~6s after the failure (doubled).
    await advanceTo(9001); // failure 2 (next ~12s later)
    expect(detailCalls()).toBe(4);
    await advanceTo(20000);
    expect(detailCalls()).toBe(4);
    await advanceTo(21001); // failure 3 (next ~24s later)
    expect(detailCalls()).toBe(5);
    await advanceTo(45001); // failure 4 (next ~48s later)
    expect(detailCalls()).toBe(6);
    expect(lost).not.toHaveBeenCalled();
    await advanceTo(93001); // failure 5 -> connection-lost banner, probe at maxMs
    expect(detailCalls()).toBe(7);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at ~maxMs, banner fires once.
    await advanceTo(150000);
    expect(detailCalls()).toBe(7);
    await advanceTo(153001);
    expect(detailCalls()).toBe(8);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good execution info is retained on transient errors.
    expect(screen.getByText("12s")).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 3s base.
    detailStatus = 200;
    detailDuration = 34;
    await advanceTo(213001); // success
    expect(detailCalls()).toBe(9);
    expect(await screen.findByText("34s")).toBeInTheDocument();
    await advanceTo(216001); // 3s base again
    expect(detailCalls()).toBe(10);
    await advanceTo(219001);
    expect(detailCalls()).toBe(11);
    expect(lost).toHaveBeenCalledTimes(1);

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  it("stops polling after unmount (no further fetches)", async () => {
    const { unmount } = renderPanel();
    await advanceTo(1); // initial effect + the poller's immediate tick
    const c0 = detailCalls();
    expect(c0).toBeGreaterThanOrEqual(1);
    unmount();
    await advanceTo(30001); // well past several 3s intervals: nothing new
    expect(detailCalls()).toBe(c0);
  });
});
