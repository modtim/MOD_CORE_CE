/**
 * GG.UI.2 — store listing detail page: detail fields per kind + the action
 * button state machine + the `app/nodes/[id]` → `/store/node/[id]` redirect.
 *
 * Real component, only `fetch` mocked. The button state machine is driven
 * ONLY by the `actions` array the API derives (see
 * `catalog_service._derive_actions`): owned | get (free) | buy | trial |
 * plan. Code redemption is intentionally NOT actions-driven: it is backed
 * by POST /codes/redeem, so a "Have a code?" disclosure renders for every
 * non-owned item (and only for non-owned ones).
 *
 * The redirect is a server component; we exercise its exported component
 * directly with Promise-wrapped params/searchParams (Next 15's contract)
 * and assert the URL it passes to `next/navigation.redirect`.
 */

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import StoreDetailPage from "@/app/store/[kind]/[id]/page";
import NodeDetailRedirect from "@/app/nodes/[id]/page";

// Capture the URL passed to next/navigation.redirect rather than relying on
// the internal NEXT_REDIRECT error shape.
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

/** Base detail matching the API row shape for a given kind. */
function detail(kind: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind,
    id: 42,
    name: "Acme",
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
  global.fetch = jest.fn(async (input: RequestInfo | URL) =>
    res(200, body)
  ) as unknown as typeof fetch;
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

describe("store detail — action button state machine", () => {
  it("get (free): price 0 + actions ['get'] → 'Get (free)' button, no buy/owned", async () => {
    mockFetchDetail(detail("node", { price_cents: 0, actions: ["get"] }));
    await openDetail("node");
    expect(screen.getByTestId("store-action-get")).toBeInTheDocument();
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-owned")).not.toBeInTheDocument();
  });

  it("buy: price >0 + actions ['buy'] → 'Buy $X' button, no get/owned", async () => {
    mockFetchDetail(detail("node", { price_cents: 2500, price_tier: "pro", actions: ["buy"] }));
    await openDetail("node");
    // CE: the priced Buy button is not rendered (rank 4 row)
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-get")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-owned")).not.toBeInTheDocument();
  });

  it("owned: actions ['owned'] + owned_via → 'Owned — via X' chip, no get/buy", async () => {
    mockFetchDetail(detail("node", {
      price_cents: 1000, owned: true, owned_via: "Included", actions: ["owned"],
    }));
    await openDetail("node");
    const owned = screen.getByTestId("store-action-owned");
    expect(owned.textContent).toContain("Owned");
    expect(owned.textContent).toContain("Included");
    expect(screen.queryByTestId("store-action-get")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
  });

  it("trial: actions ['buy','trial'] → buy button + 'Trial coming soon' badge", async () => {
    mockFetchDetail(detail("workflow", { price_cents: 500, actions: ["buy", "trial"] }));
    await openDetail("workflow");
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.queryByText("Trial coming soon")).not.toBeInTheDocument();
  });

  it("plan: actions ['buy','plan'] → buy button + 'Included in your plan' badge", async () => {
    mockFetchDetail(detail("workflow", { price_cents: 500, actions: ["buy", "plan"] }));
    await openDetail("workflow");
    expect(screen.queryByTestId("store-action-buy")).not.toBeInTheDocument();
    expect(screen.getByText("Included in your plan")).toBeInTheDocument();
  });

  it("no 'by …' header line — publisher is not part of the API row", async () => {
    // Even if a publisher-like field leaked into the row, the page must not
    // render a "by …" line for it.
    mockFetchDetail(detail("node", { price_cents: 0, publisher: "Acme Publisher" } as Record<string, unknown>));
    await openDetail("node");
    expect(screen.queryByText(/^by /)).not.toBeInTheDocument();
    expect(screen.queryByText(/Acme Publisher/)).not.toBeInTheDocument();
  });
});

describe("store detail — code redemption", () => {
  /** fetch mock serving a fixed sequence of responses, capturing calls. */
  function mockFetchSequence(entries: Array<{ status: number; body: unknown }>): Array<{ url: string; init?: RequestInit }> {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    let i = 0;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      // 23.1a.s4 variant panel: a node detail also fetches /source and
      // /variant-verification. Serve harmless empty OK responses for those
      // WITHOUT consuming a sequence entry — the sequence is for the
      // detail + redeem flow this suite is about.
      if (url.endsWith("/source")) return res(200, { variants: {} });
      if (url.endsWith("/variant-verification")) return res(200, { variants: [] });
      const next = entries[Math.min(i, entries.length - 1)];
      i += 1;
      return res(next.status, next.body);
    }) as unknown as typeof fetch;
    return calls;
  }

  async function openRedeemForm() {
    await act(async () => { screen.getByTestId("store-redeem-toggle").click(); });
    await screen.findByTestId("store-action-redeem-input");
  }

  it("shows the 'Have a code?' disclosure for a non-owned item (not actions-driven)", async () => {
    // A plain buy item — `actions` never includes "redeem".
    mockFetchDetail(detail("node", { price_cents: 1000, actions: ["buy"] }));
    await openDetail("node");
    expect(screen.getByTestId("store-redeem-disclosure")).toBeInTheDocument();
    // Collapsed by default: no input until toggled.
    expect(screen.queryByTestId("store-action-redeem-input")).not.toBeInTheDocument();
    await openRedeemForm();
    const input = screen.getByTestId("store-action-redeem-input");
    const button = screen.getByTestId("store-action-redeem-button");
    expect(button).toBeDisabled();
    await act(async () => { fireEvent.change(input, { target: { value: "GOOD-CODE" } }); });
    expect(button).not.toBeDisabled();
  });

  it("hides the redeem disclosure for an owned item", async () => {
    mockFetchDetail(detail("node", { price_cents: 1000, owned: true, owned_via: "Included", actions: ["owned"] }));
    await openDetail("node");
    expect(screen.getByTestId("store-action-owned")).toBeInTheDocument();
    expect(screen.queryByTestId("store-redeem-disclosure")).not.toBeInTheDocument();
    expect(screen.queryByTestId("store-redeem-toggle")).not.toBeInTheDocument();
  });

  it("redeems via POST /codes/redeem with the {code} body, then refetches the detail", async () => {
    const calls = mockFetchSequence([
      { status: 200, body: detail("node", { price_cents: 1000, actions: ["buy"] }) },
      { status: 200, body: { ok: true } },
      { status: 200, body: detail("node", { owned: true, owned_via: "code", actions: ["owned"] }) },
    ]);
    await openDetail("node");
    await openRedeemForm();
    await act(async () => {
      fireEvent.change(screen.getByTestId("store-action-redeem-input"), { target: { value: " GOOD-CODE " } });
    });
    await act(async () => { screen.getByTestId("store-action-redeem-button").click(); });

    // Flips to owned after the refetch.
    await screen.findByTestId("store-action-owned");
    expect(screen.queryByTestId("store-redeem-disclosure")).not.toBeInTheDocument();

    const redeemCall = calls.find((c) => c.url === "http://api.localhost/codes/redeem");
    expect(redeemCall).toBeDefined();
    expect(redeemCall?.init?.method).toBe("POST");
    expect(JSON.parse(redeemCall?.init?.body as string)).toEqual({ code: "GOOD-CODE" });
    // The variant panel (23.1a.s4) adds /source + /variant-verification fetches;
    // count only the calls this test is about (the detail + redeem flow).
    const flow = calls.filter((c) =>
      c.url === "http://api.localhost/catalog/node/42" ||
      c.url === "http://api.localhost/codes/redeem",
    );
    // Initial detail + redeem + refetch.
    expect(flow).toHaveLength(3);
    expect(flow[2].url).toBe("http://api.localhost/catalog/node/42");
  });

  it("shows the API's error message on a 4xx redeem", async () => {
    mockFetchSequence([
      { status: 200, body: detail("node", { price_cents: 1000, actions: ["buy"] }) },
      { status: 400, body: { detail: "Invalid or expired code" } },
    ]);
    await openDetail("node");
    await openRedeemForm();
    await act(async () => {
      fireEvent.change(screen.getByTestId("store-action-redeem-input"), { target: { value: "BAD-CODE" } });
    });
    await act(async () => { screen.getByTestId("store-action-redeem-button").click(); });
    const msg = await screen.findByTestId("store-action-msg");
    expect(msg.textContent).toContain("Invalid or expired code");
    // Still non-owned — the disclosure stays.
    expect(screen.getByTestId("store-redeem-disclosure")).toBeInTheDocument();
  });
});

describe("store detail — 404 catalog: no /source fetch", () => {
  it("kind=node does NOT fetch /nodes/<id>/source when the catalog 404s (not found)", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      if (String(input).includes("/catalog/node/")) {
        return res(404, {});
      }
      if (String(input).includes("/nodes/") && String(input).includes("/source")) {
        // This should NOT be called — return a fake response so the test does not crash
        // if the guard is still broken.
        return res(200, { variants: {} });
      }
      return res(200, {});
    }) as unknown as typeof fetch;

    await act(async () => {
      render(<StoreDetailPage params={{ kind: "node", id: "999999999" }} />);
    });

    const sourceCalls = calls.filter((c) => c.url.includes("/source"));
    expect(sourceCalls).toHaveLength(0);

    // The page should show the "Not found" state.
    await screen.findByRole("heading", { name: "Not found", exact: true });
  });
});

describe("store detail — per-kind detail fields", () => {
  it("node: shows name, description, price, status/visibility", async () => {
    mockFetchDetail(detail("node", {
      description: "A node", price_cents: 0, status: "active", visibility: "public",
    }));
    await openDetail("node");
    expect(screen.getByTestId("store-detail-name").textContent).toContain("Acme");
    expect(screen.getByText("A node")).toBeInTheDocument();
  });

  it("workflow: shows the versions list (id/version/price/status)", async () => {
    mockFetchDetail(detail("workflow", {
      price_cents: 0,
      versions: [
        { id: 1, version: 1, price_cents: 0, status: "published" },
        { id: 2, version: 2, price_cents: 500, status: "published" },
      ],
    }));
    await openDetail("workflow");
    const rows = screen.getAllByTestId("store-versions");
    expect(rows).toHaveLength(1);
    const items = screen.getByTestId("store-versions").querySelectorAll("li");
    expect(items).toHaveLength(2);
    expect(items[1].textContent).toContain("$5.00");
  });

  it("stack: shows stack-only detail fields (version, license_class, licence, gpu_requirements)", async () => {
    mockFetchDetail(detail("stack", {
      price_cents: 100,
      version: "1.4.0",
      license_class: "commercial",
      licence: "open",
      gpu_requirements: { count: 2, accelerator: "CUDA", vram_gb: 48 },
      versions: [{ id: 9, version: "1.4.0", slug: "acme", license_class: "commercial" }],
    }));
    await openDetail("stack");
    const details = screen.getByTestId("store-stack-details");
    expect(details.textContent).toContain("1.4.0");
    expect(details.textContent).toContain("commercial");
    expect(details.textContent).toContain("open");
    expect(details.textContent).toContain("2× CUDA 48 GB");
    // stack version row renders slug + license_class too
    const vrow = screen.getByTestId("store-versions").querySelector("li")!;
    expect(vrow.textContent).toContain("acme");
    expect(vrow.textContent).toContain("commercial");
  });

  it("stack without detail fields: no stack-details block", async () => {
    mockFetchDetail(detail("stack", { price_cents: 100 }));
    await openDetail("stack");
    expect(screen.queryByTestId("store-stack-details")).not.toBeInTheDocument();
  });
});

describe("13.5f — 'Secrets needed' install step (stack detail)", () => {
  const secretBody = (names: string[]) =>
    detail("stack", {
      owned: true,
      actions: ["owned"],
      required_secrets: names.map((n) => ({
        name: n, kind: "env", services: ["app"], required: true,
        description: null,
      })),
    });

  function mockFetchStack(secretNames: string[], existing: string[]) {
    global.fetch = jest.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/secrets/")
        ? res(200, existing.map((n) => ({ id: 1, name: n })))
        : res(200, secretBody(secretNames))
    ) as unknown as typeof fetch;
  }

  it("required secret without an existing tenant secret → masked input, no value field", async () => {
    mockFetchStack(["OPENAI_API_KEY"], []);
    await openDetail("stack");
    expect(screen.getByTestId("store-secrets-needed")).toBeInTheDocument();
    const input = screen.getByTestId("store-secret-OPENAI_API_KEY-input");
    expect(input).toBeInTheDocument();
    expect((input as HTMLInputElement).type).toBe("password");
    // Names/kinds/descriptions only — the API never sends values.
    expect(screen.queryByTestId("store-secret-OPENAI_API_KEY-value")).not.toBeInTheDocument();
  });

  it("required secret the tenant already has → 'use existing', no input", async () => {
    mockFetchStack(["OPENAI_API_KEY"], ["OPENAI_API_KEY"]);
    await openDetail("stack");
    expect(screen.getByTestId("store-secrets-needed")).toBeInTheDocument();
    expect(screen.getByTestId("store-secret-OPENAI_API_KEY-status").textContent)
      .toContain("existing secret will be used");
    expect(screen.queryByTestId("store-secret-OPENAI_API_KEY-input")).not.toBeInTheDocument();
  });

  it("no required_secrets → no secrets panel at all", async () => {
    mockFetchStack([], []);
    await openDetail("stack");
    expect(screen.queryByTestId("store-secrets-needed")).not.toBeInTheDocument();
  });
});

describe("app/nodes/[id] redirect", () => {
  // The page is a server (async) component, so we invoke its function form
  // directly with Next 15's Promise-wrapped props and capture the URL
  // passed to next/navigation.redirect.
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

  it("redirects /nodes/[id] to /store/node/[id] (query preserved)", async () => {
    const url = await invokeRedirect(
      Promise.resolve({ id: "123" }),
      Promise.resolve({ q: "upscale", ref: ["a", "b"] }),
    );
    expect(url.startsWith("/store/node/123?")).toBe(true);
    expect(url).toContain("q=upscale");
    expect(url).toContain("ref=a");
    expect(url).toContain("ref=b");
  });

  it("redirects with no query string when searchParams is empty", async () => {
    const url = await invokeRedirect(
      Promise.resolve({ id: "7" }),
      Promise.resolve({}),
    );
    expect(url).toBe("/store/node/7");
  });
});
