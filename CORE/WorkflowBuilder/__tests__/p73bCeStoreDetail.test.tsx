/**
 * 2400LR-p73 p73b3 — CE store-detail carve (`app/store/[kind]/[id]`).
 *
 * CE ships no /catalog router (CE main.py `_CATALOG_ROUTERS = []`), so the
 * detail payload never exists there; the page must NOT fetch it at all and
 * must render a degraded, name-bearing state instead of the 404 -> notFound
 * branch (which hides `store-detail-name`).
 *
 * Asserted (real component, only `fetch` mocked):
 *   (a) positive edition "ce" -> ZERO fetches whose URL contains "/catalog",
 *       and `store-detail-name` renders, named from the {kind,id} path;
 *   (b) the entitlements FAILURE shape `{ edition: "ce", error: "unresolved" }`
 *       (extractEntitlements -> null) is NOT a positive edition: the /catalog
 *       fetch still happens and the page falls back to notFound (fail-open).
 */

import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import StoreDetailPage from "@/app/store/[kind]/[id]/page";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";

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

/** Every URL the page actually requested, in order. */
let requested: string[] = [];

function mockFetch(features: unknown, catalogStatus: number) {
  requested = [];
  global.fetch = jest.fn((url: unknown) => {
    const u = String(url);
    requested.push(u);
    if (u.endsWith("/platform/features")) {
      return Promise.resolve(res(200, features));
    }
    return Promise.resolve(res(catalogStatus, { detail: "Not Found" }));
  }) as unknown as typeof fetch;
}

async function openDetail() {
  await act(async () => {
    render(<StoreDetailPage params={{ kind: "node", id: "42" }} />);
  });
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  // The /platform/features cache is module-level (30s TTL) — clear it so each
  // case gets its own entitlements payload.
  _resetPlatformFeaturesCache();
});

describe("CE store detail (2400LR-p73 p73b3)", () => {
  it("edition 'ce' -> no /catalog fetch at all, store-detail-name renders", async () => {
    mockFetch({ entitlements: { edition: "ce", capabilities: [] } }, 404);
    await openDetail();

    const name = await screen.findByTestId("store-detail-name");
    const text = name.textContent ?? "";
    expect(text).toContain("node");
    expect(text).toContain("42");

    const catalogCalls = requested.filter((u) => u.includes("/catalog"));
    expect(catalogCalls).toEqual([]);
    // The upgrade CTA chip is present (PaidLock.tsx LockedBadge).
    expect(screen.queryByText(/Not part of this edition/)).toBeTruthy();
  });

  it("unresolved entitlements ({edition:'ce',error:'unresolved'}) -> fetch still happens", async () => {
    mockFetch({ entitlements: { edition: "ce", error: "unresolved" } }, 404);
    await openDetail();

    await waitFor(() => {
      expect(requested.some((u) => u.includes("/catalog"))).toBe(true);
    });
    const catalogCalls = requested.filter((u) => u.includes("/catalog"));
    expect(catalogCalls).toEqual(["http://api.localhost/catalog/node/42"]);
    // Fail-open: today's 404 -> notFound branch, which has no name.
    expect(screen.queryByTestId("store-detail-name")).toBeNull();
  });
});
