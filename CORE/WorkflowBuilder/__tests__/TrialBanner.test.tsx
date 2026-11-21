/**
 * GG.UI.9 — TrialBanner fetch-once-on-mount contract.
 *
 * Proves: no trials → nothing renders; a trial expiring within 3 days →
 * banner with the item name and day count; a trial expiring in 10 days →
 * no banner; dismissal hides the banner and persists per item+expiry in
 * localStorage.
 */

import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import TrialBanner from "@/components/TrialBanner";

const DAY_MS = 24 * 60 * 60 * 1000;

function isoIn(days: number): string {
  return new Date(Date.now() + days * DAY_MS).toISOString();
}

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function trialItem(overrides: Record<string, unknown> = {}) {
  return {
    item_id: "listing-42",
    name: "Super Node Pro",
    kind: "node",
    source: "trial",
    active: true,
    expires_at: isoIn(2),
    ...overrides,
  };
}

function renderBanner(items: unknown[], fetchStatus = 200) {
  const fetchMock = jest.fn().mockResolvedValue(makeResponse(fetchStatus, { items }));
  global.fetch = fetchMock as unknown as typeof fetch;
  let utils: ReturnType<typeof render> | null = null;
  act(() => {
    // FIX-CE-TRIALS-OFF: the launch build hides the banner by default (enabled
    // defaults to false); this test proves the component CONTRACT, so it opts in.
    utils = render(<TrialBanner apiUrl="http://api.test" accessToken="tok" enabled />);
  });
  return { fetchMock, ...utils! };
}

describe("TrialBanner", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders nothing when there are no trials", async () => {
    renderBanner([
      trialItem({ source: "purchase", item_id: "listing-1", expires_at: null }),
      trialItem({ item_id: "listing-2", expires_at: isoIn(10) }),
    ]);
    // No 5xx path either — once the fetch settles, still nothing.
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled();
    });
    await act(async () => {});
    expect(screen.queryByTestId("trial-banner")).toBeNull();
  });

  it("shows the banner with name and days for a trial ending in 2 days", async () => {
    renderBanner([trialItem({ expires_at: isoIn(2) })]);
    const banner = await screen.findByTestId("trial-banner");
    expect(banner).toHaveTextContent("Super Node Pro");
    expect(banner).toHaveTextContent("ends in 2 days");
    expect(screen.getByTestId("trial-banner-buy-listing-42")).toHaveAttribute(
      "href",
      "/store/node/listing-42",
    );
  });

  it("shows no banner for a trial ending in 10 days", async () => {
    renderBanner([trialItem({ expires_at: isoIn(10) })]);
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled();
    });
    await act(async () => {});
    expect(screen.queryByTestId("trial-banner")).toBeNull();
  });

  it("dismiss hides the banner and persists per item + expiry", async () => {
    renderBanner([trialItem({ expires_at: isoIn(2) })]);
    const banner = await screen.findByTestId("trial-banner");
    await act(async () => {
      (screen.getByTestId("trial-banner-dismiss-listing-42") as HTMLButtonElement).click();
    });
    expect(screen.queryByTestId("trial-banner")).toBeNull();

    const stored = JSON.parse(localStorage.getItem("mod_trial_banner_dismissed") || "{}");
    expect(stored["listing-42"]).toBeTruthy();

    // Re-render with the SAME expiry → still hidden; NEW expiry → shows again.
    const second = renderBanner([trialItem({ expires_at: stored["listing-42"] })]);
    await waitFor(() => expect(second.fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByTestId("trial-banner")).toBeNull();

    renderBanner([trialItem({ expires_at: isoIn(1) })]);
    expect(await screen.findByTestId("trial-banner")).toBeTruthy();
  });

  it("renders nothing on a failed fetch", async () => {
    renderBanner([], 500);
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled();
    });
    await act(async () => {});
    expect(screen.queryByTestId("trial-banner")).toBeNull();
  });
});
