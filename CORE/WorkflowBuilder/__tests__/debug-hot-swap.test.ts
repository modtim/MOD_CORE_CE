/**
 * sz-2.10 — hot-swap client tests.
 *
 * Every test mocks `global.fetch`. The token and the response body must
 * never appear in an error/detail string.
 */

import {
  debugHotSwapUrl,
  hotSwapDebugNode,
  stripMaskedKeys,
  isMaskedValue,
} from "@/lib/debug-frame";

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

const API = "http://api.test";
const TOKEN = "secret_tok_abc";
const RUN = 4242;
const NODE = "gen_1";

// ---------------------------------------------------------------------------
// debugHotSwapUrl
// ---------------------------------------------------------------------------

describe("debugHotSwapUrl", () => {
  it("builds the hot-swap path with both ids", () => {
    expect(debugHotSwapUrl(API, RUN, NODE)).toBe(
      `${API}/road/workflow-runs/${RUN}/debug/hot-swap/${NODE}`
    );
  });

  it("percent-encodes ids that carry path characters", () => {
    const nodeId = "a b/c?d";
    const url = debugHotSwapUrl(API, RUN, nodeId);
    expect(url).toBe(
      `${API}/road/workflow-runs/${RUN}/debug/hot-swap/a%20b%2Fc%3Fd`
    );
    expect(url).not.toContain(" ");
    expect(url.split("/debug/hot-swap/")[1]).not.toContain("/");
  });
});

// ---------------------------------------------------------------------------
// stripMaskedKeys
// ---------------------------------------------------------------------------

describe("stripMaskedKeys", () => {
  it("drops top-level masked values and keeps the rest", () => {
    const src = { prompt: "hello", api_key: "***", seed: 7 };
    expect(stripMaskedKeys(src)).toEqual({ prompt: "hello", seed: 7 });
  });

  it("recurses into nested objects and arrays", () => {
    const src = {
      outer: { keep: 1, hidden: "***" },
      list: ["***", "visible", { gone: "***", ok: true }],
    };
    expect(stripMaskedKeys(src)).toEqual({
      outer: { keep: 1 },
      list: ["visible", { ok: true }],
    });
  });

  it("leaves an object with no masked values untouched (new copy)", () => {
    const src = { a: 1, b: { c: "x" } };
    const out = stripMaskedKeys(src);
    expect(out).toEqual(src);
    expect(out).not.toBe(src);
  });

  it("returns undefined for a wholly-masked value", () => {
    expect(stripMaskedKeys("***")).toBeUndefined();
  });

  it("does not treat look-alikes as masked", () => {
    expect(isMaskedValue("****")).toBe(false);
    expect(stripMaskedKeys({ k: "****" })).toEqual({ k: "****" });
  });
});

// ---------------------------------------------------------------------------
// hotSwapDebugNode
// ---------------------------------------------------------------------------

describe("hotSwapDebugNode", () => {
  beforeEach(() => {
    (global.fetch as any) = jest.fn();
  });

  it("POSTs the body with auth + json headers and returns the override", async () => {
    (global.fetch as any).mockResolvedValue(
      makeResponse(200, { run_id: RUN, node_id: NODE, override: { config: { seed: 7 } } })
    );

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, { config: { seed: 7 } });

    expect(result).toEqual({ ok: true, override: { config: { seed: 7 } } });
    expect(global.fetch).toHaveBeenCalledWith(
      debugHotSwapUrl(API, RUN, NODE),
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({ config: { seed: 7 } }),
      })
    );
  });

  it("409 resolves to the API's curated refusal detail", async () => {
    (global.fetch as any).mockResolvedValue(
      makeResponse(409, { detail: "node is not paused" })
    );

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, { config: {} });

    expect(result).toEqual({
      ok: false,
      status: 409,
      detail: "node is not paused",
    });
  });

  it("422 resolves with its detail, never throwing", async () => {
    (global.fetch as any).mockResolvedValue(
      makeResponse(422, { detail: "nothing to swap" })
    );

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, {});

    expect(result).toEqual({
      ok: false,
      status: 422,
      detail: "nothing to swap",
    });
  });

  it("a non-JSON error body falls back to a short safe message", async () => {
    (global.fetch as any).mockResolvedValue(
      {
        ok: false,
        status: 500,
        headers: { get: () => null },
        json: async () => {
          throw new Error("not json");
        },
      } as unknown as Response
    );

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, { config: {} });

    expect(result).toEqual({
      ok: false,
      status: 500,
      detail: "hot-swap refused (HTTP 500)",
    });
  });

  it("a transport failure reports status 0 and does not throw", async () => {
    (global.fetch as any).mockRejectedValue(new Error("network"));

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, { config: {} });

    expect(result).toEqual({
      ok: false,
      status: 0,
      detail: "hot-swap request failed",
    });
  });

  it("a 200 with no override key resolves to an empty override", async () => {
    (global.fetch as any).mockResolvedValue(
      makeResponse(200, { run_id: RUN, node_id: NODE })
    );

    const result = await hotSwapDebugNode(API, TOKEN, RUN, NODE, {});

    expect(result).toEqual({ ok: true, override: {} });
  });
});
