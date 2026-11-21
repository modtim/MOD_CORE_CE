/**
 * Task 4.11 (WS1.25) — hook-level poll-backoff test for useWorkflowApi.
 *
 * the internal design notes. useWorkflowApi runs three
 * polls through the shared usePollingBackoff -> Poller helper:
 *   - node-status     GET /runs/{id}                               2s
 *   - node-controls   GET /road/workflow-runs/{id}/node-controls  3s live / 6s idle
 *   - fanout-summary  GET /runs/{id}/fanout-summary                4s (live runs only)
 *
 * The REAL hook, usePollingBackoff and Poller run here; only the network
 * boundary (global fetch) is mocked, routed per URL. For each poll we prove the
 * base cadence (first tick at t=0, next exactly one base interval later), the
 * exponential backoff on 5xx, and the reset to base cadence on the first success.
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";

const RUN_ID = 7;

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

// Drain pending promises/effects without moving the fake clock.
async function flush(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await advance(0);
}

type Endpoint = "status" | "controls" | "fanout";

const MATCHERS: Record<Endpoint, RegExp> = {
  status: new RegExp(`/runs/${RUN_ID}$`),
  controls: new RegExp(`/road/workflow-runs/${RUN_ID}/node-controls$`),
  fanout: new RegExp(`/runs/${RUN_ID}/fanout-summary$`),
};

const HEALTHY: Record<Endpoint, () => Response> = {
  // Empty NodeRun list = run still live (not finished), so node-status keeps polling.
  status: () => makeResponse(200, []),
  controls: () => makeResponse(200, {}),
  fanout: () => makeResponse(200, {}),
};

// A finished run: every NodeRun terminal -> the hook marks the run inactive.
const FINISHED_RUN = [{ id: 1, workflow_node_id: 1, status: "success", workflow_node: { graph_node_id: "n1" } }];

describe("useWorkflowApi poll-backoff (hook-level)", () => {
  let fetchMock: jest.Mock;
  let responders: Record<Endpoint, () => Response>;

  const count = (ep: Endpoint): number =>
    fetchMock.mock.calls.filter(([url]) => MATCHERS[ep].test(String(url))).length;

  const nodes = [{ id: "n1", position: { x: 0, y: 0 }, data: {} }] as any[];
  const edges: any[] = [];

  async function mountWithSelectedRun() {
    const setNodes = jest.fn();
    const setEdges = jest.fn();
    const hook = renderHook(() => useWorkflowApi(nodes, edges, setNodes, setEdges, {}));
    await flush();
    expect(hook.result.current.accessToken).toBe("tok");
    act(() => {
      hook.result.current.selectRun(RUN_ID);
    });
    await flush();
    return hook;
  }

  beforeEach(() => {
    jest.useFakeTimers();
    responders = { ...HEALTHY };
    fetchMock = jest.fn(async (url: string) => {
      const u = String(url);
      for (const ep of Object.keys(MATCHERS) as Endpoint[]) {
        if (MATCHERS[ep].test(u)) return responders[ep]();
      }
      return makeResponse(200, {});
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    // Dashboard handoff: ?token= is adopted without a /me round-trip.
    window.history.replaceState({}, "", "/?token=tok");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  // Base cadence: the first tick fires at t=0, the next at exactly t=base
  // (not one ms before), then every base interval.
  async function expectBaseCadence(ep: Endpoint, base: number): Promise<void> {
    const c0 = count(ep);
    expect(c0).toBeGreaterThanOrEqual(1); // immediate tick at t=0
    await advance(base - 1);
    expect(count(ep)).toBe(c0);
    await advance(1);
    expect(count(ep)).toBe(c0 + 1);
    await advance(base);
    expect(count(ep)).toBe(c0 + 2);
  }

  // Backoff + reset. Math.random -> 1 pins the jitter to the top of the window,
  // so each backed-off delay equals curDelay exactly (2x, 4x ... base).
  //   t=0     success (immediate tick) -> endpoint starts returning 502
  //   t=1b    fail #1 -> next delay 2b
  //   t=2b    no tick (backed off, not at base cadence)
  //   t=3b    fail #2 -> next delay 4b -> endpoint recovers
  //   t=7b    success -> reset
  //   t=8b    tick at base cadence again
  async function expectBackoffAndReset(ep: Endpoint, base: number): Promise<void> {
    const c0 = count(ep);
    expect(c0).toBeGreaterThanOrEqual(1);
    responders[ep] = () => makeResponse(502);

    await advance(base);
    expect(count(ep)).toBe(c0 + 1); // fail #1

    await advance(base);
    expect(count(ep)).toBe(c0 + 1); // t=2b: backed off, no base-cadence tick
    await advance(base - 1);
    expect(count(ep)).toBe(c0 + 1);
    await advance(1);
    expect(count(ep)).toBe(c0 + 2); // t=3b: fail #2 after a 2x delay

    responders[ep] = HEALTHY[ep];
    await advance(4 * base - 1);
    expect(count(ep)).toBe(c0 + 2); // 4x delay not yet elapsed
    await advance(1);
    expect(count(ep)).toBe(c0 + 3); // t=7b: success

    await advance(base - 1);
    expect(count(ep)).toBe(c0 + 3);
    await advance(1);
    expect(count(ep)).toBe(c0 + 4); // t=8b: reset to base cadence
    await advance(base);
    expect(count(ep)).toBe(c0 + 5);
  }

  describe("node-status (2s)", () => {
    it("polls GET /runs/{id} at the 2s base cadence", async () => {
      const { unmount } = await mountWithSelectedRun();
      await expectBaseCadence("status", 2000);
      unmount();
    });

    it("backs off on 5xx and resets to 2s on the first success", async () => {
      jest.spyOn(Math, "random").mockReturnValue(1);
      const { unmount } = await mountWithSelectedRun();
      await expectBackoffAndReset("status", 2000);
      unmount();
    });

    it("stops polling after unmount (no further fetches)", async () => {
      const { unmount } = await mountWithSelectedRun();
      const c0 = count("status");
      expect(c0).toBeGreaterThanOrEqual(1);
      unmount();
      await advance(20000); // well past several 2s intervals: nothing new
      expect(count("status")).toBe(c0);
    });
  });

  describe("node-controls (3s live / 6s idle)", () => {
    it("polls at the 3s base cadence while the selected run is live", async () => {
      const { result, unmount } = await mountWithSelectedRun();
      expect(result.current.selectedRunActive).toBe(true);
      await expectBaseCadence("controls", 3000);
      unmount();
    });

    it("backs off on 5xx and resets to 3s on the first success (live run)", async () => {
      jest.spyOn(Math, "random").mockReturnValue(1);
      const { unmount } = await mountWithSelectedRun();
      await expectBackoffAndReset("controls", 3000);
      unmount();
    });

    it("slows to the 6s base cadence once the selected run finishes", async () => {
      responders.status = () => makeResponse(200, FINISHED_RUN);
      const { result, unmount } = await mountWithSelectedRun();
      expect(result.current.selectedRunActive).toBe(false);
      expect(result.current.selectedRunId).toBe(RUN_ID);
      await expectBaseCadence("controls", 6000);
      unmount();
    });

    it("backs off on 5xx and resets to 6s on the first success (finished run)", async () => {
      jest.spyOn(Math, "random").mockReturnValue(1);
      responders.status = () => makeResponse(200, FINISHED_RUN);
      const { result, unmount } = await mountWithSelectedRun();
      expect(result.current.selectedRunActive).toBe(false);
      await expectBackoffAndReset("controls", 6000);
      unmount();
    });
  });

  describe("fanout-summary (4s)", () => {
    it("polls GET /runs/{id}/fanout-summary at the 4s base cadence while live", async () => {
      const { unmount } = await mountWithSelectedRun();
      await expectBaseCadence("fanout", 4000);
      unmount();
    });

    it("backs off on 5xx and resets to 4s on the first success", async () => {
      jest.spyOn(Math, "random").mockReturnValue(1);
      const { unmount } = await mountWithSelectedRun();
      await expectBackoffAndReset("fanout", 4000);
      unmount();
    });

    it("stops polling once the selected run is no longer live", async () => {
      responders.status = () => makeResponse(200, FINISHED_RUN);
      const { unmount } = await mountWithSelectedRun();
      const c0 = count("fanout");
      await advance(20000);
      expect(count("fanout")).toBe(c0);
      unmount();
    });
  });
});
