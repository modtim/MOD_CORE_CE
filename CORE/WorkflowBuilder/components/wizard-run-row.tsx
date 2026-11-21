"use client";

// GE.WQR.14 — one row for a non-node wizard run inside the Wizard Queue
// panel (see wizard-runs-section.tsx). Mirrors the memoized-row pattern
// used by BuildQueueRow: this component owns its own log-stream state so
// expanding/collapsing one row's log drawer never re-renders its siblings.

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  ExternalLink,
  Loader2,
  RotateCcw,
  Wand2,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  cancelWizardRun,
  dismissWizardRun,
  retryWizardRun,
  streamWizardRunLogs,
  WIZARD_STATUS_LABEL,
  WIZARD_TYPE_LABEL,
  wizardArtifactTarget,
  type WizardRun,
  type WizardRunStreamLine,
} from "@/lib/wizard-queue";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const STATUS_BADGE_VARIANT: Record<WizardRun["status"], BadgeVariant> = {
  queued: "outline",
  "waiting-for-worker": "outline",
  running: "secondary",
  verifying: "secondary",
  "waiting-for-user": "secondary",
  succeeded: "default",
  failed: "destructive",
  canceled: "destructive",
};

function statusIcon(status: WizardRun["status"]) {
  switch (status) {
    case "succeeded":
      return <CheckCircle2 className="h-4 w-4 text-green-600" aria-hidden="true" />;
    case "failed":
      return <XCircle className="h-4 w-4 text-red-600" aria-hidden="true" />;
    case "canceled":
      return <AlertTriangle className="h-4 w-4 text-amber-600" aria-hidden="true" />;
    case "running":
    case "verifying":
      return <Loader2 className="h-4 w-4 text-blue-500 animate-spin" aria-hidden="true" />;
    default:
      return <Clock className="h-4 w-4 text-gray-400" aria-hidden="true" />;
  }
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

function formatStart(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

interface RowProps {
  run: WizardRun;
  apiUrl: string;
  /** Called after a cancel/retry/dismiss action succeeds so the parent
   *  section can re-fetch or optimistically update its list. */
  onChanged: (updated: WizardRun) => void;
  onDismissed: (id: string) => void;
  /** Retry produces a brand-new run linked via retry_of_id; parent may
   *  want to select/scroll to it once created. Optional. */
  onRetried?: (created: WizardRun) => void;
  /** When this run is itself a retry of another, clicking the back-link
   *  asks the parent to reveal/scroll to that original run. */
  onOpenRun?: (id: string) => void;
  /** Opens the produced artifact (target_ref_type/target_ref_id). */
  onOpenArtifact?: (refType: string, refId: string) => void;
  // Two client-side-only presentation hints ride on `run` itself (see the
  // optional `originalRunAvailable` / `highlighted` fields on WizardRun):
  // whether the retry-of back-link is a live link or plain text, and
  // whether the section is currently highlighting this row.
}

function WizardRunRowInner({
  run,
  apiUrl,
  onChanged,
  onDismissed,
  onRetried,
  onOpenRun,
  onOpenArtifact,
}: RowProps) {
  const [logsOpen, setLogsOpen] = useState(false);
  const [logLines, setLogLines] = useState<WizardRunStreamLine[]>([]);
  const [busy, setBusy] = useState<"cancel" | "retry" | "dismiss" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  const stopStream = useCallback(() => {
    if (closeStreamRef.current) {
      closeStreamRef.current();
      closeStreamRef.current = null;
    }
  }, []);

  // Open/close the log stream strictly in step with `logsOpen`, and
  // always tear it down on unmount — the caller-facing contract for
  // streamWizardRunLogs's close() (see lib/wizard-queue.ts) requires
  // this on both collapse AND unmount.
  useEffect(() => {
    if (!logsOpen) return;
    setLogLines([]);
    closeStreamRef.current = streamWizardRunLogs(
      run.id,
      (line) => {
        setLogLines((prev) => {
          if (prev.some((p) => p.seq === line.seq)) return prev;
          return [...prev, line].sort((a, b) => a.seq - b.seq);
        });
      },
      apiUrl,
    );
    return () => stopStream();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logsOpen, run.id, apiUrl]);

  const toggleLogs = () => setLogsOpen((v) => !v);

  const handleCancel = async () => {
    setBusy("cancel");
    setActionError(null);
    try {
      const updated = await cancelWizardRun(run.id, apiUrl);
      onChanged(updated);
    } catch (err) {
      setActionError((err as Error)?.message || "Cancel failed");
    } finally {
      setBusy(null);
    }
  };

  const handleRetry = async () => {
    setBusy("retry");
    setActionError(null);
    try {
      const created = await retryWizardRun(run.id, apiUrl);
      onRetried?.(created);
    } catch (err) {
      setActionError((err as Error)?.message || "Retry failed");
    } finally {
      setBusy(null);
    }
  };

  const handleDismiss = async () => {
    setBusy("dismiss");
    setActionError(null);
    try {
      await dismissWizardRun(run.id, apiUrl);
      onDismissed(run.id);
    } catch (err) {
      setActionError((err as Error)?.message || "Dismiss failed");
      setBusy(null);
    }
  };

  const canDismissRow = run.status === "succeeded" || run.status === "failed" || run.status === "canceled";

  return (
    <div
      data-testid="wizard-run-row"
      data-status={run.status}
      data-run-id={run.id}
      className={`border rounded-lg px-3 py-2 bg-white ${
        run.highlighted ? "ring-2 ring-brand-500" : ""
      }`}
    >
      <div className="flex items-center gap-3">
        <Wand2 className="h-4 w-4 text-gray-400 flex-shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-500">
              {WIZARD_TYPE_LABEL[run.wizard_type]}
            </span>
            <span className="text-sm font-semibold truncate">
              {run.target_name || "(untitled)"}
            </span>
          </div>
          {run.retry_of_id &&
            (run.originalRunAvailable ? (
              <button
                type="button"
                className="text-xs text-brand-600 hover:underline"
                onClick={() => onOpenRun?.(run.retry_of_id as string)}
              >
                retry of {run.retry_of_id.slice(0, 8)}…
              </button>
            ) : (
              <span className="text-xs text-gray-400">
                retry of {run.retry_of_id.slice(0, 8)}…
              </span>
            ))}
        </div>

        <Badge
          data-status={run.status}
          variant={STATUS_BADGE_VARIANT[run.status]}
          className="flex items-center gap-1 text-xs flex-shrink-0"
        >
          {statusIcon(run.status)}
          {WIZARD_STATUS_LABEL[run.status]}
        </Badge>
      </div>

      <div className="mt-2 flex items-center gap-3">
        <Progress value={run.progress} className="h-1.5 flex-1" />
        <span className="text-xs tabular-nums text-gray-500 w-9 text-right">
          {Math.round(run.progress)}%
        </span>
      </div>

      <div className="mt-1.5 flex items-center justify-between text-xs text-gray-500">
        <span>Started {formatStart(run.started_at)}</span>
        <span>{formatDuration(run.duration_seconds)}</span>
      </div>

      {run.error_message && (
        <div className="mt-1.5 text-xs text-red-600 line-clamp-2">{run.error_message}</div>
      )}
      {actionError && <div className="mt-1.5 text-xs text-red-600">{actionError}</div>}

      <div className="mt-2 flex items-center gap-2">
        <Button variant="ghost" size="sm" className="text-xs h-7 px-2" onClick={toggleLogs}>
          {logsOpen ? (
            <ChevronDown className="w-3.5 h-3.5 mr-1" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 mr-1" />
          )}
          Logs
        </Button>

        {run.can_cancel && (
          <Button
            variant="outline"
            size="sm"
            className="text-xs h-7 px-2"
            disabled={busy !== null}
            onClick={handleCancel}
          >
            {busy === "cancel" ? (
              <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
            ) : (
              <XCircle className="w-3.5 h-3.5 mr-1" />
            )}
            Cancel
          </Button>
        )}

        {run.can_retry && (
          <Button
            variant="outline"
            size="sm"
            className="text-xs h-7 px-2"
            disabled={busy !== null}
            onClick={handleRetry}
          >
            {busy === "retry" ? (
              <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
            ) : (
              <RotateCcw className="w-3.5 h-3.5 mr-1" />
            )}
            Retry
          </Button>
        )}

        {onOpenArtifact && wizardArtifactTarget(run.target_ref_type, run.target_ref_id) && (
          <Button
            variant="ghost"
            size="sm"
            className="text-xs h-7 px-2"
            onClick={() =>
              onOpenArtifact(run.target_ref_type as string, run.target_ref_id as string)
            }
          >
            <ExternalLink className="w-3.5 h-3.5 mr-1" />
            Open
          </Button>
        )}

        {canDismissRow && (
          <Button
            variant="ghost"
            size="sm"
            className="text-xs h-7 px-2 text-gray-400 hover:text-red-600 ml-auto"
            disabled={busy !== null}
            onClick={handleDismiss}
          >
            Dismiss
          </Button>
        )}
      </div>

      {logsOpen && (
        <div className="mt-2 rounded bg-gray-950 text-gray-100 text-xs font-mono p-2 max-h-48 overflow-y-auto">
          {logLines.length === 0 ? (
            <div className="text-gray-500">No log output yet.</div>
          ) : (
            logLines.map((line) => (
              <div key={line.seq} className="whitespace-pre-wrap break-all">
                {line.level ? `[${line.level}] ` : ""}
                {line.message}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function areRowsEqual(prev: RowProps, next: RowProps): boolean {
  return (
    prev.run.id === next.run.id &&
    prev.run.status === next.run.status &&
    prev.run.progress === next.run.progress &&
    prev.run.updated_at === next.run.updated_at &&
    prev.run.can_cancel === next.run.can_cancel &&
    prev.run.can_retry === next.run.can_retry &&
    prev.run.error_message === next.run.error_message &&
    prev.run.target_ref_type === next.run.target_ref_type &&
    prev.run.target_ref_id === next.run.target_ref_id &&
    prev.run.retry_of_id === next.run.retry_of_id &&
    prev.run.originalRunAvailable === next.run.originalRunAvailable &&
    prev.run.highlighted === next.run.highlighted &&
    // Callbacks must be part of the comparison: a rerender that newly wires
    // onOpenArtifact (or drops it) changes whether the Open button renders.
    prev.onOpenRun === next.onOpenRun &&
    prev.onOpenArtifact === next.onOpenArtifact &&
    prev.onChanged === next.onChanged &&
    prev.onDismissed === next.onDismissed &&
    prev.onRetried === next.onRetried &&
    prev.apiUrl === next.apiUrl
  );
}

const WizardRunRow = React.memo(WizardRunRowInner, areRowsEqual);
export default WizardRunRow;
