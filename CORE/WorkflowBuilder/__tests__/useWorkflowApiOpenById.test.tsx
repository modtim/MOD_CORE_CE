/**
 * WB deep-link / bought-workflow open — useWorkflowApi.openWorkflowById.
 *
 * The `?workflow=<id>` landing (useOpenWorkflowFromQuery) hands the fetched
 * WorkflowSummary to this handler. Before the fix the wiring pointed at
 * handleWorkflowSelection, which only populates the Load dialog's version
 * list — leaving a blank canvas (no name, no nodes, no dialog) because the
 * dialog was never opened. This test pins the intended behaviour: the
 * workflow's LATEST version (versions come back ordered version_number desc,
 * so data[0]) is loaded straight onto the canvas, the name is set, and the
 * Load dialog stays closed. Also covers the sealed + zero-version branches.
 */

import { act, renderHook } from "@testing-library/react";

const toastMock = jest.fn();
jest.mock("@/components/ui/use-toast", () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

import { useWorkflowApi } from "@/hooks/useWorkflowApi";

const WF_ID = 42;

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const NODES = [
  { id: "n1", position: { x: 0, y: 0 }, data: { label: "A" } },
  { id: "n2", position: { x: 10, y: 0 }, data: { label: "B" } },
] as any[];
const EDGES = [{ id: "e1", source: "n1", target: "n2" }] as any[];

// Two versions, highest version_number first (matches API desc ordering).
const V2 = { id: 11, workflow_id: WF_ID, version_number: 2, graph_json: { nodes: NODES, edges: EDGES } };
const V1 = { id: 10, workflow_id: WF_ID, version_number: 1, graph_json: { nodes: NODES, edges: EDGES } };

async function flush(): Promise<void> {
  for (let i = 0; i < 15; i += 1) await act(async () => { await Promise.resolve(); });
}

async function mount(versionsBody: unknown, wfSummary: any) {
  // Adopt the token without a /me round-trip, like the dashboard handoff.
  window.history.replaceState({}, "", "/?token=tok");
  const setNodes = jest.fn();
  const setEdges = jest.fn();
  const fetchMock = jest.fn(async (url: string) => {
    const u = String(url);
    if (u.includes(`/road/${WF_ID}/versions`)) {
      return makeResponse(typeof versionsBody === "object" && versionsBody !== null && "status" in versionsBody
        ? (versionsBody as any).status : 200,
        typeof versionsBody === "object" && versionsBody !== null && "body" in versionsBody
          ? (versionsBody as any).body : versionsBody);
    }
    if (u.includes("/nodes/")) return makeResponse(200, []);
    if (u.includes(`/road/${WF_ID}/runs`)) return makeResponse(200, []);
    return makeResponse(200, {});
  });
  global.fetch = fetchMock as unknown as typeof fetch;

  const hook = renderHook(() => useWorkflowApi([], [], setNodes, setEdges, {} as any));
  await flush();
  expect(hook.result.current.accessToken).toBe("tok");

  await act(async () => {
    await hook.result.current.openWorkflowById(wfSummary);
  });
  await flush();
  return { hook, setNodes, setEdges, fetchMock };
}

describe("useWorkflowApi.openWorkflowById (deep link / bought workflow)", () => {
  beforeEach(() => {
    toastMock.mockClear();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("loads the LATEST version onto the canvas, sets the name, keeps the dialog closed", async () => {
    const wf = { id: WF_ID, name: "Purchased Flow" };
    const { hook, setNodes, setEdges } = await mount([V2, V1], wf);

    // Latest (version_number 2) selected — not the lower one.
    expect(hook.result.current.currentVersionId).toBe(11);
    expect(hook.result.current.currentVersionNumber).toBe(2);
    expect(hook.result.current.currentWorkflow?.id).toBe(WF_ID);
    // Name pill set, Load dialog NOT opened.
    expect(hook.result.current.workflowName).toBe("Purchased Flow");
    expect(hook.result.current.isLoadDialogOpen).toBe(false);
    // Graph landed on the canvas.
    expect(setNodes).toHaveBeenCalled();
    const lastNodes = setNodes.mock.calls[setNodes.mock.calls.length - 1][0];
    expect(Array.isArray(lastNodes) ? lastNodes.length : (lastNodes as any)?.length).toBe(2);
    expect(setEdges).toHaveBeenCalled();
    expect(hook.result.current.versions).toHaveLength(2);
  });

  it("seals a SEALED installed copy (403 'sealed') — no graph, app-form flag set", async () => {
    const wf = { id: WF_ID, name: "Sealed App" };
    const { hook } = await mount({ status: 403, body: { detail: "sealed" } }, wf);

    expect(hook.result.current.sealedWorkflowId).toBe(WF_ID);
    expect(hook.result.current.currentWorkflow?.id).toBe(WF_ID);
    expect(hook.result.current.workflowName).toBe("Sealed App");
    expect(hook.result.current.currentVersionId).toBeNull();
    expect(hook.result.current.isLoadDialogOpen).toBe(false);
  });

  it("routes a 403 workflow_access_denied to the access-denied banner, not the canvas", async () => {
    const wf = { id: WF_ID, name: "Locked" };
    const { hook } = await mount(
      { status: 403, body: { detail: { code: "workflow_access_denied", workflow_name: "Locked" } } },
      wf,
    );

    expect(hook.result.current.accessDenied).toEqual({ workflowId: WF_ID, workflowName: "Locked" });
    expect(hook.result.current.sealedWorkflowId).toBeNull();
    expect(hook.result.current.currentWorkflow).toBeNull();
  });

  it("a workflow with zero versions opens empty, name set, no dialog", async () => {
    const wf = { id: WF_ID, name: "Brand New" };
    const { hook, setNodes } = await mount([], wf);

    expect(hook.result.current.currentWorkflow?.id).toBe(WF_ID);
    expect(hook.result.current.workflowName).toBe("Brand New");
    expect(hook.result.current.currentVersionId).toBeNull();
    expect(hook.result.current.currentVersionNumber).toBeNull();
    expect(hook.result.current.isLoadDialogOpen).toBe(false);
    const lastNodes = setNodes.mock.calls[setNodes.mock.calls.length - 1][0];
    expect(Array.isArray(lastNodes) ? lastNodes.length : 0).toBe(0);
  });
});
