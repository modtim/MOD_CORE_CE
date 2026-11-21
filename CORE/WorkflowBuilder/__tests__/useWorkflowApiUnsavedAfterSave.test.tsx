/**
 * BUG-1027t — "Unsaved Changes — save before running?" fired on Execute
 * immediately after a successful save (CE golden path).
 *
 * Root cause (a): the PII "Save as-is" path (confirmSaveAsIs -> _doSaveConfirm)
 * never cleared _isDirty, so the flag stayed true after the save and
 * executeWorkflow opened the saveBeforeRun dialog. Secondary: the hook's
 * `_hasUnsavedChanges` was a render-time snapshot of the ref value rather than
 * a live read.
 *
 * Harness reused from __tests__/useWorkflowApiPollBackoff.test.tsx: the REAL
 * hook runs under renderHook; only global.fetch is mocked and routed per URL.
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";

function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const RUN_URL = /\/road\/workflow-versions\/\d+\/run$/;
const WARNINGS_URL = /\/tenant\/warnings$/;
const VERSIONS_URL = /\/road\/\d+\/versions$/;
const CREATE_URL = /\/road\/$/;

let runCalls: number;

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

// Drain pending promises/effects without moving the fake clock.
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await advance(0);
}

function inputNodes(): any[] {
  return [{
    id: "n1",
    type: "input",
    position: { x: 0, y: 0 },
    data: {
      label: "Input",
      userPrompt: "hello",
      selectedFiles: [{ id: 7, name: "a.mp4" }],
    },
  }];
}

async function mount(): Promise<any> {
  const nodes = inputNodes();
  const hook = renderHook(() => useWorkflowApi(nodes, [], () => {}, () => {}, {}));
  await settle();
  return hook;
}

describe("useWorkflowApi unsaved-changes prompt after save (BUG-1027t)", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    runCalls = 0;
    global.fetch = jest.fn(async (url: string) => {
      const u = String(url);
      if (RUN_URL.test(u)) { runCalls += 1; return makeResponse(200, { workflow_run_id: 99, status: "running" }); }
      if (WARNINGS_URL.test(u)) return makeResponse(200, { warnings: [] });
      if (VERSIONS_URL.test(u)) return makeResponse(200, { id: 21, version_number: 1, workflow_id: 11 });
      if (CREATE_URL.test(u)) return makeResponse(200, { id: 11, name: "W Copy", archived_at: null });
      return makeResponse(200, []);
    }) as unknown as typeof global.fetch;
    window.history.replaceState({}, "", "/?token=tok");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });
  // U1 — CE golden path: dirty graph, saved through the PII "Save as-is"
  // dialog, then Execute. Nothing was edited after the save, so no prompt
  // and the run request goes out.
  it("save-as-is clears dirty; Execute runs without the unsaved prompt", async () => {
    const hook = await mount();
    act(() => { hook.result.current.markDirty(); });
    await settle();
    act(() => { hook.result.current.handleSaveInitiation("saveAs"); });
    await settle();
    act(() => { hook.result.current.handleSaveConfirm(); });
    await settle();
    expect(hook.result.current.isSanitizationDialogOpen).toBe(true);
    act(() => { hook.result.current.confirmSaveAsIs(); });
    await settle();
    expect(hook.result.current.isSaveDialogOpen).toBe(false);
    // A re-render sits between the save and the Execute click in the real app
    // (toast + dialog state changed), which is what refreshed the old render-time
    // snapshot with the still-true dirty flag.
    hook.rerender();
    await settle();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.saveBeforeRunOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U2 — a genuine edit AFTER the save must still prompt.
  it("edit after save still prompts before running", async () => {
    const hook = await mount();
    act(() => { hook.result.current.handleSaveInitiation("saveAs"); });
    await settle();
    act(() => { hook.result.current.handleSaveConfirm(); });
    await settle();
    act(() => { hook.result.current.confirmSaveAsIs(); });
    await settle();
    expect(hook.result.current.saveBeforeRunOpen).toBe(false);
    act(() => { hook.result.current.markDirty(); });
    await settle();
    // A re-render sits between the save and the Execute click in the real app
    // (toast + dialog state changed), which is what refreshed the old render-time
    // snapshot with the still-true dirty flag.
    hook.rerender();
    await settle();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.saveBeforeRunOpen).toBe(true);
    expect(runCalls).toBe(0);
  });

  // U3 — never-saved workflow: the save-required prompt is unchanged.
  it("never-saved workflow still asks to save first", async () => {
    const hook = await mount();
    await settle();
    // A re-render sits between the save and the Execute click in the real app
    // (toast + dialog state changed), which is what refreshed the old render-time
    // snapshot with the still-true dirty flag.
    hook.rerender();
    await settle();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.saveRequiredBeforeRunOpen).toBe(true);
    expect(hook.result.current.saveBeforeRunOpen).toBe(false);
    expect(runCalls).toBe(0);
  });
});
