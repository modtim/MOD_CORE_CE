"use client";

// GE.WQR.15 — self-fetching section mounted inside BuildQueuePanel
// (GE.WQR.16) that lists non-node wizard runs (stack / agent-project /
// build-repair / container-adapter). The existing node rows
// (BuildQueueRow) remain the NODE case and are untouched by this file.
//
// Polls via usePollingBackoff (no setInterval — see lib/poller.ts for
// why: unbounded setInterval loops caused a platform-wide 502 storm).
// Filtering is defence-in-depth only: the server already scopes the
// list to the caller (services/wizard_queue.py::scope_query), so
// isOwnScope only ever narrows, never widens, what the server returned.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import {
  getWizardRun,
  isOwnScope,
  listWizardRuns,
  type WizardQueueViewer,
  type WizardRun,
} from "@/lib/wizard-queue";
import WizardRunRow from "./wizard-run-row";

const PAGE_SIZE = 20;
const HIGHLIGHT_MS = 3000;
// How long the reveal effect keeps looking for a paged/fetched row before
// giving up (the row should mount within one poll cycle of the state update).
const REVEAL_WINDOW_MS = 1000;

interface SectionProps {
  accessToken: string | null;
  apiUrl: string;
  viewer?: WizardQueueViewer | null;
  onOpenArtifact?: (refType: string, refId: string) => void;
  /** Run id the panel should scroll to and flash (bell deep-link
   *  `#wizard-queue/<id>`). Changes to a new id re-trigger the reveal;
   *  passing the same id twice is a no-op. */
  focusRunId?: string | null;
}

export default function WizardRunsSection({
  accessToken,
  apiUrl,
  viewer,
  onOpenArtifact,
  focusRunId,
}: SectionProps) {
  const [runs, setRuns] = useState<WizardRun[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [highlightedRunId, setHighlightedRunId] = useState<string | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A reveal in flight: the run id plus a deadline by which the row must be
  // found. Driven by the effect below (not a one-shot timer in the fetch
  // path) so a poll-induced re-render mid-fetch can't cancel the reveal.
  const focus = useRef<{ id: string; deadline: number } | null>(null);
  // Ids we pulled in via the single-run endpoint (deep-link / retry-of
  // reveal) that fall outside the paginated server list. Tracked so a poll
  // can preserve them instead of clobbering a reveal that just landed.
  const prependedIds = useRef<Set<string>>(new Set());

  const flashRun = useCallback((id: string) => {
    if (highlightTimer.current) clearTimeout(highlightTimer.current);
    setHighlightedRunId(id);
    highlightTimer.current = setTimeout(() => {
      highlightTimer.current = null;
      setHighlightedRunId((cur) => (cur === id ? null : cur));
    }, HIGHLIGHT_MS);
  }, []);

  // Shared highlight helper: scroll the row into view and flash a ring.
  // Used by the "retry of …" back-link AND the bell deep-link focus.
  const revealRun = useCallback(
    (id: string) => {
      const el = document.querySelector<HTMLElement>(
        `[data-run-id="${CSS.escape(id)}"]`,
      );
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        flashRun(id);
      }
    },
    [flashRun],
  );

  // Reveal-or-fetch: if the run is already in the list, page to it (if
  // needed) and reveal in place; otherwise fetch it via the single-run
  // endpoint and prepend it. A fetch failure (404 = not accessible or
  // gone) leaves the list untouched — the affordance stays inert.
  const revealOrFetchRun = useCallback(
    async (id: string, onFetchFail?: (id: string) => void) => {
      const idx = runs.findIndex((r) => r.id === id);
      if (idx >= 0) {
        const targetPage = Math.floor(idx / PAGE_SIZE);
        if (targetPage !== page) {
          setPage(targetPage);
          focus.current = { id, deadline: Date.now() + REVEAL_WINDOW_MS };
          return;
        }
        revealRun(id);
        return;
      }
      if (!accessToken) return;
      try {
        const fetched = await getWizardRun(id, apiUrl);
        prependedIds.current.add(id);
        setRuns((prev) => (prev.some((r) => r.id === id) ? prev : [fetched, ...prev]));
        setTotalCount((c) => c + 1);
      } catch {
        // 404 (inaccessible or gone) / network — the row can't be shown.
        onFetchFail?.(id);
        return;
      }
      // Hand the reveal to the effect below rather than a one-shot timer:
      // a poll-induced re-render right after the prepend would cancel a
      // pending timer before the row commits. The effect polls for the row
      // until its deadline.
      if (page !== 0) setPage(0);
      focus.current = { id, deadline: Date.now() + REVEAL_WINDOW_MS };
    },
    [runs, page, accessToken, apiUrl, revealRun],
  );

  // Reveal a run that revealOrFetchRun just paged to / fetched, once the
  // row has mounted. Polls every 50ms until the row appears or the
  // deadline passes — resilient to the poller's re-renders, which would
  // otherwise cancel a one-shot reveal timer mid-flight.
  useEffect(() => {
    if (!focus.current) return;
    const f = focus.current;
    // Reveal synchronously when the row is already committed (the effect
    // runs after the render that mounted it) — the poller's re-renders make
    // a one-shot/repeating timer unreliable, so don't defer to one here.
    const el = document.querySelector<HTMLElement>(
      `[data-run-id="${CSS.escape(f.id)}"]`,
    );
    if (el) {
      focus.current = null;
      revealRun(f.id);
      return;
    }
    // Row not mounted yet (e.g. a page change is in flight) — poll until it
    // appears or the deadline passes.
    const timer = setInterval(() => {
      const cur = focus.current;
      if (!cur) return;
      const row = document.querySelector<HTMLElement>(
        `[data-run-id="${CSS.escape(cur.id)}"]`,
      );
      if (row) {
        focus.current = null;
        clearInterval(timer);
        revealRun(cur.id);
      } else if (Date.now() > cur.deadline) {
        focus.current = null;
        clearInterval(timer);
      }
    }, 50);
    return () => clearInterval(timer);
  }, [runs, page, revealRun]);

  // External focus request (bell deep-link `#wizard-queue/<id>`).
  const lastFocus = useRef<string | null>(null);
  useEffect(() => {
    if (!focusRunId || focusRunId === lastFocus.current) return;
    lastFocus.current = focusRunId;
    void revealOrFetchRun(focusRunId);
  }, [focusRunId, revealOrFetchRun]);

  useEffect(
    () => () => {
      if (highlightTimer.current) clearTimeout(highlightTimer.current);
    },
    [],
  );

  const fetchPage = useCallback(async () => {
    const resp = await listWizardRuns(page, apiUrl);
    setRuns((prev) => {
      // Preserve runs we pulled in via the single-run endpoint while they
      // are still outside the paginated list — a poll must not wipe a
      // deep-link / retry-of reveal that just landed (the poller fires an
      // immediate tick on start, right alongside revealOrFetchRun).
      const fresh = new Set(resp.runs.map((r) => r.id));
      for (const pid of [...prependedIds.current]) {
        if (fresh.has(pid)) prependedIds.current.delete(pid);
      }
      const extras = prev.filter(
        (r) => prependedIds.current.has(r.id) && !fresh.has(r.id),
      );
      return [...resp.runs, ...extras];
    });
    setTotalCount(resp.total_count);
    setLoading(false);
    setLoadError(null);
  }, [page, apiUrl]);

  usePollingBackoff(
    async () => {
      try {
        await fetchPage();
      } catch (err) {
        setLoadError((err as Error)?.message || "Failed to load wizard runs");
        throw err;
      }
    },
    { baseMs: 4000, enabled: !!accessToken },
    [accessToken, page, apiUrl],
  );

  // Server already scopes to the caller; this only ever narrows further
  // (node rows render via BuildQueueRow, not here) and never re-adds a
  // row the server excluded.
  const visible = useMemo(
    () =>
      runs.filter((r) => r.wizard_type !== "node" && isOwnScope(r, viewer ?? null)),
    [runs, viewer],
  );

  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));

  const handleChanged = useCallback((updated: WizardRun) => {
    setRuns((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
  }, []);

  const handleDismissed = useCallback((id: string) => {
    setRuns((prev) => prev.filter((r) => r.id !== id));
    setTotalCount((c) => Math.max(0, c - 1));
  }, []);

  const handleRetried = useCallback((created: WizardRun) => {
    setRuns((prev) => [created, ...prev]);
    setTotalCount((c) => c + 1);
  }, []);

  // "retry of …" back-link: reveal the original run in place, or fetch
  // + prepend it when it isn't in the list (soft-deleted, other tenant,
  // older than the current window, …). The row itself renders plain
  // text when the single-run endpoint 404s (see handleOpenRunResult
  // below), so a dead link never masquerades as a working one.
  const [unresolvableRunIds, setUnresolvableRunIds] = useState<Set<string>>(
    () => new Set(),
  );
  const markUnresolvable = useCallback((id: string) => {
    setUnresolvableRunIds((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);
  const handleOpenRun = useCallback(
    (id: string) => {
      void (async () => {
        // If the run isn't in the list, revealOrFetchRun tries the
        // single-run endpoint; on 404 it marks the id unresolvable so
        // the back-link downgrades to plain text.
        const inList = runs.some((r) => r.id === id);
        await revealOrFetchRun(id, (failedId) => {
          if (!inList) markUnresolvable(failedId);
        });
      })();
    },
    [runs, revealOrFetchRun, markUnresolvable],
  );

  return (
    <section data-testid="wizard-runs-section" className="mt-4 pt-3 border-t">
      <h3 className="text-sm font-semibold text-gray-600 mb-2">Other wizard runs</h3>

      {loading && runs.length === 0 && (
        <div className="flex items-center justify-center py-6 text-sm text-gray-400">
          <Loader2 className="h-4 w-4 animate-spin mr-2" />
          Loading…
        </div>
      )}

      {!loading && visible.length === 0 && (
        <div className="py-6 text-center text-sm text-gray-400">No wizard runs.</div>
      )}

      {loadError && runs.length === 0 && !loading && (
        <div className="py-2 text-center text-xs text-red-600">{loadError}</div>
      )}

      {visible.length > 0 && (
        <div className="space-y-2">
          {visible.map((run) => (
            <WizardRunRow
              key={run.id}
              // Client-side-only presentation hints ride on the run object
              // (see WizardRun.originalRunAvailable / .highlighted).
              run={{
                ...run,
                originalRunAvailable:
                  !!run.retry_of_id && !unresolvableRunIds.has(run.retry_of_id),
                highlighted: highlightedRunId === run.id,
              }}
              apiUrl={apiUrl}
              onChanged={handleChanged}
              onDismissed={handleDismissed}
              onRetried={handleRetried}
              onOpenRun={handleOpenRun}
              onOpenArtifact={onOpenArtifact}
            />
          ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-3">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            <ChevronLeft className="w-4 h-4 mr-1" />
            Prev
          </Button>
          <span className="text-xs text-gray-500 tabular-nums">
            Page {page + 1} of {totalPages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
            <ChevronRight className="w-4 h-4 ml-1" />
          </Button>
        </div>
      )}
    </section>
  );
}
