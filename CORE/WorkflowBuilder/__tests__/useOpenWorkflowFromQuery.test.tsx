/**
 * WB "open workflow by id" landing — `?workflow=<id>` on the main page.
 *
 * Dashboard Library's "Open workflow" button points at the WB with
 * `?workflow=<installed_workflow_id>`. This hook loads that workflow exactly
 * as if the user had opened it from the WB's own list (by handing the
 * fetched WorkflowSummary to the same handler the list uses), then strips
 * the `workflow` param so a refresh doesn't re-trigger it.
 */

import { act, renderHook } from "@testing-library/react";

const toastMock = jest.fn();
jest.mock("@/components/ui/use-toast", () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

const authenticatedFetchMock = jest.fn();
jest.mock("@/lib/api-client", () => ({ authenticatedFetch: (...args: unknown[]) => authenticatedFetchMock(...args) }));

jest.mock("@/lib/urls", () => ({ getApiUrl: () => "http://api.test" }));

import { useOpenWorkflowFromQuery } from "@/hooks/useOpenWorkflowFromQuery";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await act(async () => { await Promise.resolve(); });
}

describe("useOpenWorkflowFromQuery", () => {
  beforeEach(() => {
    toastMock.mockClear();
    authenticatedFetchMock.mockReset();
  });

  it("valid id: fetches the workflow, calls the loader once, and strips the param", async () => {
    window.history.replaceState({}, "", "/?workflow=42");
    const workflow = { id: 42, name: "My Workflow", latest_version_id: 1 };
    authenticatedFetchMock.mockResolvedValue(makeResponse(200, workflow));
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
    expect(String(authenticatedFetchMock.mock.calls[0][0])).toBe("http://api.test/road/42");
    expect(onOpenWorkflow).toHaveBeenCalledTimes(1);
    expect(onOpenWorkflow).toHaveBeenCalledWith(workflow);
    expect(window.location.search).toBe("");
    expect(setAccessDenied).not.toHaveBeenCalled();
  });

  it("bad (non-numeric) id: never calls the loader, surfaces an error, and strips the param", async () => {
    window.history.replaceState({}, "", "/?workflow=abc");
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(authenticatedFetchMock).not.toHaveBeenCalled();
    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(toastMock.mock.calls[0][0]).toMatchObject({ variant: "destructive" });
    expect(window.location.search).toBe("");
  });

  it("404: surfaces an error and never calls the loader", async () => {
    window.history.replaceState({}, "", "/?workflow=999");
    authenticatedFetchMock.mockResolvedValue(makeResponse(404));
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(toastMock.mock.calls[0][0]).toMatchObject({ title: "Workflow not found", variant: "destructive" });
    expect(window.location.search).toBe("");
  });

  it("403 workflow_access_denied: routes to setAccessDenied, not a generic toast", async () => {
    window.history.replaceState({}, "", "/?workflow=7");
    authenticatedFetchMock.mockResolvedValue(
      makeResponse(403, { detail: { code: "workflow_access_denied", workflow_name: "Someone Else's Flow" } }),
    );
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(setAccessDenied).toHaveBeenCalledWith({ workflowId: 7, workflowName: "Someone Else's Flow" });
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("does nothing when there is no ?workflow= param", async () => {
    window.history.replaceState({}, "", "/?token=tok");
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(authenticatedFetchMock).not.toHaveBeenCalled();
    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("waits for accessToken before firing", async () => {
    window.history.replaceState({}, "", "/?workflow=42");
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    const { rerender } = renderHook(
      ({ accessToken }) => useOpenWorkflowFromQuery({ accessToken, onOpenWorkflow, setAccessDenied }),
      { initialProps: { accessToken: null as string | null } },
    );
    await flush();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();

    authenticatedFetchMock.mockResolvedValue(makeResponse(200, { id: 42, name: "X", latest_version_id: 1 }));
    rerender({ accessToken: "tok" });
    await flush();

    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
    expect(onOpenWorkflow).toHaveBeenCalledTimes(1);
  });

  it("401 (session died mid-load): routes to setAccessDenied with the GENERIC name, no toast", async () => {
    window.history.replaceState({}, "", "/?workflow=42");
    authenticatedFetchMock.mockResolvedValue(makeResponse(401));
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: "tok", onOpenWorkflow, setAccessDenied }));
    await flush();

    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(toastMock).not.toHaveBeenCalled();
    // Q9=B: same generic name the cold-load denial uses — the 401 body is never
    // read, so nothing about the workflow's real name can leak here.
    expect(setAccessDenied).toHaveBeenCalledTimes(1);
    expect(setAccessDenied).toHaveBeenCalledWith({ workflowId: 42, workflowName: "workflow 42" });
    expect(window.location.search).toBe("");
  });

  it("no token: the hook never fires and never denies (cold denial belongs to initializeAuth)", async () => {
    window.history.replaceState({}, "", "/?workflow=42");
    const onOpenWorkflow = jest.fn();
    const setAccessDenied = jest.fn();

    renderHook(() => useOpenWorkflowFromQuery({ accessToken: null, onOpenWorkflow, setAccessDenied }));
    await flush();
    await flush();

    expect(authenticatedFetchMock).not.toHaveBeenCalled();
    expect(onOpenWorkflow).not.toHaveBeenCalled();
    expect(toastMock).not.toHaveBeenCalled();
    expect(setAccessDenied).not.toHaveBeenCalled();
    // The param is NOT stripped: the hook has not tried yet, so a later
    // authenticated load can still act on it.
    expect(window.location.search).toBe("?workflow=42");
  });
});
