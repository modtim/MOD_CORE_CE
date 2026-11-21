/**
 * CAT-371: After cancel succeeds, the poller must still be able to finish.
 * The fix removes the premature `setPollingRunId(null)` in cancelActiveRun,
 * allowing the poller's `isFinished` check at :784 to clear it once the run
 * reaches a terminal state. A 30-poll guard (MAX_POLLS_AFTER_CANCEL) also
 * exists to prevent infinite polling if the backend leaves the run CANCELLING.
 *
 * Harness: renderHook + rerender to set initial state.
 * The hook gets its access token the way the app does: the Dashboard handoff
 * puts it in the URL (beforeEach replaceState), which the hook's initializeAuth
 * effect at :413 reads via parseHandoffTokens and stores through the jest-mocked
 * api-client saveWorkflowTokens/getAccessToken (the sessionStorage source at
 * :434). No hook-internal setter is called from the test.
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";
import * as apiClient from "@/lib/api-client";

const toastMock = jest.fn();
jest.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => toastMock(...args),
}));

let _token: string | null = null;
jest.mock("@/lib/api-client", () => {
  const actual = jest.requireActual<typeof apiClient>("@/lib/api-client");
  return {
    ...actual,
    getAccessToken: () => _token,
    saveWorkflowTokens: (t: string, _r: string) => { _token = t; },
  };
});

function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const CANCEL_URL = /\/road\/workflow-runs\/\d+\/cancel$/;
const RUNS_URL = /\/runs\/\d+$/;
const RUN_URL = /\/road\/workflow-versions\/\d+\/run$/;
const FEATURES_URL = /\/platform\/features$/;

let runCalls: number;

async function advance(ms: number): Promise<void> {
  await act(async () => { await jest.advanceTimersByTimeAsync(ms); });
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await advance(0);
}

describe("CAT-371: cancel then poll finishes with FAILED status", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    toastMock.mockClear();
    runCalls = 0;
    _token = "tok";
    _resetPlatformFeaturesCache();
    global.fetch = jest.fn(async (url: string) => {
      const u = String(url);
      if (CANCEL_URL.test(u)) {
        return makeResponse(200, { success: true });
      }
      if (RUNS_URL.test(u)) {
        runCalls += 1;
        return makeResponse(200, [
          {
            id: 1,
            workflow_node_id: 10,
            status: "running",
            workflow_node: { graph_node_id: "n1" },
          },
        ]);
      }
      if (RUN_URL.test(u)) {
        return makeResponse(200, { workflow_run_id: 99, status: "running" });
      }
      if (FEATURES_URL.test(u)) return makeResponse(200, { entitlements: { edition: "ce", capabilities: [] } });
      return makeResponse(200, []);
    }) as unknown as typeof global.fetch;
    window.history.replaceState({}, "", "/#token=tok");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("cancel succeeds, next poll returns FAILED → polling stops", async () => {
    const hook = renderHook(() =>
      useWorkflowApi(
        [{ id: "n1", type: "processing", position: { x: 0, y: 0 }, data: { label: "Blur", node_kind: "image_blur" } }],
        [],
        jest.fn(),
        jest.fn(),
        {}
      )
    );
    await settle();

    // Manually set the state that a running workflow would have
    hook.rerender();
    await act(async () => { hook.result.current.selectRun(99); });
    await settle();
    await advance(2000);
    await settle();

    expect(hook.result.current.pollingRunId).toBe(99);

    // Cancel the run
    await act(async () => { await hook.result.current.cancelActiveRun(); });
    await settle();
    expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ title: "Workflow Cancelled" }));
    // After cancel: pollingRunId is NOT set to null (the fix)
    expect(hook.result.current.pollingRunId).toBe(99);

    // Now make the next poll return FAILED
    (global.fetch as any).mockImplementation(async (url: string) => {
      const u = String(url);
      if (CANCEL_URL.test(u)) return makeResponse(200, { success: true });
      if (RUNS_URL.test(u)) {
        return makeResponse(200, [
          {
            id: 1,
            workflow_node_id: 10,
            status: "failure",
            workflow_node: { graph_node_id: "n1" },
          },
        ]);
      }
      if (RUN_URL.test(u)) return makeResponse(200, { workflow_run_id: 99, status: "running" });
      if (FEATURES_URL.test(u)) return makeResponse(200, { entitlements: { edition: "ce", capabilities: [] } });
      return makeResponse(200, []);
    });

    // Advance the poll (poll every 2s)
    await advance(2000);
    await settle();

    // The poller should have cleared pollingRunId because the run is terminal (failure)
    expect(hook.result.current.pollingRunId).toBe(null);
  });

  it("cancel then 30+ polls → poll guard stops polling", async () => {
    const hook = renderHook(() =>
      useWorkflowApi(
        [{ id: "n1", type: "processing", position: { x: 0, y: 0 }, data: { label: "Blur", node_kind: "image_blur" } }],
        [],
        jest.fn(),
        jest.fn(),
        {}
      )
    );
    await settle();

    hook.rerender();
    await act(async () => { hook.result.current.selectRun(99); });
    await settle();
    await advance(2000);
    await settle();
    expect(hook.result.current.pollingRunId).toBe(99);

    // Cancel
    await act(async () => { await hook.result.current.cancelActiveRun(); });
    await settle();

    // Now make every poll return a non-terminal status (CANCELLING)
    (global.fetch as any).mockImplementation(async (url: string) => {
      const u = String(url);
      if (CANCEL_URL.test(u)) return makeResponse(200, { success: true });
      if (RUNS_URL.test(u)) {
        return makeResponse(200, [
          {
            id: 1,
            workflow_node_id: 10,
            status: "queued", // non-terminal
            workflow_node: { graph_node_id: "n1" },
          },
        ]);
      }
      if (RUN_URL.test(u)) return makeResponse(200, { workflow_run_id: 99, status: "running" });
      if (FEATURES_URL.test(u)) return makeResponse(200, { entitlements: { edition: "ce", capabilities: [] } });
      return makeResponse(200, []);
    });

    // Advance 30 polls (2s each)
    for (let i = 0; i < 30; i++) {
      await advance(2000);
      await settle();
    }

    // After 30 polls, the guard should have stopped polling
    expect(hook.result.current.pollingRunId).toBe(null);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Run Cancelled" })
    );
  });
});
