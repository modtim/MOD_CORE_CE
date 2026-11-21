/**
 * rc20-batch17 R2 — pausedNodes must sync from the run-rail poll.
 *
 * The "Inspect frame" menu item (workflow-builder.tsx) renders only when
 * selectedRunId != null && pausedNodes.includes(node.id). pausedNodes was
 * written ONLY on the per-node pause-node POST path, so a node held by a
 * server BREAKPOINT (breakpoint_gate appends to WorkflowRun.paused_nodes,
 * never a pause-node call) left pausedNodes empty client-side and the menu
 * never rendered.
 *
 * The node-controls poll response ({ workflow_run_id, controls }) derives its
 * map from db_run.paused_nodes (routers/workflows_domains/workflows.py), so
 * keys mapped to "paused" are the authoritative pause-point set. This test
 * mounts the REAL hook with fetch mocked, selects a run, and proves:
 *   1. a poll whose controls carry a "paused" entry populates pausedNodes;
 *   2. a poll response WITHOUT a controls field does NOT clobber the
 *      optimistic add from _nodePauseCtl.
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

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await advance(0);
}

const CONTROLS_URL = new RegExp(`/road/workflow-runs/${RUN_ID}/node-controls$`);
const PAUSE_URL = new RegExp(`/road/workflow-runs/${RUN_ID}/pause-node$`);

describe("useWorkflowApi pausedNodes poll sync", () => {
  let fetchMock: jest.Mock;
  let controlsBody: unknown;

  const nodes = [{ id: "n1", position: { x: 0, y: 0 }, data: {} }] as any[];
  const edges: any[] = [];

  beforeEach(() => {
    jest.useFakeTimers();
    controlsBody = {};
    fetchMock = jest.fn(async (url: string) => {
      const u = String(url);
      if (PAUSE_URL.test(u)) return makeResponse(200, {});
      if (CONTROLS_URL.test(u)) return makeResponse(200, controlsBody);
      // node-status poll: empty NodeRun list = run still live.
      return makeResponse(200, []);
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    window.history.replaceState({}, "", "/?token=tok");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  async function mountWithSelectedRun() {
    const hook = renderHook(() => useWorkflowApi(nodes, edges, jest.fn(), jest.fn(), {}));
    await flush();
    expect(hook.result.current.accessToken).toBe("tok");
    act(() => {
      hook.result.current.selectRun(RUN_ID);
    });
    await flush();
    return hook;
  }

  it("server breakpoint: poll controls populate pausedNodes", async () => {
    controlsBody = { workflow_run_id: RUN_ID, controls: { n1: "paused" } };
    const hook = await mountWithSelectedRun();
    await advance(100);
    expect(hook.result.current.pausedNodes).toEqual(["n1"]);
  });

  it("absent controls field does not clobber the optimistic pause add", async () => {
    const hook = await mountWithSelectedRun();
    act(() => {
      hook.result.current.pauseNode("n1");
    });
    await flush();
    expect(hook.result.current.pausedNodes).toEqual(["n1"]);
    controlsBody = { workflow_run_id: RUN_ID }; // no controls key at all
    await advance(6000);
    expect(hook.result.current.pausedNodes).toEqual(["n1"]);
  });
});
