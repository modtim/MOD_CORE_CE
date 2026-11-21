/**
 * Task 4.11 — representative component integration test.
 *
 * the internal design notes. RunRail is the reference
 * migrated site (runs list, 4s). It renders the REAL component with a mocked
 * global `fetch` and fake timers, proving the migrated site (not just the hook)
 * inherits: base 4s cadence when healthy, enable/disable gating, backoff on
 * repeated 5xx with the self-heal contract (AUTH_CONNECTION_LOST_EVENT fires
 * once, probing continues at maxMs, cadence resets to base on the first
 * success) while keeping the last-rendered list, and pause-when-hidden.
 */
import React from "react";
import { act, render, screen } from "@testing-library/react";
import RunRail from "@/components/RunRail";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

function makeResponse(status: number, body: unknown = []): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

const RUNS = [{ id: 7, status: "running", run_name: "nightly" }];

function renderRail(props: Partial<React.ComponentProps<typeof RunRail>> = {}) {
  return render(
    <RunRail
      workflowId={123}
      accessToken="tok"
      apiUrl="http://api.test"
      selectedRunId={null}
      onSelectRun={() => {}}
      {...props}
    />,
  );
}

describe("RunRail poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    jest.useFakeTimers();
    fetchMock = jest.fn().mockResolvedValue(makeResponse(200, RUNS));
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("polls GET /road/{id}/runs at the 4s base interval when healthy", async () => {
    const { unmount } = renderRail();
    await advance(1); // immediate tick on mount
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/road/123/runs");
    await advance(3998); // just before the 4s edge — not polling faster than base
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(2); // cross the edge
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    unmount();
  });

  it("does not poll until both workflowId and accessToken are present (enable gating)", async () => {
    const { unmount, rerender } = renderRail({ accessToken: null });
    await advance(20000);
    expect(fetchMock).not.toHaveBeenCalled();
    // Supplying the token flips `enabled` true → polling starts.
    rerender(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} />,
    );
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("backs off on 5xx, fires the connection-lost banner once, keeps probing at maxMs and resets to base on recovery", async () => {
    // First tick succeeds so the list renders; then the API storms with 502.
    fetchMock
      .mockResolvedValueOnce(makeResponse(200, RUNS))
      .mockResolvedValue(makeResponse(502));
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (8s, 16s, 32s, then capped at the 60s default maxMs), slightly
    // under it, so every edge below is crossed with a small margin.
    const rand = jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    let now = 0;
    const advanceTo = async (t: number): Promise<void> => {
      const delta = t - now;
      now = t;
      await advance(delta);
    };
    const { unmount } = renderRail();

    await advanceTo(1); // t=0 success → list populates
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/1 live/)).toBeInTheDocument();

    await advanceTo(4001); // t=4s: failure 1 (next ~8s later)
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Backoff: the next probe is NOT at the 4s base cadence...
    await advanceTo(8001);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // ...but ~8s after the failure (doubled).
    await advanceTo(12001); // failure 2 (next ~16s later)
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await advanceTo(27000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await advanceTo(28001); // failure 3 (next ~32s later)
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await advanceTo(59000);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await advanceTo(60001); // failure 4 (next ~60s later, capped at maxMs)
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(lost).not.toHaveBeenCalled();
    await advanceTo(119000);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await advanceTo(120001); // failure 5 → connection-lost banner, probe at maxMs
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at ~maxMs and the banner fires once.
    await advanceTo(179000);
    expect(fetchMock).toHaveBeenCalledTimes(6);
    await advanceTo(180001); // failure 6
    expect(fetchMock).toHaveBeenCalledTimes(7);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good list is retained (never cleared on transient errors).
    expect(screen.getByText(/1 live/)).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 4s base.
    fetchMock.mockResolvedValue(makeResponse(200, RUNS));
    await advanceTo(239000);
    expect(fetchMock).toHaveBeenCalledTimes(7);
    await advanceTo(240001); // success
    expect(fetchMock).toHaveBeenCalledTimes(8);
    await advanceTo(244001); // 4s base again
    expect(fetchMock).toHaveBeenCalledTimes(9);
    await advanceTo(248001);
    expect(fetchMock).toHaveBeenCalledTimes(10);
    expect(lost).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/1 live/)).toBeInTheDocument();

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    rand.mockRestore();
    unmount();
  });

  it("404 stops polling for the deleted workflow inside RunRail, then restarts on new workflowId (2400UI126b)", async () => {
    const { rerender } = renderRail();
    await advance(1); // immediate tick
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Now the API returns 404 (workflow deleted).
    fetchMock.mockResolvedValue(makeResponse(404, []));
    await advance(4001); // next tick
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // No more polls — RunRail self-stops on 404, even after multiple intervals.
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Rerender with a new workflowId → polling restarts for the new workflow.
    rerender(
      <RunRail
        workflowId={456}
        accessToken="tok"
        apiUrl="http://api.test"
        selectedRunId={null}
        onSelectRun={() => {}}
      />,
    );
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("pauses polling while the tab is hidden and resumes on return to visible", async () => {
    const { unmount } = renderRail();
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Hide the tab.
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(30000); // hidden: no polling
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Return to visible.
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(800); // resume stagger window → one refresh
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
  });
});
