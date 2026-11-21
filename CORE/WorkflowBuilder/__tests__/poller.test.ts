// Tests for the resilient polling core (lib/poller.ts): the fix for the WB
// self-DDoS. Verifies backoff, stop-after-N (+ connection-lost event),
// single-flight, non-transient handling, and Response classification.
import { Poller, PollError, pollErrorForResponse } from "@/lib/poller";
import { AUTH_CONNECTION_LOST_EVENT } from "@/lib/api-client";

function makeResponse(status: number, headers: Record<string, string> = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k: string) => headers[k] ?? headers[k.toLowerCase()] ?? null },
  } as unknown as Response;
}

describe("pollErrorForResponse", () => {
  it("returns null for a 2xx", () => {
    expect(pollErrorForResponse(makeResponse(200))).toBeNull();
  });

  it("classifies 5xx and 408 as transient", () => {
    expect(pollErrorForResponse(makeResponse(502))?.transient).toBe(true);
    expect(pollErrorForResponse(makeResponse(500))?.transient).toBe(true);
    expect(pollErrorForResponse(makeResponse(408))?.transient).toBe(true);
  });

  it("classifies a plain 4xx as non-transient (no storm)", () => {
    expect(pollErrorForResponse(makeResponse(404))?.transient).toBe(false);
    expect(pollErrorForResponse(makeResponse(403))?.transient).toBe(false);
  });

  it("reads Retry-After seconds on 429/503", () => {
    const e = pollErrorForResponse(makeResponse(503, { "Retry-After": "12" }));
    expect(e?.transient).toBe(true);
    expect(e?.retryAfterMs).toBe(12000);
  });
});

describe("Poller", () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("does an immediate tick and reschedules at base on success", async () => {
    const fn = jest.fn().mockResolvedValue(undefined);
    const p = new Poller({ fn, baseMs: 1000, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // flush the immediate tick
    expect(fn).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it("backs off exponentially on transient failures", async () => {
    const fn = jest.fn().mockRejectedValue(new PollError({ transient: true }));
    // Backoff delays double: 2000 → 4000 → 8000 (fire times 2000, 6000, 14000).
    const p = new Poller({ fn, baseMs: 1000, maxMs: 60000, maxFailures: 99, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // immediate tick 1 (fail) → next @2000
    expect(fn).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(2000); // tick 2 (fail) → next @6000
    expect(fn).toHaveBeenCalledTimes(2);
    await jest.advanceTimersByTimeAsync(4000); // tick 3 (fail) → next @14000
    expect(fn).toHaveBeenCalledTimes(3);
    await jest.advanceTimersByTimeAsync(7000); // safely inside the 8000 gap (proves backoff, not base)
    expect(fn).toHaveBeenCalledTimes(3);
    await jest.advanceTimersByTimeAsync(1000); // crosses @14000 → tick 4
    expect(fn).toHaveBeenCalledTimes(4);
    p.stop();
  });

  it("after N failures emits connection-lost ONCE and keeps probing at maxMs (self-heals)", async () => {
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const fn = jest.fn().mockRejectedValue(new PollError({ transient: true }));
    const p = new Poller({ fn, baseMs: 1000, maxMs: 60000, maxFailures: 3, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // fail 1 → schedule 2000
    await jest.advanceTimersByTimeAsync(2000); // fail 2 → schedule 4000
    await jest.advanceTimersByTimeAsync(4000); // fail 3 → degraded: emit lost + schedule maxMs
    expect(fn).toHaveBeenCalledTimes(3);
    expect(lost).toHaveBeenCalledTimes(1);
    // Degraded pollers KEEP PROBING at the max cadence (they used to hard-stop
    // and freeze the UI until a tab hide→show). One probe per maxMs.
    await jest.advanceTimersByTimeAsync(60000);
    expect(fn).toHaveBeenCalledTimes(4);
    await jest.advanceTimersByTimeAsync(60000);
    expect(fn).toHaveBeenCalledTimes(5);
    // Banner is surfaced only once, not on every subsequent failed probe.
    expect(lost).toHaveBeenCalledTimes(1);
    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    p.stop();
  });

  it("self-heals: a degraded poller resets to base cadence after a probe succeeds", async () => {
    const fn = jest
      .fn<Promise<void>, []>()
      .mockRejectedValueOnce(new PollError({ transient: true }))
      .mockRejectedValueOnce(new PollError({ transient: true }))
      .mockResolvedValue(undefined); // the maxMs probe succeeds → recover
    const p = new Poller({ fn, baseMs: 1000, maxMs: 60000, maxFailures: 2, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // fail 1
    await jest.advanceTimersByTimeAsync(2000); // fail 2 → degraded, schedule maxMs
    await jest.advanceTimersByTimeAsync(60000); // probe succeeds → back to base
    const callsAfterRecover = fn.mock.calls.length;
    await jest.advanceTimersByTimeAsync(1000); // base cadence resumed
    expect(fn.mock.calls.length).toBe(callsAfterRecover + 1);
    p.stop();
  });

  it("does not count a non-transient failure toward the stop threshold", async () => {
    const lost = jest.fn();
    window.addEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    const fn = jest.fn().mockRejectedValue(new PollError({ transient: false }));
    const p = new Poller({ fn, baseMs: 1000, maxFailures: 2, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // non-transient → reschedule base
    await jest.advanceTimersByTimeAsync(1000);
    await jest.advanceTimersByTimeAsync(1000);
    expect(fn.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(lost).not.toHaveBeenCalled(); // never stopped
    window.removeEventListener(AUTH_CONNECTION_LOST_EVENT, lost);
    p.stop();
  });

  it("is single-flight: skips a tick while a request is still pending", async () => {
    let resolveFn: (() => void) | null = null;
    // Wrap in a function: TS's flow analysis doesn't see the closure's
    // assignment, so at call sites it would narrow resolveFn to null.
    const finishTick = () => { if (resolveFn) resolveFn(); };
    const fn = jest.fn().mockImplementation(
      () => new Promise<void>((res) => { resolveFn = () => res(); }),
    );
    const p = new Poller({ fn, baseMs: 1000, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1); // tick 1 starts, stays in-flight
    expect(fn).toHaveBeenCalledTimes(1);
    // A due tick while busy must NOT issue a second request. (No timer is even
    // scheduled until the in-flight one resolves, so advancing does nothing.)
    await jest.advanceTimersByTimeAsync(5000);
    expect(fn).toHaveBeenCalledTimes(1);
    finishTick(); // finish tick 1 → schedules the next
    await jest.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it("stops issuing after stop()", async () => {
    const fn = jest.fn().mockResolvedValue(undefined);
    const p = new Poller({ fn, baseMs: 1000, jitter: false });
    p.start();
    await jest.advanceTimersByTimeAsync(1);
    p.stop();
    await jest.advanceTimersByTimeAsync(10000);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
