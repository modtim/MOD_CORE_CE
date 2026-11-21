/**
 * Task 4.11 — representative component integration test (WS1.5).
 *
 * the internal design notes. FanoutModal polls
 * `/runs/{runId}/node-fanout/{nodeId}` every 3s through the shared
 * `usePollingBackoff` hook (which wraps `Poller`) while any fanned item is
 * non-terminal. This renders the REAL component with only global `fetch`
 * mocked (the network boundary) and fake timers, and proves the fan-out poll
 * inherits: 3s base cadence when healthy, stop once every item is terminal,
 * exponential backoff on repeated 5xx, the self-heal contract (banner once,
 * keep probing at maxMs — no dead-stop), reset to the 3s base on the first
 * success, and pause-when-hidden.
 *
 * NB: on open the modal fires TWO fetches at t=0 — its own initial-load effect
 * plus the poller's immediate first tick — so the poll count starts at 2.
 */

import React from "react";
import { act, render, screen } from "@testing-library/react";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

// Render-only leaf, NOT the network boundary: AssetPreview statically imports
// three.js ESM loaders that Jest cannot transform. Every item in these fixtures
// has null previews, so it never renders anyway. Same stub as the other
// component tests that transitively import it.
jest.mock("@/components/nodes/asset-preview", () => ({ AssetPreview: () => null }));

import FanoutModal from "@/components/FanoutModal";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const RUN_ID = 55;
const NODE_ID = "node-7";
const FANOUT_PATH = `/runs/${RUN_ID}/node-fanout/${NODE_ID}`;

function fanoutBody(itemStatus: string) {
  return {
    run_id: RUN_ID,
    node_id: NODE_ID,
    name: "Upscale",
    fanned: true,
    item_count: 2,
    items: [
      { cog_id: 1, name: "a.png", status: "completed", task_id: 101, input: null, output: null, outputs: [] },
      { cog_id: 2, name: "b.png", status: itemStatus, task_id: 102, input: null, output: null, outputs: [] },
    ],
  };
}

describe("FanoutModal poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;
  let fanoutStatus: number;
  let itemStatus: string;
  let now: number;

  const fanoutCalls = () =>
    fetchMock.mock.calls.filter((c) => String(c[0]).includes(FANOUT_PATH)).length;

  async function advanceTo(t: number): Promise<void> {
    const delta = t - now;
    now = t;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(delta);
    });
  }

  function setVisibility(hidden: boolean): void {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => (hidden ? "hidden" : "visible"),
    });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  }

  function renderModal(props: Partial<React.ComponentProps<typeof FanoutModal>> = {}) {
    return render(
      <FanoutModal
        open
        onClose={() => {}}
        runId={RUN_ID}
        nodeId={NODE_ID}
        nodeName="Upscale"
        apiUrl="http://api.test"
        accessToken="tok"
        {...props}
      />,
    );
  }

  beforeEach(() => {
    jest.useFakeTimers();
    now = 0;
    fanoutStatus = 200;
    itemStatus = "running";
    fetchMock = jest.fn(async (url: string) => {
      if (String(url).includes(FANOUT_PATH)) {
        return fanoutStatus === 200 ? makeResponse(200, fanoutBody(itemStatus)) : makeResponse(fanoutStatus);
      }
      return makeResponse(404);
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    setVisibility(false);
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("polls the node fan-out at the 3s base interval while any item is live", async () => {
    const { unmount } = renderModal();
    await advanceTo(1);
    // t=0: initial-load effect + the poller's immediate tick.
    expect(fanoutCalls()).toBe(2);
    expect(fetchMock.mock.calls[0][0]).toBe(`http://api.test${FANOUT_PATH}`);
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer tok");
    expect(await screen.findByText("b.png")).toBeInTheDocument();
    expect(screen.getByText("1/2 done")).toBeInTheDocument();

    await advanceTo(2999); // just before the 3s edge (first tick fired at t=0)
    expect(fanoutCalls()).toBe(2);
    await advanceTo(3000); // cross the edge
    expect(fanoutCalls()).toBe(3);
    await advanceTo(5999);
    expect(fanoutCalls()).toBe(3);
    await advanceTo(6000);
    expect(fanoutCalls()).toBe(4);
    unmount();
  });

  it("does not poll while closed or without a token, and stops once every item is terminal", async () => {
    const { rerender, unmount } = renderModal({ open: false });
    await advanceTo(30000);
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(
      <FanoutModal open onClose={() => {}} runId={RUN_ID} nodeId={NODE_ID} apiUrl="http://api.test" accessToken={null} />,
    );
    await advanceTo(60000);
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(
      <FanoutModal open onClose={() => {}} runId={RUN_ID} nodeId={NODE_ID} apiUrl="http://api.test" accessToken="tok" />,
    );
    // The poller has been running since t=30000 (opening enables it); its
    // token-less ticks early-return as successes, so it is on the 3s base
    // cadence (next tick t=63000). Only the initial-load effect fetches now;
    // the poller's next tick picks up the new `load` via the hook's fnRef.
    await advanceTo(60001);
    expect(fanoutCalls()).toBe(1);
    await advanceTo(63001);
    expect(fanoutCalls()).toBe(2);

    // The last live item finishes -> anyLive false -> poller torn down.
    itemStatus = "completed";
    await advanceTo(66001);
    expect(fanoutCalls()).toBe(3);
    await advanceTo(120000);
    expect(fanoutCalls()).toBe(3);
    unmount();
  });

  it("backs off on repeated 5xx, keeps probing after the banner (no dead-stop) and resets to the 3s base on success", async () => {
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (6s, 12s, 24s, 48s, then capped at the 60s default maxMs).
    jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const { unmount } = renderModal();

    await advanceTo(1); // t=0 success (initial load + first poll) -> items render
    expect(fanoutCalls()).toBe(2);
    expect(await screen.findByText("b.png")).toBeInTheDocument();

    fanoutStatus = 502;
    await advanceTo(3001); // t=3s: failure 1
    expect(fanoutCalls()).toBe(3);

    // Backoff: the next probe is NOT at the 3s base cadence...
    await advanceTo(6001);
    expect(fanoutCalls()).toBe(3);
    // ...but ~6s after the failure (doubled).
    await advanceTo(9001); // failure 2 (next ~12s later)
    expect(fanoutCalls()).toBe(4);
    await advanceTo(20000);
    expect(fanoutCalls()).toBe(4);
    await advanceTo(21001); // failure 3 (next ~24s later)
    expect(fanoutCalls()).toBe(5);
    await advanceTo(44000);
    expect(fanoutCalls()).toBe(5);
    await advanceTo(45001); // failure 4 (next ~48s later)
    expect(fanoutCalls()).toBe(6);
    expect(lost).not.toHaveBeenCalled();
    await advanceTo(92000);
    expect(fanoutCalls()).toBe(6);
    await advanceTo(93001); // failure 5 -> connection-lost banner, probe at maxMs
    expect(fanoutCalls()).toBe(7);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at ~maxMs, banner fires once.
    await advanceTo(152000);
    expect(fanoutCalls()).toBe(7);
    await advanceTo(153001); // failure 6
    expect(fanoutCalls()).toBe(8);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good item list is retained on transient errors.
    expect(screen.getByText("b.png")).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 3s base.
    fanoutStatus = 200;
    await advanceTo(212000);
    expect(fanoutCalls()).toBe(8);
    await advanceTo(213001); // success
    expect(fanoutCalls()).toBe(9);
    await advanceTo(216001); // 3s base again
    expect(fanoutCalls()).toBe(10);
    await advanceTo(219001);
    expect(fanoutCalls()).toBe(11);
    expect(lost).toHaveBeenCalledTimes(1);

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  it("pauses polling while the tab is hidden and resumes on return to visible", async () => {
    const { unmount } = renderModal();
    await advanceTo(1);
    expect(fanoutCalls()).toBe(2);

    setVisibility(true);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advanceTo(60000); // hidden: no polling
    expect(fanoutCalls()).toBe(2);

    setVisibility(false);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advanceTo(60800); // resume stagger window -> one refresh
    expect(fanoutCalls()).toBe(3);
    await advanceTo(63800); // then back on the 3s base cadence
    expect(fanoutCalls()).toBe(4);
    unmount();
  });

  it("stops polling after unmount (no further fetches)", async () => {
    const { unmount } = renderModal();
    await advanceTo(1);
    expect(fanoutCalls()).toBe(2);
    unmount();
    await advanceTo(30001); // well past several 3s intervals: nothing new
    expect(fanoutCalls()).toBe(2);
  });
});
