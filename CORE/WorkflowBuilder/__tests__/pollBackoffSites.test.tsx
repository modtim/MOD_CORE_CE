/**
 * Task 4.11 — per-site poll-backoff integration tests.
 *
 * the internal design notes. The core `Poller` is unit
 * tested in poller.test.ts; this suite verifies the BEHAVIOR at the site level:
 * every migrated hot poll site routes through `usePollingBackoff`, so exercising
 * the hook with a realistic per-site poll fn (mocked `fetch` + `pollErrorForResponse`)
 * proves each site inherits base cadence, backoff, the self-heal contract (the
 * connection-lost banner fires once, probing continues at maxMs, cadence resets
 * to base on the first success), 4xx no-storm, backpressure, pause-when-hidden,
 * single-flight, and the global stagger/concurrency cap.
 *
 * Base intervals asserted here mirror the real callers:
 *   RunRail runs 4s, useWorkflowApi node-status 4s / node-controls 3s|6s /
 *   fanout-summary 2s, FanoutModal 3s, GridView 4s, workflow-progress 3s,
 *   notifications-bell 30s.
 */
import { renderHook, act } from "@testing-library/react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse, PollError } from "@/lib/poller";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

// --- helpers -------------------------------------------------------------

function makeResponse(status: number, headers: Record<string, string> = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k: string) => headers[k] ?? headers[k.toLowerCase()] ?? null },
    json: async () => ({}),
  } as unknown as Response;
}

// A poll fn shaped exactly like the migrated sites: fetch, classify via
// pollErrorForResponse, throw on failure so the poller can back off.
function siteFetchFn(fetchMock: jest.Mock): () => Promise<void> {
  return async () => {
    const res = await fetchMock();
    const err = pollErrorForResponse(res);
    if (err) throw err;
    await res.json();
  };
}

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  Object.defineProperty(document, "hidden", { configurable: true, get: () => state === "hidden" });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

// Mount a single poller through the hook (mirrors what every migrated site does).
function mountPoller(
  fn: () => Promise<void>,
  opts: { baseMs: number; enabled?: boolean; jitter?: boolean; maxFailures?: number; maxMs?: number },
) {
  return renderHook(() =>
    usePollingBackoff(fn, { enabled: true, jitter: false, ...opts }, []),
  );
}

describe("poll-backoff at the site level (usePollingBackoff)", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    setVisibility("visible");
  });

  // ---- Healthy behavior unchanged: base cadence per site -----------------
  describe("healthy cadence matches today's base interval (Requirement: Healthy behavior is unchanged)", () => {
    // Real callers do NOT pass jitter:false; on success the scheduled delay is
    // baseMs and jitter is a no-op (random(base, base) === base), so cadence is
    // deterministic even with the production default. We assert that here.
    const cases: Array<{ name: string; baseMs: number }> = [
      { name: "RunRail runs", baseMs: 4000 },
      { name: "node-status", baseMs: 4000 },
      { name: "node-controls active", baseMs: 3000 },
      { name: "node-controls idle", baseMs: 6000 },
      { name: "fanout-summary", baseMs: 2000 },
      { name: "FanoutModal / workflow-progress", baseMs: 3000 },
      { name: "notifications-bell", baseMs: 30000 },
    ];
    it.each(cases)("$name polls every $baseMs ms when succeeding (production jitter default)", async ({ baseMs }) => {
      const fetchMock = jest.fn().mockResolvedValue(makeResponse(200));
      const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs, jitter: true });
      await advance(1); // immediate tick on start
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await advance(baseMs - 2); // just before the next edge — no early/faster poll
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await advance(2); // cross the edge
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await advance(baseMs); // one more base interval
      expect(fetchMock).toHaveBeenCalledTimes(3);
      unmount();
    });
  });

  it("does not poll while enabled=false (enable/disable conditions preserved)", async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200));
    const { unmount } = renderHook(() =>
      usePollingBackoff(siteFetchFn(fetchMock), { baseMs: 4000, enabled: false, jitter: false }, []),
    );
    await advance(60000);
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();
  });

  // ---- Backoff grows on 5xx, resets on success ---------------------------
  it("backs off exponentially on repeated 5xx and resets to base on recovery (Requirement: Exponential backoff with jitter)", async () => {
    // base 4000 → backoff fires at 8000, 16000, ... . jitter:false makes it exact.
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(makeResponse(502))
      .mockResolvedValueOnce(makeResponse(502))
      .mockResolvedValueOnce(makeResponse(502))
      .mockResolvedValue(makeResponse(200));
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000, maxFailures: 99 });
    await advance(1); // tick 1 fails → next @ 8000
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(7000); // inside the 8000 gap — proves it is NOT still 4s
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(1000); // cross 8000 → tick 2 fails → next @ 16000
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(15000); // inside the 16000 gap
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(1000); // cross 16000 → tick 3 fails → next @ 32000
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await advance(32000); // tick 4 succeeds → delay resets to base 4000
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await advance(4000); // back at base cadence
    expect(fetchMock).toHaveBeenCalledTimes(5);
    unmount();
  });

  // ---- Self-heal after N failures + connection-lost banner path ----------
  it("after N consecutive 5xx fires AUTH_CONNECTION_LOST_EVENT once, keeps probing at maxMs and resets to base on the first success (Requirement: Stop after repeated failures / self-heal)", async () => {
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(503)); // always transient
    // maxMs defaults to 60000; jitter:false makes every delay exact.
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000, maxFailures: 3 });
    await advance(1); // t=0 fail 1 → next @ 8000
    await advance(8000); // t=8000 fail 2 → next @ 24000
    expect(lost).not.toHaveBeenCalled();
    await advance(16000); // t=24000 fail 3 → banner + next probe at maxMs (@ 84000)
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(lost).toHaveBeenCalledTimes(1);
    // No dead-stop, but no storm either: the next probe waits the full maxMs.
    await advance(59998); // t=83999
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await advance(2); // t=84001 fail 4 → next @ 144000
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await advance(60000); // t=144001 fail 5 → next @ 204000
    expect(fetchMock).toHaveBeenCalledTimes(5);
    // The banner fires only once per outage.
    expect(lost).toHaveBeenCalledTimes(1);
    // API recovers: the next maxMs probe succeeds and cadence resets to base.
    fetchMock.mockResolvedValue(makeResponse(200));
    await advance(60000); // t=204001 success → next @ 208000
    expect(fetchMock).toHaveBeenCalledTimes(6);
    await advance(3998); // t=207999 — just before the base edge
    expect(fetchMock).toHaveBeenCalledTimes(6);
    await advance(2); // t=208001 base cadence again
    expect(fetchMock).toHaveBeenCalledTimes(7);
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(8);
    expect(lost).toHaveBeenCalledTimes(1);
    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  it("auto-resumes a stopped poller on visibilitychange→visible and resets the failure count (Requirement: Stop after repeated failures / resume)", async () => {
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    // 3 failures to stop, then healthy on resume.
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(makeResponse(503))
      .mockResolvedValueOnce(makeResponse(503))
      .mockResolvedValueOnce(makeResponse(503))
      .mockResolvedValue(makeResponse(200));
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000, maxFailures: 3 });
    await advance(1);
    await advance(8000);
    await advance(16000); // STOP
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(lost).toHaveBeenCalledTimes(1);
    // Hide then show — the poller must come back and resume at base cadence.
    setVisibility("hidden");
    setVisibility("visible");
    await advance(800); // resume stagger window
    expect(fetchMock).toHaveBeenCalledTimes(4); // resumed with a fresh tick
    await advance(4000); // failure count was reset → healthy base cadence again
    expect(fetchMock).toHaveBeenCalledTimes(5);
    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  // ---- Non-retryable 4xx must not storm ----------------------------------
  it("a non-retryable 4xx does not storm-retry or stop (Requirement: non-retryable client error)", async () => {
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(404));
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000, maxFailures: 3 });
    await advance(1); // tick 1 (404) → reschedule at BASE, not exponential
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(4000); // still base cadence (not doubled)
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // A 4xx never counts toward stop-after-N → banner never fires.
    await advance(20000);
    expect(lost).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    unmount();
  });

  // ---- Server backpressure ----------------------------------------------
  it("honors Retry-After on 503, waiting at least that long (Requirement: Honor server backpressure)", async () => {
    // Retry-After 30s dwarfs the first backoff step (base 4000 → 8000), so the
    // next attempt must wait ~30s, not 8s.
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(makeResponse(503, { "Retry-After": "30" }))
      .mockResolvedValue(makeResponse(200));
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000, maxFailures: 99 });
    await advance(1); // tick 1: 503 + Retry-After 30s
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(8000); // the plain backoff step — must NOT have fired yet
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(22000); // cross the 30s Retry-After mark
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  // ---- Single-flight -----------------------------------------------------
  it("single-flight: a slow in-flight request is not stacked by the next tick (Requirement: Single-flight per poller)", async () => {
    let resolveInFlight: (() => void) | null = null;
    // Wrap in a function: TS's flow analysis doesn't see the closure's
    // assignment, so at call sites it would narrow resolveInFlight to null.
    const finishInFlight = () => { if (resolveInFlight) resolveInFlight(); };
    const fetchMock = jest.fn().mockImplementation(
      () =>
        new Promise<Response>((res) => {
          resolveInFlight = () => res(makeResponse(200));
        }),
    );
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000 });
    await advance(1); // tick 1 starts, stays in-flight
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Even long past several base intervals, no duplicate request is issued.
    await advance(20000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    finishInFlight(); // finish tick 1 → schedules the next
    await advance(4000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    finishInFlight(); // resolve the 2nd so no slot leaks between tests
    await advance(1);
    unmount();
  });

  // ---- Pause when hidden -------------------------------------------------
  it("does not poll while the tab is hidden and resumes when visible (Requirement: Pause polling when the tab is hidden)", async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200));
    const { unmount } = mountPoller(siteFetchFn(fetchMock), { baseMs: 4000 });
    await advance(1); // one healthy tick
    expect(fetchMock).toHaveBeenCalledTimes(1);
    setVisibility("hidden");
    await advance(30000); // hidden: no ticks at all
    expect(fetchMock).toHaveBeenCalledTimes(1);
    setVisibility("visible");
    await advance(800); // one immediate refresh on return (within stagger window)
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(4000); // then base cadence resumes
    expect(fetchMock).toHaveBeenCalledTimes(3);
    unmount();
  });

  // ---- Global stagger: no synchronous burst on return --------------------
  it("staggers many pollers on return to visible — no synchronous burst of all polls at once (Requirement: Global stagger and concurrency cap)", async () => {
    const N = 6;
    const fetchMocks = Array.from({ length: N }, () => jest.fn().mockResolvedValue(makeResponse(200)));
    const hooks = fetchMocks.map((fm) => mountPoller(siteFetchFn(fm), { baseMs: 4000 }));
    await advance(1); // initial tick for each
    fetchMocks.forEach((fm) => expect(fm).toHaveBeenCalledTimes(1));
    setVisibility("hidden");
    await advance(10000);
    fetchMocks.forEach((fm) => expect(fm).toHaveBeenCalledTimes(1)); // paused
    setVisibility("visible");
    // The key assertion: on return, the resume ticks are deferred by a jittered
    // timer, so NONE fire synchronously on the visibilitychange edge.
    await advance(0);
    const firedSynchronously = fetchMocks.filter((fm) => fm.mock.calls.length > 1).length;
    expect(firedSynchronously).toBe(0);
    // After the full stagger window, every poller has refreshed exactly once.
    await advance(800);
    fetchMocks.forEach((fm) => expect(fm).toHaveBeenCalledTimes(2));
    hooks.forEach((h) => h.unmount());
  });

  // ---- Global concurrency cap -------------------------------------------
  it("caps concurrent in-flight polls at 6; extra ticks wait for a slot (Requirement: Global stagger and concurrency cap)", async () => {
    const resolvers: Array<() => void> = [];
    const sharedFn = jest.fn(
      () => new Promise<void>((res) => { resolvers.push(res); }),
    );
    // 8 pollers all start together; the coordinator must let only 6 run at once.
    const hooks = Array.from({ length: 8 }, () => mountPoller(sharedFn, { baseMs: 4000 }));
    await advance(1);
    expect(sharedFn).toHaveBeenCalledTimes(6); // 2 waiting for a slot
    resolvers[0](); // free one slot
    await advance(1);
    expect(sharedFn).toHaveBeenCalledTimes(7); // a waiter got in
    resolvers[1]();
    await advance(1);
    expect(sharedFn).toHaveBeenCalledTimes(8);
    // Drain everything so no slot/in-flight state leaks into later tests.
    resolvers.forEach((r) => r());
    await advance(1);
    hooks.forEach((h) => h.unmount());
    await advance(1);
  });
});
