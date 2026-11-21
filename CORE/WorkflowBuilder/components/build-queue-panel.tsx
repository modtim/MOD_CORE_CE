"use client";

import { wbNavLabel } from "@/lib/product-names";
import { useMemo, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight, Trash2, RefreshCw, Hammer } from "lucide-react";
import BuildQueueRow, { type BuildQueueEntry } from "./build-queue-row";
import WizardRunsSection from "./wizard-runs-section";

interface PanelProps {
  open: boolean;
  onClose: () => void;
  builds: BuildQueueEntry[];
  accessToken: string | null;
  apiUrl: string;
  onFinished: (taskId: number, outcome: "success" | "failed") => void;
  onDismiss: (taskId: number) => void;
  onClearCompleted: () => void;
  /** Generation-phase rows (wizard deferred via Close & notify me)
   *  render a Resume button on completion that calls this with
   *  their task_id. Parent reopens the wizard at Preview. */
  onResumeGeneration?: (taskId: number) => void;
  /** Failed Phase 2 rows render a "Retry with corrections" button
   *  that calls this with the failed task id (issue #2). Parent
   *  POSTs to /retry-with-corrections, dismisses the failed row,
   *  and adds a new row for the retried task. */
  onRetryWithCorrections?: (failedTaskId: number) => void;
  /** Bell deep-link target (`#wizard-queue/<id>`): the "Other wizard
   *  runs" section scrolls to and flashes that row. A change to the
   *  prop re-triggers the reveal. */
  focusRunId?: string | null;
  /** Opens a non-node run's artifact ("Open" button on the row). The
   *  parent decides where each ref type lands (project session panel,
   *  Dashboard workers tab, …); rows whose ref type has no view hide
   *  the button via wizardArtifactTarget. */
  onOpenArtifact?: (refType: string, refId: string) => void;
}

const PAGE_SIZE = 20;

/**
 * Dialog that shows one row per in-progress (or recently-finished)
 * build. Rows are memoized: when one row's stage updates, only that
 * row re-renders — siblings skip reconciliation. Pagination limits
 * any single render to 20 rows.
 */
export default function BuildQueuePanel({
  open,
  onClose,
  builds,
  accessToken,
  apiUrl,
  onFinished,
  onDismiss,
  onClearCompleted,
  onResumeGeneration,
  onRetryWithCorrections,
  focusRunId,
  onOpenArtifact,
}: PanelProps) {
  const [page, setPage] = useState(0);
  // LDF-4 #4 — Refresh button bumps a key that re-mounts every
  // BuildQueueRow, triggering its internal poll effect to re-fetch
  // immediately. Debounced via the disabled state (prevents rapid
  // clicks from spawning N concurrent re-fetches per row).
  // (Restored from 1a9c1813 after accidental revert in a022aa2e.)
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshKey(k => k + 1);
    // Re-enable after a short cooldown so the user can re-click.
    setTimeout(() => setRefreshing(false), 1500);
  };

  // Sort: in-progress first, newest first within each bucket.
  const sorted = useMemo(() => {
    const inProg = builds.filter((b) => !b.outcome).sort((a, b) => b.startedAt - a.startedAt);
    const done = builds.filter((b) => b.outcome).sort((a, b) => b.startedAt - a.startedAt);
    return [...inProg, ...done];
  }, [builds]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages - 1);
  const visible = sorted.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  const completedCount = useMemo(() => builds.filter((b) => b.outcome).length, [builds]);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-[720px] w-[720px] h-[620px] max-h-[90vh] flex flex-col">
        <DialogHeader className="flex-shrink-0">
          <DialogTitle className="flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Hammer className="h-5 w-5" aria-hidden="true" />
              {wbNavLabel('wb-nav-wizard-queue', 'Wizard Queue')}
              <span className="ml-2 text-sm font-normal text-gray-500">
                ({sorted.length} {sorted.length === 1 ? "build" : "builds"})
              </span>
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={onRefresh}
                disabled={refreshing}
                className="text-xs text-gray-500 hover:text-brand-600"
                title="Re-poll every row"
              >
                <RefreshCw className={`w-3.5 h-3.5 mr-1 ${refreshing ? "animate-spin" : ""}`} />
                Refresh
              </Button>
              {completedCount > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onClearCompleted}
                  className="text-xs text-gray-500 hover:text-red-600"
                >
                  <Trash2 className="w-3.5 h-3.5 mr-1" />
                  Clear {completedCount} completed
                </Button>
              )}
            </div>
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-2 pr-1">
          {visible.length === 0 && (
            <div className="h-full flex items-center justify-center text-sm text-gray-400">
              No builds queued. Click <strong className="mx-1">+ New Node</strong> to start one.
            </div>
          )}
          {visible.map((entry) => (
            <BuildQueueRow
              key={`${entry.taskId}-${refreshKey}`}
              entry={entry}
              accessToken={accessToken}
              apiUrl={apiUrl}
              onFinished={onFinished}
              onDismiss={onDismiss}
              onResumeGeneration={onResumeGeneration}
              onRetryWithCorrections={onRetryWithCorrections}
            />
          ))}
        </div>

        <WizardRunsSection
          accessToken={accessToken}
          apiUrl={apiUrl}
          focusRunId={focusRunId}
          onOpenArtifact={onOpenArtifact}
        />

        {totalPages > 1 && (
          <div className="flex items-center justify-between pt-3 border-t flex-shrink-0">
            <Button
              variant="outline"
              size="sm"
              disabled={currentPage === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeft className="w-4 h-4 mr-1" />
              Prev
            </Button>
            <span className="text-sm text-gray-500 tabular-nums">
              Page {currentPage + 1} of {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={currentPage + 1 >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
              <ChevronRight className="w-4 h-4 ml-1" />
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
