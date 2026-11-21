/**
 * sz-2.10c — breakpoints client tests.
 *
 * Every test mocks `global.fetch`.  Token and response body must never
 * appear in error messages.
 */

import {
  listBreakpoints,
  setBreakpoint,
  removeBreakpoint,
  toggleBreakpoint,
  breakpointMap,
  isBreakpointActive,
  Breakpoint,
} from "@/lib/breakpoints";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const API = "http://api.test/v1";
const TOKEN = "secret_tok_abc";
const WID = "wf-1";
const VID = "v-2";
const NODE = "gen_1";

const FIXTURE: Breakpoint = {
  id: 42,
  node_id: NODE,
  enabled: true,
  condition: null,
  hit_count: 3,
  last_hit_at: "2025-01-01T00:00:00Z",
  created_at: "2025-01-01T00:00:00Z",
  updated_at: "2025-01-01T00:00:00Z",
};

// ---------------------------------------------------------------------------
// listBreakpoints
// ---------------------------------------------------------------------------

describe("listBreakpoints", () => {
  beforeEach(() => {
    (global.fetch as any) = jest.fn();
  });

  it("maps rows from the API into a Breakpoint[]", async () => {
    const rows: Breakpoint[] = [FIXTURE];
    (global.fetch as any).mockResolvedValue(
      makeResponse(200, { workflow_id: 1, version_id: 2, breakpoints: rows })
    );

    const result = await listBreakpoints(API, TOKEN, WID, VID);

    expect(result).toEqual(rows);
  });

  it("returns [] on 404", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(404));

    const result = await listBreakpoints(API, TOKEN, WID, VID);

    expect(result).toEqual([]);
  });

  it("request URL carries a single /v1 (base already ends in /v1 — never /v1/v1)", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(200, { breakpoints: [] }));

    await listBreakpoints(API, TOKEN, WID, VID);

    expect(global.fetch).toHaveBeenCalledWith(
      `${API}/road/${encodeURIComponent(WID)}/versions/${encodeURIComponent(VID)}/breakpoints`,
      expect.anything()
    );
    const url = (global.fetch as any).mock.calls[0][0] as string;
    expect(url).toContain("/v1/road/");
    expect(url).not.toContain("/v1/v1/");
  });
});

// ---------------------------------------------------------------------------
// setBreakpoint
// ---------------------------------------------------------------------------

describe("setBreakpoint", () => {
  beforeEach(() => {
    (global.fetch as any) = jest.fn();
  });

  it("sends PUT with JSON body and Authorization header", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(200, FIXTURE));

    await setBreakpoint(API, TOKEN, WID, VID, NODE, {
      enabled: false,
      condition: { foo: "bar" },
    });

    expect(global.fetch).toHaveBeenCalledWith(
      `${API}/road/${encodeURIComponent(WID)}/versions/${encodeURIComponent(VID)}/breakpoints/${encodeURIComponent(NODE)}`,
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({ enabled: false, condition: { foo: "bar" } }),
      })
    );
  });

  it("encodes node id with spaces/slashes", async () => {
    const tricky = "a/b c";
    (global.fetch as any).mockResolvedValue(makeResponse(200, { ...FIXTURE, node_id: tricky, id: 99 }));

    await setBreakpoint(API, TOKEN, WID, VID, tricky, { enabled: true });

    expect(global.fetch).toHaveBeenCalledWith(
      `${API}/road/${encodeURIComponent(WID)}/versions/${encodeURIComponent(VID)}/breakpoints/${encodeURIComponent(tricky)}`,
      expect.any(Object)
    );
  });

  it("clears condition when null is passed", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(200, FIXTURE));

    await setBreakpoint(API, TOKEN, WID, VID, NODE, { condition: null });

    expect(global.fetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        body: JSON.stringify({ condition: null }),
      })
    );
  });
});

// ---------------------------------------------------------------------------
// removeBreakpoint
// ---------------------------------------------------------------------------

describe("removeBreakpoint", () => {
  beforeEach(() => {
    (global.fetch as any) = jest.fn();
  });

  it("resolves on 204", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(204));

    await expect(removeBreakpoint(API, TOKEN, WID, VID, NODE)).resolves.toBeUndefined();
  });

  it("resolves on 404 (idempotent)", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(404));

    await expect(removeBreakpoint(API, TOKEN, WID, VID, NODE)).resolves.toBeUndefined();
  });

  it("throws on 500", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(500));

    await expect(removeBreakpoint(API, TOKEN, WID, VID, NODE)).rejects.toThrow(
      "breakpoints request failed (500)"
    );
  });

  it("error message has no token or body", async () => {
    (global.fetch as any).mockResolvedValue(makeResponse(500, { detail: "oops" }));

    try {
      await removeBreakpoint(API, TOKEN, WID, VID, NODE);
      fail("should have thrown");
    } catch (e) {
      const msg = (e as Error).message;
      expect(msg).not.toContain(TOKEN);
      expect(msg).not.toContain("oops");
      expect(msg).toContain("500");
    }
  });
});

// ---------------------------------------------------------------------------
// toggleBreakpoint
// ---------------------------------------------------------------------------

describe("toggleBreakpoint", () => {
  beforeEach(() => {
    (global.fetch as any) = jest.fn();
  });

  it("adds when absent", async () => {
    const created: Breakpoint = { ...FIXTURE, node_id: "new_node", id: 99, enabled: true };
    (global.fetch as any).mockResolvedValue(makeResponse(200, created));

    const result = await toggleBreakpoint(new Map(), "new_node", API, TOKEN, WID, VID);

    expect(result.has("new_node")).toBe(true);
    expect(result.get("new_node")?.enabled).toBe(true);
  });

  it("removes (disables) when present", async () => {
    const existing: Breakpoint = { ...FIXTURE, enabled: true, node_id: "existing" };
    const updated: Breakpoint = { ...existing, enabled: false, updated_at: "2025-01-02" };
    (global.fetch as any).mockResolvedValue(makeResponse(200, updated));

    const input = new Map<string, Breakpoint>();
    input.set("existing", existing);

    const result = await toggleBreakpoint(input, "existing", API, TOKEN, WID, VID);

    expect(result.get("existing")?.enabled).toBe(false);
  });

  it("input map is never mutated", async () => {
    const existing: Breakpoint = { ...FIXTURE, enabled: true, node_id: "orig" };
    (global.fetch as any).mockResolvedValue(makeResponse(200, { ...existing, enabled: false, updated_at: "2025-01-02" }));

    const input = new Map<string, Breakpoint>();
    input.set("orig", existing);

    await toggleBreakpoint(input, "orig", API, TOKEN, WID, VID);

    // Input map must still have exactly one entry with original state.
    expect(input.size).toBe(1);
    expect(input.get("orig")?.enabled).toBe(true);
  });

  it("error on API throws and returns a map without the change", async () => {
    (global.fetch as any).mockRejectedValue(new Error("network"));

    const existing: Breakpoint = { ...FIXTURE, enabled: true, node_id: "x" };
    const input = new Map<string, Breakpoint>();
    input.set("x", existing);

    await expect(
      toggleBreakpoint(input, "x", API, TOKEN, WID, VID)
    ).rejects.toThrow("network");

    // Input is still untouched.
    expect(input.size).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// breakpointMap (pure)
// ---------------------------------------------------------------------------

describe("breakpointMap", () => {
  it("returns a Map keyed by node_id", () => {
    const list: Breakpoint[] = [
      { ...FIXTURE, node_id: "a", id: 1 },
      { ...FIXTURE, node_id: "b", id: 2 },
    ];
    const m = breakpointMap(list);
    expect(m.size).toBe(2);
    expect(m.get("a")?.id).toBe(1);
    expect(m.get("b")?.id).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// isBreakpointActive (pure)
// ---------------------------------------------------------------------------

describe("isBreakpointActive", () => {
  it("returns false for empty map", () => {
    expect(isBreakpointActive(new Map(), "x")).toBe(false);
  });

  it("returns false for disabled breakpoint", () => {
    const m = breakpointMap([{ ...FIXTURE, enabled: false }]);
    expect(isBreakpointActive(m, FIXTURE.node_id)).toBe(false);
  });

  it("returns true for enabled breakpoint", () => {
    const m = breakpointMap([FIXTURE]);
    expect(isBreakpointActive(m, FIXTURE.node_id)).toBe(true);
  });
});
