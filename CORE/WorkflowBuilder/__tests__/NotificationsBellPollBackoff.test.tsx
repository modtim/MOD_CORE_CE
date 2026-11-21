/**
 * Task 4.11 — representative component integration test.
 *
 * the internal design notes. NotificationsBell polls
 * `/notifications/unread-count` every 30s and uses the shared `usePollingBackoff`
 * hook which wraps `Poller`. This test proves the real component inherits:
 * base 30s cadence when healthy, enable gating on accessToken, backoff on
 * repeated 5xx with the self-heal contract (banner once, keep probing at maxMs,
 * reset to base on the first success), and pause-when-hidden.
 */

import React from "react";
import { act, render, screen } from "@testing-library/react";
import NotificationsBell from "@/components/notifications-bell";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

const UNREAD_COUNT = { unread_count: 5 };

function renderBell(props: Partial<React.ComponentProps<typeof NotificationsBell>> = {}) {
  return render(
    <NotificationsBell
      accessToken="tok"
      apiUrl="http://api.test"
      {...props}
    />,
  );
}

describe("NotificationsBell poll-backoff (real component)", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    jest.useFakeTimers();
    fetchMock = jest.fn().mockResolvedValue(makeResponse(200, UNREAD_COUNT));
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("polls GET /notifications/unread-count at the 30s base interval when healthy", async () => {
    const { unmount } = renderBell();
    // Immediate tick on mount
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/notifications/unread-count");

    // Just before the 30s edge (first tick fired at t=0, so t=29999 here)
    await advance(29998);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Cross the edge
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Next interval
    await advance(30000);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    unmount();
  });

  it("does not poll until accessToken is present (enable gating)", async () => {
    const { unmount, rerender } = renderBell({ accessToken: null });
    await advance(60000);
    expect(fetchMock).not.toHaveBeenCalled();

    // Supplying the token flips `enabled` true → polling starts.
    rerender(<NotificationsBell accessToken="tok" apiUrl="http://api.test" />);
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
  });

  it("backs off on 5xx, fires the connection-lost banner once, keeps probing at maxMs and resets to base on recovery", async () => {
    // First tick succeeds so the badge renders; then the API storms with 502.
    fetchMock
      .mockResolvedValueOnce(makeResponse(200, UNREAD_COUNT))
      .mockResolvedValue(makeResponse(502));
    // Deterministic jitter at the top of the window: each backed-off delay is
    // ~curDelay (capped at the 60s default maxMs), not the 30s base.
    const rand = jest.spyOn(Math, "random").mockReturnValue(0.999);
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const { unmount } = renderBell();

    await advance(1); // t=0 success -> badge populates
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("5")).toBeInTheDocument();

    await advance(30000); // t=30s: first 502
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Backoff: the next probe is NOT at the 30s base cadence...
    await advance(30000); // t=60s
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // ...but ~60s after the failure (doubled, capped at maxMs).
    await advance(30000); // t=90s
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await advance(60000); // t=150s: failure 3
    await advance(60000); // t=210s: failure 4
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(lost).not.toHaveBeenCalled();
    await advance(60000); // t=270s: failure 5 -> banner
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(lost).toHaveBeenCalledTimes(1);

    // Self-heal: no hard stop; it keeps probing at maxMs and the banner fires only once.
    await advance(60000); // t=330s
    expect(fetchMock).toHaveBeenCalledTimes(7);
    expect(lost).toHaveBeenCalledTimes(1);
    // The last good count is retained on transient errors.
    expect(screen.getByText("5")).toBeInTheDocument();

    // API recovers: the next probe succeeds and cadence resets to the 30s base.
    fetchMock.mockResolvedValue(makeResponse(200, { unread_count: 2 }));
    await advance(60000); // t=390s: success
    expect(fetchMock).toHaveBeenCalledTimes(8);
    expect(await screen.findByText("2")).toBeInTheDocument();
    await advance(30000); // t=420s: base cadence again
    expect(fetchMock).toHaveBeenCalledTimes(9);
    expect(lost).toHaveBeenCalledTimes(1);

    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    rand.mockRestore();
    unmount();
  });

  it("pauses polling while the tab is hidden and resumes on return to visible", async () => {
    const { unmount } = renderBell();
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Hide the tab.
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(120000); // hidden: no polling
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Return to visible.
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await advance(800); // resume stagger window → one refresh
    expect(fetchMock).toHaveBeenCalledTimes(2);

    unmount();
  });

  it("stops polling after unmount (no further fetches)", async () => {
    const { unmount } = renderBell();
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unmount();
    await advance(120000); // well past several 30s intervals: nothing new
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
