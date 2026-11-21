"use client";

// Thin React wrapper over the `Poller` core (lib/poller.ts). Every WorkflowBuilder
// interval poll routes through this instead of a raw `setInterval`, so backoff,
// stop-after-N, pause-when-hidden, single-flight and the global concurrency cap
// are enforced uniformly. See the internal design notes.
//
// Usage — replaces the old `useEffect(() => { fn(); const iv = setInterval(fn,
// ms); return () => clearInterval(iv); }, deps)` shape:
//
//   usePollingBackoff(fetchRuns, { baseMs: 4000, enabled: !!workflowId }, [workflowId]);
//
// The polled `fn` MUST throw on failure (throw a PollError for classified
// failures, or use `pollErrorForResponse(res)`), so the poller can back off.
// Returning normally is treated as success and resets the backoff.

import { useEffect, useRef } from "react";
import { Poller, type PollStatus, type PollError } from "@/lib/poller";

export interface UsePollingBackoffOptions {
  baseMs: number;
  enabled?: boolean; // default true
  maxMs?: number;
  maxFailures?: number;
  jitter?: boolean;
  onStatus?: (status: PollStatus) => void;
}

export function usePollingBackoff(
  fn: () => Promise<void>,
  opts: UsePollingBackoffOptions,
  deps: React.DependencyList,
): void {
  const { baseMs, enabled = true, maxMs, maxFailures, jitter, onStatus } = opts;

  // Keep the latest fn/onStatus in refs so a re-render that only changes the
  // closure does not tear down and restart the poller (which would reset
  // backoff and fire an immediate tick every render).
  const fnRef = useRef(fn);
  const statusRef = useRef(onStatus);
  fnRef.current = fn;
  statusRef.current = onStatus;

  useEffect(() => {
    if (!enabled) return;
    const poller = new Poller({
      fn: () => fnRef.current(),
      baseMs,
      maxMs,
      maxFailures,
      jitter,
      onStatus: (s) => statusRef.current?.(s),
    });
    poller.start();
    return () => poller.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, baseMs, maxMs, maxFailures, jitter, ...deps]);
}

export type { PollStatus, PollError };
