/**
 * 2400UI24 — executeWorkflow() must never fetch /tenant/warnings on CE.
 *
 * /tenant/warnings is PAID-only: CORE/API/main.py keeps its mount inside
 * _CLOUD_PAID_ROUTER_MOUNTS and open-core/manifest.yaml denies
 * CORE/API/routers/tenant/*.py, so on CE the route is absent and the WB
 * Execute pre-check 404s on every run (the e2e net-guard CORE/e2e/fixtures/
 * net.ts:62 flags any 404 under /v1/ -> wb-run-surface.spec.ts red).
 *
 * The fix bails BEFORE the request when the edition positively resolves to
 * "ce" — the SAME source of truth every other CE gate in the builder uses
 * (hooks/use-entitlements.ts -> lib/entitlements.ts loadEntitlements over the
 * shared /platform/features cache; components/node-library.tsx:179 `isCE`).
 * Fail-open is preserved: an unresolved/errored block leaves edition null,
 * so paid behaviour (the fetch still runs) is unchanged.
 *
 * Harness reused from __tests__/useWorkflowApiUnsavedAfterSave.test.tsx: the
 * REAL hook runs under renderHook; only global.fetch is mocked and routed per
 * URL. The /platform/features payload decides the edition, so the cache is
 * reset per test (lib/platform-features.ts _resetPlatformFeaturesCache).
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";

function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const FEATURES_URL = /\/platform\/features$/;
const WARNINGS_URL = /\/tenant\/warnings$/;
const RUN_URL = /\/road\/workflow-versions\/\d+\/run$/;
const VERSIONS_URL = /\/road\/\d+\/versions$/;
const CREATE_URL = /\/road\/$/;

let warningsCalls: number;
let runCalls: number;
let featuresPayload: unknown;

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

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

// Save an already-dirty graph through the PII "Save as-is" path, then leave a
// re-render between the save and the Execute click (the real app always has
// one; it is what refreshes the hook body, and with it the edition read).
async function mountAndSave(): Promise<any> {
  const hook = renderHook(() => useWorkflowApi(inputNodes(), [], () => {}, () => {}, {}));
  await settle();
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
  hook.rerender();
  await settle();
  return hook;
}

describe("useWorkflowApi CE /tenant/warnings pre-check (2400UI24)", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    warningsCalls = 0;
    runCalls = 0;
    _resetPlatformFeaturesCache();
    global.fetch = jest.fn(async (url: string) => {
      const u = String(url);
      if (FEATURES_URL.test(u)) return makeResponse(200, featuresPayload);
      if (WARNINGS_URL.test(u)) { warningsCalls += 1; return makeResponse(200, { warnings: [] }); }
      if (RUN_URL.test(u)) { runCalls += 1; return makeResponse(200, { workflow_run_id: 99, status: "running" }); }
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

  // U1 — CE: the edition resolves to "ce", so the PAID-only route is never
  // requested; Execute still runs the workflow.
  it("CE: Execute makes no request to /tenant/warnings and still runs", async () => {
    featuresPayload = { entitlements: { edition: "ce", capabilities: ["marketplace_publish"] } };
    const hook = await mountAndSave();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(warningsCalls).toBe(0);
    expect(runCalls).toBe(1);
    expect(hook.result.current.saveBeforeRunOpen).toBe(false);
  });

  // U2 — paid: behaviour is identical to before the fix — the warning check
  // runs first, then the run request.
  it("paid edition: Execute still checks /tenant/warnings before running", async () => {
    featuresPayload = { entitlements: { edition: "pro", capabilities: ["marketplace_publish"] } };
    const hook = await mountAndSave();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(warningsCalls).toBe(1);
    expect(runCalls).toBe(1);
    expect(hook.result.current.saveBeforeRunOpen).toBe(false);
  });

  // U3 — fail-CLOSED witness (2400UI24b): an unresolved block (edition null)
  // must NOT request the paid-only route either. On CE the features read can
  // still be loading/errored at Execute time, and the 404 the old fail-open
  // predicate produced is exactly what the e2e net-guard (CORE/e2e/fixtures/
  // net.ts) flags — Execute still runs the workflow.
  it("unresolved entitlements: no request to /tenant/warnings, run still fires", async () => {
    featuresPayload = { entitlements: { edition: "pro", error: "unresolved" } };
    const hook = await mountAndSave();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(warningsCalls).toBe(0);
    expect(runCalls).toBe(1);
  });

  // U4 — the edition resolves to "ce" only AFTER the first render (the live
  // CE app: the features block lands later than the hook's first pass). The
  // Execute click must still make 0 warnings calls, i.e. the predicate is read
  // fresh at Execute time, not frozen at the hook's first render.
  it("edition resolves to ce after first render: Execute makes no warnings call", async () => {
    featuresPayload = { entitlements: { edition: "pro", error: "unresolved" } };
    const hook = await mountAndSave();
    await settle();
    expect(warningsCalls).toBe(0);
    // Second /platform/features load now resolves the block as CE.
    _resetPlatformFeaturesCache();
    featuresPayload = { entitlements: { edition: "ce", capabilities: ["marketplace_publish"] } };
    hook.rerender();
    await settle();
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(warningsCalls).toBe(0);
    expect(runCalls).toBe(1);
  });
});
