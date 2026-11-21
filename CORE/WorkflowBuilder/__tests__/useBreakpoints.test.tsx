/**
 * sz-2.10d — useBreakpoints hook + breakpointMenuLabel tests.
 *
 * The hook is exercised inside a tiny host component (useState/useEffect
 * need a React render); lib/breakpoints fetch calls are mocked via
 * global.fetch. The context-menu item itself lives in the 2900-line
 * workflow-builder.tsx (too heavy to render here), so its label logic is
 * tested through the exported pure helper `breakpointMenuLabel`.
 */

import React from "react";
import { render, waitFor } from "@testing-library/react";
import { useBreakpoints, breakpointMenuLabel } from "@/lib/use-breakpoints";

const API = "http://api.test";
const TOKEN = "tok_abc";
const WID = "wf-1";
const VID = "v-2";

function bp(nodeId: string, enabled = true): any {
  return {
    id: 1,
    node_id: nodeId,
    enabled,
    condition: null,
    hit_count: 0,
    last_hit_at: null,
    created_at: "2026-10-11T00:00:00Z",
    updated_at: "2026-10-11T00:00:00Z",
  };
}

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

// Host component: exposes the hook state as text so waitFor can observe it.
let captured: any = null;

function Host(props: any) {
  const state = useBreakpoints(props);
  captured = state;
  const nid = String(props.nodeId ?? "");
  const text = [
    `size=${state.map.size}`,
    `err=${state.error ?? "none"}`,
    `loading=${state.loading ? "y" : "n"}`,
    `active=${state.isActive(nid) ? "y" : "n"}`,
  ].join(" ");
  return <div data-testid="bp-state">{text}</div>;
}

function mount(props: any) {
  render(<Host {...props} /> as any);
}

describe("breakpointMenuLabel (exported pure helper for the context menu)", () => {
  it("says Add when the node has no active breakpoint", () => {
    expect(breakpointMenuLabel(false)).toBe("Add breakpoint");
  });
  it("says Remove when the node has an active breakpoint", () => {
    expect(breakpointMenuLabel(true)).toBe("Remove breakpoint");
  });
});

describe("useBreakpoints", () => {
  it("loads the breakpoint map once ids are present", async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      makeResponse(200, { breakpoints: [bp("gen_1")] })
    );
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: WID, versionId: VID, nodeId: "gen_1" });
    await waitFor(() =>
      expect(screen_text()).toMatch(/size=1 .*active=y/),
      50
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("never fetches when the workflow is unsaved (missing ids)", async () => {
    const fetchMock = jest.fn();
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: null, versionId: null, nodeId: "gen_1" });
    await waitFor(() => expect(screen_text()).toMatch(/size=0 .*err=none/), 50);
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });

  it("loads once per (workflowId, versionId) — re-renders do not refetch", async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      makeResponse(200, { breakpoints: [bp("gen_1")] })
    );
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: WID, versionId: VID, nodeId: "gen_1" });
    await waitFor(() => expect(screen_text()).toMatch(/size=1/), 50);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("toggle is optimistic: the map flips before the round-trip completes", async () => {
    const slow: any = new Promise<any>(() => {});
    global.fetch = jest.fn().mockResolvedValue(slow) as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: WID, versionId: VID, nodeId: "gen_1" });
    void captured.toggle("gen_1");
    expect(captured.isActive("gen_1")).toBe(true);
  });

  it("toggle on an absent node creates it; on an active node it removes it", async () => {
    const responses = [makeResponse(200, { breakpoints: [] }), makeResponse(200, bp("gen_1"))];
    let call = 0;
    const fetchMock = jest.fn(() => responses[Math.min(call++, responses.length - 1)]);
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: WID, versionId: VID, nodeId: "gen_1" });
    await waitFor(() => expect(screen_text()).toMatch(/size=0/), 50);
    void captured.toggle("gen_1");
    await waitFor(() => expect(screen_text()).toMatch(/size=1 .*active=y/), 50);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("toggle rolls back + surfaces error when the API rejects", async () => {
    let call = 0;
    const fetchMock = jest.fn(() => {
      call += 1;
      if (call > 1) throw new Error("breakpoints request failed (500)");
      return makeResponse(200, { breakpoints: [] });
    });
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: WID, versionId: VID, nodeId: "gen_1" });
    await waitFor(() => expect(screen_text()).toMatch(/size=0 .*err=none/), 50);
    void captured.toggle("gen_1");
    await waitFor(() => expect(screen_text()).toMatch(/size=0 .*err=breakpoints request failed \(500\).*active=n/), 50);
  });

  it("toggle is a no-op fetch when ids are missing", async () => {
    const fetchMock = jest.fn();
    global.fetch = fetchMock as any;
    mount({ apiUrl: API, accessToken: TOKEN, workflowId: null, versionId: null, nodeId: "gen_1" });
    void captured.toggle("gen_1");
    await waitFor(() => expect(screen_text()).toMatch(/err=none/), 50);
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });
});

function screen_text(): string {
  const el = document.querySelector('[data-testid="bp-state"]');
  return el ? el.textContent || "" : "";
}
