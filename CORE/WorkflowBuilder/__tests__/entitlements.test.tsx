/**
 * GT.D11b — entitlements hook + data layer.
 *
 *   - extractEntitlements / hasCapability / getLimit (pure)
 *   - useEntitlements: has/limit/edition/tierName, and FAIL-OPEN on fetch
 *     error and on the `{ edition, error: "unresolved" }` shape.
 */

import React from "react";
import { renderHook, waitFor } from "@testing-library/react";
import {
  _resetPlatformFeaturesCache,
} from "@/lib/platform-features";
import {
  extractEntitlements,
  hasCapability,
  getLimit,
} from "@/lib/entitlements";
import { useEntitlements } from "@/hooks/use-entitlements";

const mockFeatures = (features: Record<string, unknown>) => {
  global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(features) })
  ) as any;
};
const mockFetchFail = () => {
  global.fetch = jest.fn(() => Promise.reject(new Error("offline"))) as any;
};

const FULL_BLOCK = {
  deployment_mode: "onprem",
  entitlements: {
    edition: "pro",
    tier_name: "Scale",
    source: "plan",
    capabilities: ["stack_wizard"],
    limits: { nodes: 5, workers: "unlimited" },
  },
};

beforeEach(() => {
  _resetPlatformFeaturesCache();
  jest.clearAllMocks();
});

describe("extractEntitlements", () => {
  it("returns the block when present", () => {
    expect(extractEntitlements(FULL_BLOCK)?.edition).toBe("pro");
  });
  it("returns null for null / non-object / missing block", () => {
    expect(extractEntitlements(null)).toBeNull();
    expect(extractEntitlements({})).toBeNull();
    expect(extractEntitlements({ credits_enabled: true })).toBeNull();
  });
  it("treats the { edition, error: 'unresolved' } shape as unresolved", () => {
    expect(extractEntitlements({ edition: "ce", error: "unresolved" })).toBeNull();
  });
});

describe("hasCapability / getLimit (pure)", () => {
  const block = extractEntitlements(FULL_BLOCK)!;
  it("has: granted / absent / fail-open on null block", () => {
    expect(hasCapability(block, "stack_wizard")).toBe(true);
    expect(hasCapability(block, "marketplace_publish")).toBe(false);
    expect(hasCapability(null, "stack_wizard")).toBe(true); // fail open
  });
  it("getLimit: int, 'unlimited', unknown -> null, null block -> null", () => {
    expect(getLimit(block, "nodes")).toBe(5);
    expect(getLimit(block, "workers")).toBe("unlimited");
    expect(getLimit(block, "nope")).toBeNull();
    expect(getLimit(null, "nodes")).toBeNull();
  });
});

describe("useEntitlements", () => {
  it("resolves has/limit/edition/tierName from the block", async () => {
    mockFeatures(FULL_BLOCK);
    const { result } = renderHook(() => useEntitlements());
    await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 20000 });
    expect(result.current.resolved).toBe(true);
    expect(result.current.has("stack_wizard")).toBe(true);
    expect(result.current.has("marketplace_publish")).toBe(false);
    expect(result.current.limit("nodes")).toBe(5);
    expect(result.current.limit("workers")).toBe("unlimited");
    expect(result.current.limit("unknown")).toBeNull();
    expect(result.current.edition).toBe("pro");
    expect(result.current.tierName).toBe("Scale");
  });

  it("FAILS OPEN when /platform/features errors", async () => {
    mockFetchFail();
    const { result } = renderHook(() => useEntitlements());
    await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 20000 });
    expect(result.current.resolved).toBe(false);
    expect(result.current.has("stack_wizard")).toBe(true);
    expect(result.current.has("marketplace_publish")).toBe(true);
    expect(result.current.limit("nodes")).toBeNull();
    expect(result.current.edition).toBeNull();
  });

  it("FAILS OPEN on the { edition, error: 'unresolved' } shape", async () => {
    mockFeatures({ edition: "ce", error: "unresolved" });
    const { result } = renderHook(() => useEntitlements());
    await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 20000 });
    expect(result.current.resolved).toBe(false);
    expect(result.current.has("anything")).toBe(true);
  });

  it("FAILS OPEN when the block is missing entirely", async () => {
    mockFeatures({ credits_enabled: true });
    const { result } = renderHook(() => useEntitlements());
    await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 20000 });
    expect(result.current.has("stack_wizard")).toBe(true);
  });
});
