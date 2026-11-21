/**
 * Task 4.11 — representative component integration test (WS1.9).
 *
 * the internal design notes. GridView polls
 * `/road/{id}/runs-grid` every 4s through the shared `usePollingBackoff`
 * hook (which wraps `Poller`) while its dialog is open. This renders the REAL
 * component with only global `fetch` mocked (the network boundary) and fake
 * timers, and proves the grid poll inherits: 4s base cadence when healthy,
 * enable gating (dialog open; no request without workflowId + token), backoff
 * on repeated 5xx, the self-heal contract (banner once, keep probing at maxMs —
 * no dead-stop), reset to the 4s base on the first success, and
 * pause-when-hidden.
 *
 * NB: opening the dialog issues TWO requests at t=0 — the spinner `useEffect`
 * fetch plus the poller's immediate first tick. Every count below accounts for
 * that; subsequent ticks are poller-only.
 */
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import GridView from "@/components/GridView";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const WORKFLOW_ID = 7;
const GRID_PATH = `/road/${WORKFLOW_ID}/runs-grid`;

const GRID = {
  workflow_id: WORKFLOW_ID,
  nodes: [
    { graph_node_id: "node_1", name: "Ingest" },
    { graph_node_id: "node_2", name: "Upscale" },
  ],
  runs: [
    {
      id: 101,
      run_name: "nightly",
      status: "success",
      started_at: "2026-09-24T00:00:00Z",
      version_number: 1,
      cells: { node_1: "success", node_2: "success" },
    },
  ],
};

type GridProps = Partial<React.ComponentProps<typeof GridView>>;

function gridView(props: GridProps = {}): React.ReactElement {
  return (
    <GridView
      workflowId={WORKFLOW_ID}
      accessToken="tok"
      apiUrl="http://api.test"
      selectedRunId={null}
      onSelectRun={() => {}}
      {...props}
    />
  );
}

describe("GridView poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;
  let gridStatus: number;
  let now: number;

  const gridCalls = (): number =>
    fetchMock.mock.calls.filter((c) => String(c[0]).includes(GRID_PATH)).length;

  async function advanceTo(t: number): Promise<void> {
    const delta = t - now;
    now = t;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(delta);
    });
  }

  async function openGrid(): Promise<void> {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    });
  }

  beforeEach(() => {
    jest.useFakeTimers();
    now = 0;
    gridStatus = 200;
    fetchMock = jest.fn(async (url: string) => {
      if (String(url).includes(GRID_PATH)) {
        return gridStatus === 200 ? makeResponse(200, GRID) : makeResponse(gridStatus);
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

  it("polls GET /road/{id}/runs-grid at the 4s base interval while the dialog is open", async () => {
    const { unmount } = render(gridView());
    await advanceTo(10000);
    expect(gridCalls()).toBe(0); // closed dialog: no polling

    now = 0; // re-anchor the clock at the moment the dialog opens
    await openGrid();
    await advanceTo(1); // t=0: spinner fetch + poller's immediate tick
    expect(gridCalls()).toBe(2);
    expect(fetchMock.mock.calls[0][0]).toContain(`${GRID_PATH}?limit=30`);
    expect(await screen.findByText("Ingest")).toBeInTheDocument();

    await advanceTo(3999); // just before the 4s edge (first tick fired at t=0)
    expect(gridCalls()).toBe(2);
    await advanceTo(4000); // cross the edge
    expect(gridCalls()).toBe(3);
    await advanceTo(7999);
    expect(gridCalls()).toBe(3);
    await advanceTo(8000);
    expect(gridCalls()).toBe(4);
    unmount();
  });

  it("gates polling on the dialog being open and never requests without a token", async () => {
    const { unmount, rerender } = render(gridView({ accessToken: null }));
    await openGrid();
    await advanceTo(30000);
    expect(fetchMock).not.toHaveBeenCalled(); // open, but no token → no request

    // Supplying the token: the spinner effect refetches immediately and the
    // running poller picks up the new closure on its next 4s tick.
    rerender(gridView());
    await advanceTo(30001);
    expect(gridCalls()).toBe(1);
    await advanceTo(34001);
    expect(gridCalls()).toBe(2);

    // Closing the dialog disables the poller → no further requests.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
    });
    await advanceTo(90000);
    expect(gridCalls()).toBe(2);
    unmount();
  });

  it("backs off on 5xx, fires the connection-lost banner once, keeps probing at maxMs and resets to base on recovery", async () => {
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (8s, 16s, 32s, then capped at the 60s default maxMs), slightly
    // under it, so every edge below is crossed with a small margin.
    jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const { unmount } = render(gridView());

    await openGrid();
    await advanceTo(1); // t=0: both opening requests succeed → grid renders
    expect(gridCalls()).toBe(2);
    expect(await screen.findByText("Ingest")).toBeInTheDocument();

    gridStatus = 502;
    await advanceTo(4001); // t=4s: failure 1 (next ~8s later)
    expect(gridCalls()).toBe(3);
    // Backoff: the next probe is NOT at the 4s base cadence...
    await advanceTo(8001);
    expect(gridCalls()).toBe(3);
    // ...but ~8s after the failure (doubled).
    await advanceTo(12001); // failure 2 (next ~16s later)
    expect(gridCalls()).toBe(4);
    await advanceTo(27000);
    expect(gridCalls()).toBe(4);
    await advanceTo(28001); // failure 3 (next ~32s later)
    expect(gridCalls()).toBe(5);
    await advanceTo(59000);
    expect(gridCalls()).toBe(5);
    await advanceTo(60001); // failure 4 (next ~60s later, capped at maxMs)
    expect(gridCalls()).toBe(6);
    expect(lost).not.toHaveBeenCalled();
    await advanceTo(119000);
    expect(gridCalls()).toBe(6);
    await advanceTo(120001); // failure 5 → connection-lost banner, probe at maxMs
    expect(gridCalls()).toBe(7);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at ~maxMs and the banner fires once.
    await advanceTo(179000);
    expect(gridCalls()).toBe(7);
    await advanceTo(180001); // failure 6
    expect(gridCalls()).toBe(8);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good grid is retained on transient errors.
    expect(screen.getByText("Ingest")).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 4s base.
    gridStatus = 200;
    await advanceTo(239000);
    expect(gridCalls()).toBe(8);
    await advanceTo(240001); // success
    expect(gridCalls()).toBe(9);
    await advanceTo(244001); // 4s base again
    expect(gridCalls()).toBe(10);
    await advanceTo(248001);
    expect(gridCalls()).toBe(11);
    expect(lost).toHaveBeenCalledTimes(1);

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  it("pauses polling while the tab is hidden and resumes on return to visible", async () => {
    const { unmount } = render(gridView());
    await openGrid();
    await advanceTo(1);
    expect(gridCalls()).toBe(2);

    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advanceTo(30001); // hidden: no polling
    expect(gridCalls()).toBe(2);

    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advanceTo(30801); // resume stagger window → one refresh
    expect(gridCalls()).toBe(3);
    unmount();
  });

  it("stops polling after unmount (no further fetches)", async () => {
    const { unmount } = render(gridView());
    await openGrid();
    await advanceTo(1);
    expect(gridCalls()).toBe(2);
    unmount();
    await advanceTo(30001); // well past several 4s intervals: nothing new
    expect(gridCalls()).toBe(2);
  });
});
