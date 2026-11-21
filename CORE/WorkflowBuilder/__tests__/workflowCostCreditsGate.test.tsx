/**
 * Credit-display consistency: the WorkflowBuilder cost pill must resolve
 * from the SAME /platform/features.credits_enabled signal as the
 * Dashboard header (CORE/Dashboard/js/ui.js renderHeaderCredits), so the
 * two surfaces cannot disagree ("Unlimited" on the Dashboard vs a raw
 * "2100 credits" in the builder for the same on-prem user).
 *
 *   - credits_enabled !== true (on-prem / credits off) -> "Unlimited"
 *   - credits_enabled === true (SaaS)                  -> numeric cost
 */

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import {
  getPlatformFeatures,
  areCreditsEnabled,
  _resetPlatformFeaturesCache,
} from "@/lib/platform-features";

// Mock the authenticated cost fetch so the SaaS numeric branch is
// deterministic and does not require a real token/network.
jest.mock("@/lib/api-client", () => ({
  authenticatedFetch: jest.fn(),
}));
import { authenticatedFetch } from "@/lib/api-client";
import { WorkflowCostDisplay } from "@/components/WorkflowCostDisplay";

const mockFeatures = (features: Record<string, unknown>) => {
  global.fetch = jest.fn(() =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve(features),
    })
  ) as any;
};

const mockPredictCost = (payload: Record<string, unknown>) => {
  (authenticatedFetch as jest.Mock).mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "OK",
    json: () => Promise.resolve(payload),
  });
};

const NODES = [{ id: "n1", data: { node_kind: "process" } }];
const EDGES: any[] = [];

beforeEach(() => {
  _resetPlatformFeaturesCache();
  jest.clearAllMocks();
});

describe("platform-features shared signal", () => {
  it("areCreditsEnabled is true ONLY when credits_enabled === true", () => {
    expect(areCreditsEnabled({ credits_enabled: true })).toBe(true);
    expect(areCreditsEnabled({ credits_enabled: false })).toBe(false);
    expect(areCreditsEnabled({})).toBe(false);
    expect(areCreditsEnabled(null)).toBe(false);
    expect(areCreditsEnabled(undefined)).toBe(false);
    // Guard against truthy-but-not-true drift.
    expect(areCreditsEnabled({ credits_enabled: 1 as unknown as boolean })).toBe(false);
  });

  it("fetches /platform/features and coalesces/caches the result", async () => {
    mockFeatures({ deployment_mode: "onprem", credits_enabled: false });
    const [a, b] = await Promise.all([getPlatformFeatures(), getPlatformFeatures()]);
    expect(a).toEqual(b);
    expect(a.credits_enabled).toBe(false);
    // Coalesced + cached: a single network call for two concurrent asks.
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect((global.fetch as jest.Mock).mock.calls[0][0]).toContain("/platform/features");
  });
});

describe("WorkflowCostDisplay credit gating", () => {
  it('on-prem (credits_enabled=false) shows "Unlimited", never a raw number', async () => {
    mockFeatures({ deployment_mode: "onprem", credits_enabled: false });
    mockPredictCost({ total_credits: 2100, user_balance: 2100, billing_mode: "prepaid", warnings: [] });

    render(<WorkflowCostDisplay nodes={NODES} edges={EDGES} accessToken="tok" />);

    expect(await screen.findByText("Unlimited")).toBeInTheDocument();
    // The misleading balance/number must NOT be shown, and predict-cost
    // must not even be called when credits aren't metered.
    expect(screen.queryByText(/2,?100/)).not.toBeInTheDocument();
    expect(screen.queryByText(/credits/)).not.toBeInTheDocument();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("SaaS (credits_enabled=true) preserves the numeric cost path", async () => {
    mockFeatures({ deployment_mode: "saas", credits_enabled: true });
    mockPredictCost({
      total_credits: 42,
      total_usd: null,
      billing_mode: "prepaid",
      user_balance: 2100,
      deficit: 0,
      sufficient_credits: true,
      warnings: [],
    });

    render(<WorkflowCostDisplay nodes={NODES} edges={EDGES} accessToken="tok" />);

    expect(await screen.findByText("42 credits")).toBeInTheDocument();
    expect(screen.queryByText("Unlimited")).not.toBeInTheDocument();
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
  });
});
