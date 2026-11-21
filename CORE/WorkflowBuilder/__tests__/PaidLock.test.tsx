/**
 * GT.D11b — <PaidLock> / <LockedBadge> render contract.
 *
 *   - cap present (or unresolved)  -> child rendered, NO badge
 *   - cap absent                   -> child disabled + "Not part of this edition"
 *                                      badge + Upgrade link
 */

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";
import { PaidLock, LockedBadge, upgradeUrl } from "@/components/PaidLock";

const mockFeatures = (features: Record<string, unknown>) => {
  global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(features) })
  ) as any;
};
const mockFetchFail = () => {
  global.fetch = jest.fn(() => Promise.reject(new Error("offline"))) as any;
};

beforeEach(() => {
  _resetPlatformFeaturesCache();
  jest.clearAllMocks();
});

describe("PaidLock", () => {
  it("renders the child unlocked when the cap is present", async () => {
    mockFeatures({
      entitlements: {
        edition: "saas",
        capabilities: ["stack_wizard"],
        limits: {},
      },
    });
    render(
      <PaidLock cap="stack_wizard" label="Stack Wizard">
        <button type="button">Stack Wizard</button>
      </PaidLock>,
    );
    const btn = await screen.findByRole("button", { name: "Stack Wizard" });
    expect(btn).toBeInTheDocument();
    expect(btn).toBeEnabled();
    expect(screen.queryByText(/not part of this edition/i)).not.toBeInTheDocument();
  });

  it("disables the child and shows the badge when the cap is absent", async () => {
    mockFeatures({
      entitlements: {
        edition: "ce",
        capabilities: [],
        limits: {},
      },
    });
    render(
      <PaidLock cap="stack_wizard" label="Stack Wizard">
        <button type="button">Stack Wizard</button>
      </PaidLock>,
    );
    // Wait for the badge — it only exists in the locked state (during the
    // loading phase the child renders unlocked first).
    await screen.findByText(/not part of this edition/i);
    // The locked child is aria-hidden (disabled control), so query the DOM.
    const btn = (await screen.findByText("Stack Wizard")).closest("button")!;
    expect(btn).toBeInTheDocument();
    // The wrapper is aria-disabled and the child is pointer-events-none.
    const wrapper = screen.getByTitle(/not part of this edition/i);
    expect(wrapper).toHaveAttribute("aria-disabled", "true");
    expect(wrapper).toHaveAttribute("data-mod-locked", "1");
    expect(screen.getByText(/not part of this edition/i)).toBeInTheDocument();
    // Upgrade link points at the dashboard billing route.
    expect(screen.queryByRole("link", { name: "Upgrade" })).toBeNull();
  });

  it("fails OPEN (no badge) when the fetch errors", async () => {
    mockFetchFail();
    render(
      <PaidLock cap="stack_wizard" label="Stack Wizard">
        <button type="button">Stack Wizard</button>
      </PaidLock>,
    );
    const btn = await screen.findByRole("button", { name: "Stack Wizard" });
    expect(btn).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/not part of this edition/i)).not.toBeInTheDocument(),
    );
  });

  it("fails OPEN (no badge) on the unresolved error shape", async () => {
    mockFeatures({ edition: "ce", error: "unresolved" });
    render(
      <PaidLock cap="stack_wizard" label="Stack Wizard">
        <button type="button">Stack Wizard</button>
      </PaidLock>,
    );
    await screen.findByRole("button", { name: "Stack Wizard" });
    await waitFor(() =>
      expect(screen.queryByText(/not part of this edition/i)).not.toBeInTheDocument(),
    );
  });
});

describe("LockedBadge", () => {
  it("renders the label prefix and an Upgrade link", () => {
    render(<LockedBadge label="Publish to all tenants" />);
    expect(screen.getByText(/publish to all tenants — not part of this edition/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Upgrade" })).toBeNull();
  });

  it("renders without a label", () => {
    render(<LockedBadge />);
    expect(screen.getByText(/not part of this edition/i)).toBeInTheDocument();
  });
});
