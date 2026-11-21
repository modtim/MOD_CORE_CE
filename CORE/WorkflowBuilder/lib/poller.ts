"use client";

// Resilient polling core (framework-agnostic). One `Poller` per polled endpoint.
//
// Why this exists: the WorkflowBuilder had ~8 independent `setInterval` loops
// (RunRail runs, node-status, node-controls, fan-out, grid, progress,
// node-config, notifications) that each retried blindly every few seconds with
// NO backoff, NO stop-on-error, NO pause-when-hidden and NO single-flight. On a
// fan-out run several fire at once, and when the API slows to a crawl they keep
// firing — stacking duplicate in-flight requests and never letting the API
// recover. That death spiral turned a slow response into a full 502 storm (a
// DB-free /openapi.json timed out while the DB pool was idle: the client, not
// the DB, was overrunning the 8 uvicorn workers).
//
// This centralizes the polling POLICY in one place: exponential backoff with
// jitter, stop-after-N-failures (surfacing the existing connection-lost banner),
// pause-when-hidden, single-flight per poller, and a global stagger +
// concurrency cap across all pollers. See
// the internal design notes for the full design.

import { AUTH_CONNECTION_LOST_EVENT } from "./api-client";

// Thrown by a poll fn to signal a failed tick. `transient` failures (5xx,
// network, timeout) drive exponential backoff + the stop-after-N counter; a
// non-transient failure (a genuine 4xx that is not 408/429) does NOT storm-retry
// — it just reschedules at the base interval and the caller handles it.
export class PollError extends Error {
  transient: boolean;
  retryAfterMs?: number;
  constructor(opts?: { message?: string; transient?: boolean; retryAfterMs?: number }) {
    super(opts?.message || "poll failed");
    this.name = "PollError";
    this.transient = opts?.transient ?? true;
    this.retryAfterMs = opts?.retryAfterMs;
  }
}

// Map a fetch Response to either "proceed" (ok) or a PollError. Callers use this
// so failure detection is uniform: 429/503 carry Retry-After (backpressure),
// 408 + other 5xx are transient, and other 4xx are non-transient (no storm).
export function pollErrorForResponse(res: Response): PollError | null {
  if (res.ok) return null;
  const status = res.status;
  if (status === 429 || status === 503) {
    return new PollError({
      message: `backpressure ${status}`,
      transient: true,
      retryAfterMs: parseRetryAfter(res.headers.get("Retry-After")),
    });
  }
  if (status === 408 || status >= 500) {
    return new PollError({ message: `transient ${status}`, transient: true });
  }
  return new PollError({ message: `client ${status}`, transient: false });
}

function parseRetryAfter(value: string | null): number | undefined {
  if (!value) return undefined;
  const secs = Number(value);
  if (Number.isFinite(secs)) return Math.max(0, secs * 1000);
  const when = Date.parse(value); // HTTP-date form
  if (!Number.isNaN(when)) return Math.max(0, when - Date.now());
  return undefined;
}

// --- Global coordinator: concurrency cap so a fan-out canvas can't fire every
// poller on the same edge. Extra ticks wait for a slot instead of piling on. ---
const MAX_CONCURRENT = 6;
let inFlight = 0;
const slotWaiters: Array<() => void> = [];

function acquireSlot(): Promise<void> {
  if (inFlight < MAX_CONCURRENT) {
    inFlight += 1;
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    slotWaiters.push(() => {
      inFlight += 1;
      resolve();
    });
  });
}

function releaseSlot(): void {
  inFlight = Math.max(0, inFlight - 1);
  const next = slotWaiters.shift();
  if (next) next();
}

// --- Registry + document-visibility / network-online handling at module level,
// so a single listener drives every poller instead of N listeners. ---
const registry = new Set<Poller>();
let listenersBound = false;

function isHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

function bindGlobalListeners(): void {
  if (listenersBound || typeof window === "undefined") return;
  listenersBound = true;
  document.addEventListener("visibilitychange", () => {
    if (isHidden()) {
      registry.forEach((p) => p._onHidden());
    } else {
      registry.forEach((p) => p._onVisible());
    }
  });
  window.addEventListener("online", () => {
    registry.forEach((p) => p._onOnline());
  });
}

function emitConnectionLost(reason: string): void {
  try {
    window.dispatchEvent(new CustomEvent(AUTH_CONNECTION_LOST_EVENT, { detail: { reason } }));
  } catch {
    /* best effort — banner is a nicety, the STOP is the real fix */
  }
}

export type PollStatus = "idle" | "polling" | "backing-off" | "stopped";

export interface PollerOptions {
  // Does one poll (fetch + state update). Resolve = success; throw = failure.
  // Throw a PollError for classified failures; any other throw is transient.
  fn: () => Promise<void>;
  baseMs: number;
  maxMs?: number; // backoff cap; default 60s
  maxFailures?: number; // stop after N consecutive failures; default 5
  jitter?: boolean; // default true
  // Notified on status transitions (for optional UI); optional.
  onStatus?: (status: PollStatus) => void;
}

export class Poller {
  private fn: () => Promise<void>;
  private baseMs: number;
  private maxMs: number;
  private maxFailures: number;
  private jitter: boolean;
  private onStatus?: (status: PollStatus) => void;

  private timer: ReturnType<typeof setTimeout> | null = null;
  private busy = false;
  private failures = 0;
  private curDelay: number;
  private enabled = false;
  private stopped = false;
  private notifiedLost = false;
  private status: PollStatus = "idle";

  constructor(opts: PollerOptions) {
    this.fn = opts.fn;
    this.baseMs = opts.baseMs;
    this.maxMs = opts.maxMs ?? 60_000;
    this.maxFailures = opts.maxFailures ?? 5;
    this.jitter = opts.jitter ?? true;
    this.onStatus = opts.onStatus;
    this.curDelay = this.baseMs;
    bindGlobalListeners();
  }

  // Replace the poll fn without tearing down the timer (deps changed but the
  // poller keeps running). Kept simple: callers usually recreate via the hook.
  setFn(fn: () => Promise<void>): void {
    this.fn = fn;
  }

  private setStatus(s: PollStatus): void {
    if (this.status === s) return;
    this.status = s;
    this.onStatus?.(s);
  }

  // Start (or restart) polling: one immediate tick, then scheduled ticks.
  start(): void {
    if (this.enabled) return;
    this.enabled = true;
    this.stopped = false;
    this.failures = 0;
    this.curDelay = this.baseMs;
    registry.add(this);
    if (!isHidden()) {
      this.setStatus("polling");
      void this.tick();
    } else {
      // Hidden at start: wait for visibility (avoids an initial hidden-tab poll).
      this.setStatus("idle");
    }
  }

  stop(): void {
    this.enabled = false;
    this.clearTimer();
    registry.delete(this);
    this.setStatus("idle");
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private schedule(delay: number): void {
    this.clearTimer();
    if (!this.enabled || this.stopped) return;
    const jittered = this.jitter ? randomBetween(this.baseMs, delay) : delay;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.tick();
    }, Math.max(0, jittered));
  }

  private async tick(): Promise<void> {
    if (!this.enabled || this.stopped) return;
    if (isHidden()) return; // paused; visibilitychange will resume
    if (this.busy) return; // single-flight: skip; the next tick picks up
    this.busy = true;
    await acquireSlot();
    // Re-check after possibly waiting for a slot.
    if (!this.enabled || this.stopped) {
      releaseSlot();
      this.busy = false;
      return;
    }
    try {
      await this.fn();
      // Success — reset everything and resume base cadence. If we had surfaced
      // the connection-lost banner, allow it to fire again on a future outage.
      this.failures = 0;
      this.curDelay = this.baseMs;
      this.notifiedLost = false;
      this.setStatus("polling");
      this.schedule(this.baseMs);
    } catch (err) {
      const pe = err instanceof PollError ? err : new PollError({ transient: true });
      if (!pe.transient) {
        // Non-transient (a real 4xx) — don't storm; reschedule at base cadence
        // and let the caller's own handling deal with it.
        this.schedule(this.baseMs);
      } else {
        this.failures += 1;
        // Exponential backoff, capped, honoring server Retry-After.
        this.curDelay = Math.min(this.curDelay * 2, this.maxMs);
        const delay = Math.max(this.curDelay, pe.retryAfterMs ?? 0);
        if (this.failures >= this.maxFailures) {
          // Degraded — surface the reconnect banner ONCE and slow to the max
          // cadence, but KEEP PROBING. The old code hard-stopped here and only
          // ever resumed on a tab hide→show or a network `online` event, so a
          // focused tab whose polls failed 5x (e.g. during an API restart or a
          // slow spell) froze the UI until a manual refresh — "the UI doesn't
          // update all the time". Probing at maxMs self-heals the moment the API
          // returns (the next success resets to base cadence above) without
          // hammering; the global concurrency cap still bounds total load.
          if (!this.notifiedLost) {
            this.notifiedLost = true;
            emitConnectionLost(`poll failed ${this.failures}x`);
          }
          this.setStatus("stopped");
          this.schedule(Math.max(delay, this.maxMs));
        } else {
          this.setStatus("backing-off");
          this.schedule(delay);
        }
      }
    } finally {
      releaseSlot();
      this.busy = false;
    }
  }

  // --- global-listener callbacks ---
  _onHidden(): void {
    // Stop ticking while hidden; keep enabled so we resume on visibility.
    this.clearTimer();
  }

  _onVisible(): void {
    if (!this.enabled) return;
    if (this.stopped) {
      // A stopped poller auto-resumes when the tab returns (D3): reset + retry.
      this.stopped = false;
      this.failures = 0;
      this.curDelay = this.baseMs;
    }
    this._resumeSoon();
  }

  _onOnline(): void {
    if (!this.enabled) return;
    if (this.stopped) {
      this.stopped = false;
      this.failures = 0;
      this.curDelay = this.baseMs;
    }
    if (!isHidden()) this._resumeSoon();
  }

  // Resume the refresh over a short JITTERED window instead of firing an
  // immediate tick. If every poller ticked synchronously on visibilitychange,
  // a big graph got N simultaneous refetches + a full setNodes on one frame and
  // the tab appeared to "hang" on return. Spreading the resume ticks across a
  // small window keeps the return snappy; each still resumes base cadence after
  // its first success.
  private _resumeSoon(): void {
    this.clearTimer();
    if (!this.enabled || this.stopped || isHidden()) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.tick();
    }, Math.random() * RESUME_STAGGER_MS);
  }
}

// Max spread for the on-return resume of all pollers (see _resumeSoon).
const RESUME_STAGGER_MS = 800;

function randomBetween(min: number, max: number): number {
  if (max <= min) return min;
  return min + Math.random() * (max - min);
}
