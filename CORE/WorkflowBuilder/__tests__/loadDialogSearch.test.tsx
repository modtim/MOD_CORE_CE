/**
 * 2400UI127 — WB Load Workflow dialog server-side search.
 *
 * The list endpoint caps at 50 rows sorted by name, so a tenant with more
 * workflows could never reach rows past the 50th. The Load dialog now owns a
 * search Input whose onChange feeds useWorkflowApi.searchWorkflows, which
 * appends `&search=<term>` (an ilike name-substring filter applied BEFORE the
 * limit — workflow_cud.py:552) and debounces keystrokes so typing "abc" costs
 * ONE fetch.
 *
 * Pinned here:
 *   - a burst of keystrokes collapses to a single fetch carrying the newest
 *     term (fake timers drive the 300ms window);
 *   - an empty/whitespace term sends NO `search=` param (default view intact);
 *   - the term keeps the current view's include_archived flag;
 *   - closing the dialog clears the term.
 */

import { act, renderHook } from "@testing-library/react";

const toastMock = jest.fn();
jest.mock("@/components/ui/use-toast", () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

import { useWorkflowApi } from "@/hooks/useWorkflowApi";

function makeResponse(status: number, body: unknown = []): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const WF = (id: number, name: string) =>
  ({ id, name, description: "", is_system: false, archived_at: null } as any);

async function flush(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await act(async () => { await Promise.resolve(); });
}

async function mount(): Promise<any> {
  window.history.replaceState({}, "", "/?token=tok");
  const hook = renderHook(() => useWorkflowApi([], [], jest.fn(), jest.fn(), {} as any));
  await flush();
  expect(hook.result.current.accessToken).toBe("tok");
  // Open the Load dialog — the search box only exists inside it, and closing
  // it is what clears the term.
  await act(async () => {
    void hook.result.current.handleLoadWorkflowList();
  });
  await flush();
  expect(hook.result.current.isLoadDialogOpen).toBe(true);
  return hook;
}

async function typeInto(hook: any, term: string): Promise<void> {
  for (let i = 1; i <= term.length; i += 1) {
    const partial = term.slice(0, i);
    hook.result.current.searchWorkflows(partial);
  }
  act(() => {
    jest.advanceTimersByTime(400);
  });
  await flush();
}

describe("useWorkflowApi.searchWorkflows (Load dialog server search)", () => {
  let urls: string[] = [];

  beforeEach(() => {
    toastMock.mockClear();
    urls = [];
    jest.useFakeTimers();
    global.fetch = (jest.fn(async (url: string) => {
      urls.push(String(url));
      return makeResponse(200, [WF(1, "Actions for e2e-wf-share-alpha"), WF(2, "abc-trainer")]);
    }) as unknown) as typeof fetch;
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("typing `abc` fires ONE fetch carrying search=abc", async () => {
    const hook = await mount();
    const before = urls.length;

    await typeInto(hook, "abc");

    const listFetches = urls.slice(before);
    expect(listFetches).toHaveLength(1);
    expect(listFetches[0]).toContain("search=abc");
    expect(hook.result.current.workflowSearchTerm).toBe("abc");
    expect(hook.result.current.workflows.map((w: any) => w.name)).toContain("abc-trainer");
  });

  it("an empty term sends no search= param", async () => {
    const hook = await mount();
    const before = urls.length;

    await typeInto(hook, "   ");

    const listFetches = urls.slice(before);
    expect(listFetches).toHaveLength(1);
    expect(listFetches[0].includes("search=")).toBe(false);
    expect(hook.result.current.workflowSearchTerm).toBe("");
  });

  it("search keeps the archived view's include_archived flag", async () => {
    const hook = await mount();

    await act(async () => {
      void hook.result.current.toggleArchivedView();
    });
    await flush();
    expect(hook.result.current.archivedView).toBe(true);
    const before = urls.length;

    await typeInto(hook, "old");

    const listFetches = urls.slice(before);
    expect(listFetches).toHaveLength(1);
    expect(listFetches[0]).toContain("include_archived=true");
    expect(listFetches[0]).toContain("search=old");
    expect(hook.result.current.workflowSearchTerm).toBe("old");
  });

  it("closing the Load dialog clears the term", async () => {
    const hook = await mount();

    await typeInto(hook, "abc");
    expect(hook.result.current.workflowSearchTerm).toBe("abc");

    await act(async () => {
      hook.result.current.setLoadDialogOpen(false);
    });
    await flush();
    expect(hook.result.current.isLoadDialogOpen).toBe(false);
    expect(hook.result.current.workflowSearchTerm).toBe("");
  });
});
