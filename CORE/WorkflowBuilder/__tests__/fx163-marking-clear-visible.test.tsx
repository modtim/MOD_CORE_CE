/**
 * FX.163 p3b4b — data-marking banner "Clear" button visibility gate.
 *
 * The root cause of the missing Clear control was that the <button> elements
 * had zero CSS classes on the dark-red banner, making them invisible (FX.163
 * p3b4b). This test asserts the two visibility branches:
 *
 *  - an admin (canClear=true) sees one Clear button per label;
 *  - a viewer (canClear=false) sees none.
 */

import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { DataMarkingBanner } from "@/components/data-marking-banner";
import { useEntitlements } from "@/hooks/use-entitlements";
import { makeResponse, markingsResponse } from "./helpers/data-marking-fixtures";

// Previously inherited by importing data-marking-banner.test (which also
// re-registered its suites here). Paid edition so the banner fetches markings.
jest.mock("@/hooks/use-entitlements", () => ({ useEntitlements: jest.fn() }));

const PAID_ENTITLEMENTS = { edition: null, loading: false, has: jest.fn(() => true), limit: jest.fn(() => null), tierName: null, resolved: true, declared: jest.fn(() => null) };
beforeEach(() => { (useEntitlements as jest.Mock).mockReturnValue(PAID_ENTITLEMENTS); });

function renderBanner(
  handler: (url: string, init?: RequestInit) => Promise<unknown>,
  canClear: boolean,
): ReturnType<typeof render> {
  global.fetch = jest.fn(
    (url: unknown, init?: RequestInit) => handler(String(url), init),
  ) as unknown as typeof fetch;
  return render(
    <DataMarkingBanner
      resourceType="workflow"
      resourceId={7}
      position="top"
      apiUrl="http://api.test"
      accessToken="tok"
      canClear={canClear}
    />,
  );
}

describe("FX.163 p3b4b — Clear button visibility (admin vs viewer)", () => {
  beforeEach(() => jest.restoreAllMocks());

  it("renders a visible Clear button for an admin", async () => {
    renderBanner(
      async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])),
      true,
    );
    const note = await screen.findByRole("note");
    // The button exists inside the banner container
    const clearBtn = note.querySelector("[data-marking-clear-label='ITAR']");
    expect(clearBtn).not.toBeNull();
    expect(clearBtn!.textContent).toBe("Clear");
    // The button has styling classes that make it visible on the red banner
    expect(clearBtn!.className).toContain("text-white");
    expect(clearBtn!.className).toContain("bg-white/10");
  });

  it("renders a visible Clear button per label for an admin with multiple marks", async () => {
    renderBanner(
      async () =>
        makeResponse(200, markingsResponse([{ label: "CUI" }, { label: "ITAR" }])),
      true,
    );
    const note = await screen.findByRole("note");
    const buttons = note.querySelectorAll("[data-marking-clear-label]");
    expect(buttons).toHaveLength(2);
    // Both buttons have the visibility-enabling classes
    for (const btn of buttons) {
      expect(btn.className).toContain("text-white");
      expect(btn.className).toContain("bg-white/10");
    }
  });

  it("renders NO Clear button for a viewer (canClear=false)", async () => {
    renderBanner(
      async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])),
      false,
    );
    const note = await screen.findByRole("note");
    const clearBtns = note.querySelectorAll("[data-marking-clear-label]");
    expect(clearBtns).toHaveLength(0);
  });

  it("renders NO Clear button when canClear prop is omitted (default false)", async () => {
    global.fetch = jest.fn(
      async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])),
    ) as unknown as typeof fetch;
    render(
      <DataMarkingBanner
        resourceType="workflow"
        resourceId={7}
        position="top"
        apiUrl="http://api.test"
        accessToken="tok"
        // canClear omitted — defaults to false
      />,
    );
    const note = await screen.findByRole("note");
    const clearBtns = note.querySelectorAll("[data-marking-clear-label]");
    expect(clearBtns).toHaveLength(0);
  });
});
