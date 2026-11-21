/**
 * BUG.B60 — TrialBanner fetch-once must wait for auth readiness.
 *
 * On first WorkflowBuilder load the parent's useWorkflowApi is still
 * initialising (silent-SSO round-trip, token validation, etc.) so
 * sessionStorage has no token yet.  The previous code read null at
 * render time and fired a raw fetch() with no Bearer header → 401
 * that never retried (raw fetch bypasses authenticatedFetch's retry).
 *
 * Fix: read the token from sessionStorage inside the effect (at
 * execution time) so the token saved by useWorkflowApi is visible.
 * Gate: no fetch until token is present.
 *
 * Listens for TOKEN_REFRESHED_EVENT (api-client:353) and also
 * polls sessionStorage every 500 ms (up to ~15 s) for the initial
 * mount case where saveWorkflowTokens does NOT fire that event
 * (api-client:77-89).
 *
 * This test proves:
 *  1. No accessToken prop → no fetch (gate fires).
 *  2. accessToken prop set → fetch fires once.
 *  3. No token at mount, then token stored + event dispatched →
 *     exactly one fetch with Bearer header.
 *  4. No token at mount, then token stored + timers advanced →
 *     exactly one fetch via the poll fallback.
 */

import React from "react";
import { act, render, screen, waitFor, cleanup } from "@testing-library/react";
import { TOKEN_REFRESHED_EVENT } from "@/lib/api-client";
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

describe("BUG.B60 — TrialBanner waits for auth", () => {
  // Save originals before the first test.
  let _origFetch: typeof global.fetch | undefined;

  beforeAll(() => {
    _origFetch = global.fetch;
  });

  beforeEach(() => {
    localStorage.clear();
    jest.restoreAllMocks();
    // Restore global.fetch — jest.restoreAllMocks() does NOT restore
    // raw property assignments on the global object.
    if (_origFetch) {
      global.fetch = _origFetch as any;
    }
    // Clear sessionStorage to avoid cross-test token leakage.
    sessionStorage.clear();
  });

  afterAll(() => {
    if (_origFetch) {
      global.fetch = _origFetch as any;
    }
  });

  it("does NOT fire a fetch when no accessToken prop is given (auth not ready)", () => {
    const fetchMock = jest.fn() as jest.Mock;
    global.fetch = fetchMock as any;

    // No accessToken prop — simulates production rendering.
    render(<TrialBanner apiUrl="http://api.test" enabled />);

    // Zero calls — the gate reads sessionStorage at runtime, finds nothing, skips.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fires the fetch once accessToken is provided via prop", async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      makeResponse(200, { items: [trialItem()] }),
    );
    global.fetch = fetchMock as any;

    // Token provided explicitly — simulates production after auth init.
    render(<TrialBanner apiUrl="http://api.test" accessToken="tok123" enabled />);

    // Fetch fires with the token.
    await waitFor(
      () => expect(fetchMock).toHaveBeenCalledTimes(1),
      { timeout: 5000 },
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/library",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok123" },
      }),
    );
  });

  it("fires one fetch when token is stored then event dispatched", async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      makeResponse(200, { items: [trialItem()] }),
    );
    global.fetch = fetchMock as any;

    // No token in sessionStorage at mount — override getItem.
    const origGetItem = sessionStorage.getItem.bind(sessionStorage);
    Object.defineProperty(sessionStorage, "getItem", {
      value: () => null,
      configurable: true,
    });

    try {
      render(<TrialBanner apiUrl="http://api.test" enabled />);
      expect(fetchMock).not.toHaveBeenCalled();

      // Simulate useWorkflowApi saving the token.
      sessionStorage.setItem("workflow_access_token", "tok-event");

      // Simulate the TOKEN_REFRESHED_EVENT (fires from tryRefreshToken).
      act(() => {
        window.dispatchEvent(new CustomEvent(TOKEN_REFRESHED_EVENT, {
          detail: { accessToken: "tok-event" },
        }));
      });

      await waitFor(
        () => expect(fetchMock).toHaveBeenCalledTimes(1),
        { timeout: 5000 },
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "http://api.test/library",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok-event" },
        }),
      );
    } finally {
      // Always restore — prevents the interval from test 2's effect
      // from leaking into subsequent tests.
      Object.defineProperty(sessionStorage, "getItem", {
        value: origGetItem,
        configurable: true,
      });
      cleanup(); // Unmount component, triggering effect cleanup.
    }
  });

  it("fires one fetch when token appears in sessionStorage and timers advance", async () => {
    jest.useFakeTimers();

    const fetchMock = jest.fn().mockResolvedValue(
      makeResponse(200, { items: [trialItem()] }),
    );
    global.fetch = fetchMock as any;

    // No token initially — override getItem.
    const origGetItem = sessionStorage.getItem.bind(sessionStorage);
    Object.defineProperty(sessionStorage, "getItem", {
      value: () => null,
      configurable: true,
    });

    try {
      render(<TrialBanner apiUrl="http://api.test" enabled />);
      expect(fetchMock).not.toHaveBeenCalled();

      // Advance time: poll checks at t=500ms, 1000ms, …
      act(() => { jest.advanceTimersByTime(500); });
      expect(fetchMock).not.toHaveBeenCalled();

      // Now the token appears in storage (simulates useWorkflowApi completing).
      sessionStorage.setItem("workflow_access_token", "tok-poll");
      act(() => { jest.advanceTimersByTime(500); });

      await waitFor(
        () => expect(fetchMock).toHaveBeenCalledTimes(1),
        { timeout: 5000 },
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "http://api.test/library",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok-poll" },
        }),
      );
    } finally {
      // Always restore and clean up.
      Object.defineProperty(sessionStorage, "getItem", {
        value: origGetItem,
        configurable: true,
      });
      cleanup();
      jest.useRealTimers();
    }
  });
});
