/**
 * GG.UI.2 — store listing detail page (`app/store/[kind]/[id]`): one test
 * per action-button state (buy / get / trial / redeem / plan / owned) plus
 * the `app/nodes/[id]` → `/store/node/[id]` redirect.
 *
 * Real component, only `fetch` mocked (no api-client wrapper exists for this
 * page — it talks to `GET/POST /catalog/{kind}/{id}` and `POST /codes/redeem`
 * directly, matching the store dialog's own fetch conventions). The
 * button state machine is driven ONLY by the `actions` array the API derives
 * server-side (`catalog_service._derive_actions`); this suite never
 * reimplements that logic — it only asserts the page renders the right
 * control for each `actions` value.
 *
 * See also `storeDetail.test.tsx` (broader per-kind field coverage) and
 * `storeCheckout.test.tsx` (the buy → quote → Stripe/credits flow itself).
 * This file is the narrow "one case per button state" contract requested by
 * GG.UI.2's task spec.
 */

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import StoreDetailPage from "@/app/store/[kind]/[id]/page";
import NodeDetailRedirect from "@/app/nodes/[id]/page";

jest.mock("next/navigation", () => ({
  redirect: (url: string) => {
    throw { __redirect: url };
  },
}));

function res(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function detail(kind: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind,
    id: 42,
    name: "Acme Listing",
    description: "A test listing",
    price_cents: 0,
    price_tier: null,
    owned: false,
    owned_via: null,
    versions: null,
    closure: null,
    actions: ["get"],
    ...overrides,
  };
}

function mockFetchDetail(body: Record<string, unknown>) {
  global.fetch = jest.fn(async () => res(200, body)) as unknown as typeof fetch;
}

function mockFetchSequence(entries: Array<{ status: number; body: unknown }>): Array<{ url: string; init?: RequestInit }> {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let i = 0;
  global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    const next = entries[Math.min(i, entries.length - 1)];
    i += 1;
    return res(next.status, next.body);
  }) as unknown as typeof fetch;
  return calls;
}

async function openDetail(kind: string, id = "42") {
  await act(async () => {
    render(<StoreDetailPage params={{ kind, id }} />);
  });
  await screen.findByTestId("store-detail-name");
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  sessionStorage.setItem("workflow_access_token", "tok");
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("store listing detail — action button state machine (one case per state)", () => {
  it("get: actions ['get'] renders the free-claim button only", async () => {
    mockFetchDetail(detail("node", { price_cents: 0, actions: ["get"] }));
    await openDetail("node");
    expect(screen.getByTestId("store-action-get")).toHaveTextContent("Get (free)");
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-owned")).not.toBeInTheDocument();
  });

  it("buy: actions ['buy'] renders the priced Buy button only", async () => {
    mockFetchDetail(detail("node", { price_cents: 1500, actions: ["buy"] }));
    await openDetail("node");
    // CE: the priced Buy button is not rendered (rank 4 row)
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-get")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-owned")).not.toBeInTheDocument();
  });

  it("trial: actions ['buy','trial'] renders the buy button plus a trial badge", async () => {
    mockFetchDetail(detail("workflow", { price_cents: 500, actions: ["buy", "trial"] }));
    await openDetail("workflow");
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByText("Trial coming soon")).not.toBeInTheDocument();
  });

  it("plan: actions ['buy','plan'] renders the buy button plus an included-in-plan badge", async () => {
    mockFetchDetail(detail("workflow", { price_cents: 500, actions: ["buy", "plan"] }));
    await openDetail("workflow");
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.getByText("Included in your plan")).toBeInTheDocument();
  });

  it("owned: actions ['owned'] renders the owned chip only, hides get/buy and the redeem disclosure", async () => {
    mockFetchDetail(detail("node", {
      price_cents: 1000, owned: true, owned_via: "Included", actions: ["owned"],
    }));
    await openDetail("node");
    const owned = screen.getByTestId("store-action-owned");
    expect(owned).toHaveTextContent("Owned");
    expect(owned).toHaveTextContent("Included");
    expect(screen.queryByTestId("store-action-get")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-redeem-disclosure")).not.toBeInTheDocument();
  });

  it("redeem: not actions-driven — the 'Have a code?' disclosure claims via POST /codes/redeem and flips to owned", async () => {
    const calls = mockFetchSequence([
      { status: 200, body: detail("node", { price_cents: 1000, actions: ["buy"] }) },
      { status: 200, body: { ok: true } },
      { status: 200, body: detail("node", { owned: true, owned_via: "code", actions: ["owned"] }) },
    ]);
    await openDetail("node");
    expect(screen.getByTestId("store-redeem-disclosure")).toBeInTheDocument();

    await act(async () => { screen.getByTestId("store-redeem-toggle").click(); });
    const input = await screen.findByTestId("store-action-redeem-input");
    await act(async () => { fireEvent.change(input, { target: { value: " GOOD-CODE " } }); });
    await act(async () => { screen.getByTestId("store-action-redeem-button").click(); });

    await screen.findByTestId("store-action-owned");

    const redeemCall = calls.find((c) => c.url === "http://api.localhost/codes/redeem");
    expect(redeemCall?.init?.method).toBe("POST");
    expect(JSON.parse(redeemCall?.init?.body as string)).toEqual({ code: "GOOD-CODE" });
  });
});

describe("app/nodes/[id] → /store/node/[id] redirect", () => {
  async function invokeRedirect(params: unknown, searchParams: unknown): Promise<string> {
    try {
      await (NodeDetailRedirect as unknown as (props: {
        params: unknown;
        searchParams: unknown;
      }) => Promise<void>)({ params, searchParams });
    } catch (e) {
      if (e && typeof e === "object" && "__redirect" in e) {
        return (e as { __redirect: string }).__redirect;
      }
      throw e;
    }
    throw new Error("redirect() was not called");
  }

  it("redirects /nodes/{id} to /store/node/{id}, preserving query params", async () => {
    const url = await invokeRedirect(
      Promise.resolve({ id: "123" }),
      Promise.resolve({ q: "upscale", ref: ["a", "b"] }),
    );
    expect(url.startsWith("/store/node/123?")).toBe(true);
    expect(url).toContain("q=upscale");
    expect(url).toContain("ref=a");
    expect(url).toContain("ref=b");
  });

  it("redirects /nodes/{id} to /store/node/{id} with no query string when none is present", async () => {
    const url = await invokeRedirect(
      Promise.resolve({ id: "7" }),
      Promise.resolve({}),
    );
    expect(url).toBe("/store/node/7");
  });
});
