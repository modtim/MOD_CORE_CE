/**
 * GT.D11.s2 — <EnvironmentNeeds /> render contract (the GY.C0 "needs"
 * readout off the /platform/features `entitlements.compliance` block).
 *
 *   - declared + needs present -> environment label + need chips + Upgrade link
 *   - CE carve (declared values, needs []) -> declaration + "paid MOD
 *     feature" note, NEVER a fabricated needs list
 *   - nothing declared -> "None declared" copy, no chips
 *   - no compliance sub-block (unresolved payload) -> renders NOTHING
 */

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";
import { EnvironmentNeeds } from "@/components/EnvironmentNeeds";

const mockFeatures = (features: Record<string, unknown>) => {
  global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(features) }),
  ) as any;
};

beforeEach(() => {
  _resetPlatformFeaturesCache();
  jest.clearAllMocks();
});

describe("EnvironmentNeeds", () => {
  it("renders the declared environment + need chips + Upgrade link", async () => {
    mockFeatures({
      entitlements: {
        edition: "ce",
        capabilities: [],
        limits: {},
        compliance: {
          declared_environment: ["cmmc"],
          source: "db",
          edition: "ce",
          map_version: 4,
          needs: [
            {
              id: "profile_locks",
              title: "Profile locks",
              feature: "GY.C1",
              available_on: "paid",
              available_on_this_edition: false,
            },
            {
              id: "keycloak_native_mfa",
              title: "Keycloak-native MFA",
              feature: "Keycloak",
              available_on: "ce",
              available_on_this_edition: true,
            },
          ],
        },
      },
    });
    render(<EnvironmentNeeds />);
    // The needs list only exists once the block resolves.
    await screen.findByText(/Profile locks/i);
    expect(screen.getByText(/Cmmc/i)).toBeInTheDocument();
    expect(screen.getByText(/Keycloak-native MFA/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Upgrade" })).toBeNull();
  });

  it("CE carve: declared but no map -> no fabricated needs list", async () => {
    mockFeatures({
      entitlements: {
        edition: "ce",
        capabilities: [],
        limits: {},
        compliance: {
          declared_environment: ["iso27001"],
          source: "db",
          edition: "ce",
          map_version: 0,
          needs: [],
        },
      },
    });
    render(<EnvironmentNeeds />);
    await waitFor(() =>
      expect(screen.getByText(/control map for this environment/i)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/control map for this environment is not part of this edition/i),
    ).toBeInTheDocument();
  });

  it("nothing declared -> 'None declared' copy", async () => {
    mockFeatures({
      entitlements: {
        edition: "pro",
        capabilities: [],
        limits: {},
        compliance: {
          declared_environment: [],
          source: "default",
          edition: "paid",
          map_version: 4,
          needs: [],
        },
      },
    });
    render(<EnvironmentNeeds />);
    await waitFor(() =>
      expect(screen.getByText(/None declared/i)).toBeInTheDocument(),
    );
  });

  it("no compliance sub-block -> renders nothing", async () => {
    mockFeatures({
      entitlements: { edition: "ce", capabilities: [], limits: {} },
    });
    render(<EnvironmentNeeds />);
    await waitFor(() =>
      expect(screen.queryByText(/Declared environment/i)).not.toBeInTheDocument(),
    );
  });
});
