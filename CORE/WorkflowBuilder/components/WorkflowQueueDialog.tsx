"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

import React, { useState, useEffect, useCallback } from "react";
import { getPlatformFeatures } from "@/lib/platform-features";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight, Eye, FileText, Loader2, XCircle, RotateCcw, Activity, Copy, Check } from "lucide-react";


interface RunFile {
    id: number;
    file_id: string;
    title: string;
    status: string | null;
}

interface WorkflowRun {
    id: number;
    run_name: string | null;
    status: 'completed' | 'failed' | 'running' | 'queued';
    started_at: string | null;
    completed_at: string | null;
    workflow_id: number | null;
    workflow_version_id: number | null;
    workflow_name: string | null;
    version_number: number | null;
    orchestrator_run_id: string | null;
    // Gate 7 — what the run cost to produce and what it bills at, from real
    // task durations. Null while running, or when the workers that ran it
    // carry no cost — which must read as unknown, never as free.
    cost_cents: number | null;
    billed_cents: number | null;
    run_files: RunFile[];
}

/** What the run billed, with its cost underneath.
 *
 *  Both numbers, because the billed figure alone cannot answer whether the
 *  work was profitable — which is the question the costing exists for. A run
 *  that has not been costed says so: rendering an uncosted run as "$0.00"
 *  would read as free, and that error only ever goes one direction.
 */
function renderRunCost(run: WorkflowRun): React.JSX.Element {
    if (run.billed_cents === null || run.billed_cents === undefined) {
        const pending = run.status === 'running' || run.status === 'queued';
        return <span className="text-gray-400 text-xs">{pending ? '—' : 'not costed'}</span>;
    }
    const billed = (run.billed_cents / 100).toFixed(2);
    const cost = run.cost_cents !== null && run.cost_cents !== undefined
        ? (run.cost_cents / 100).toFixed(2)
        : null;
    return (
        <span title={cost !== null ? `Cost $${cost} to run` : undefined}>
            <span className="font-medium">${billed}</span>
            {cost !== null && (
                <span className="text-[11px] text-gray-400 ml-1">/ ${cost} cost</span>
            )}
        </span>
    );
}

/** What a run used (30.6.15 / cUI-3). Mirrors CORE/API/schemas.py:676-694
 *  `RunUsage`. The serializer there (schemas.py:689-694) DROPS keys whose
 *  value is None, so every nullable key is optional here and a missing key
 *  reads exactly like a null. `models` (c11-J5, run_result_service.py:257)
 *  is per-model detail, not run-level token totals, so this mirror carries
 *  no token row — see the Result. */
interface RunUsage {
    wall_seconds?: number | null;
    compute_seconds: number;
    nodes: number;
    final: boolean;
    credits_charged?: number | null;
    cost?: { amount: string; currency: string } | null;
}

/** GET /v1/runs/{run_id}/result (CORE/API/routers/node_runs_domains/
 *  node_runs.py:137). `hidden` = 404 (block hidden), `unavailable` = fetch
 *  failed or 4xx/5xx (block says so, dialog still works). */
interface RunUsageFetchResult {
    kind: 'ok' | 'hidden' | 'unavailable';
    usage: RunUsage | null;
}

interface UsageEntry {
    result: RunUsageFetchResult;
    terminal: boolean;
    fetches: number;
}

/** Seconds → the file's existing duration formatter (WorkflowQueueDialog.tsx:96
 *  `formatDuration`, which takes start/end ISO strings). Anchoring both ends
 *  on the epoch turns a duration in seconds into the same "12s / 3m 20s / 2h
 *  5m" shape the Duration column already uses. */
const formatUsageDuration = (seconds: number): string =>
    formatDuration(new Date(0).toISOString(), new Date(seconds * 1000).toISOString());

/** One fetch of the run's usage. Never throws, never logs; the caller
 *  renders whatever it gets back. URL built per request from apiUrl()
 *  (lib/runtime-config.ts:98) — no module-scope URL. */
export async function fetchRunUsage(runId: number, accessToken: string): Promise<RunUsageFetchResult> {
    try {
        const response = await fetch(
            `${apiUrl()}/runs/${runId}/result`,
            {
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    Accept: 'application/json',
                },
            }
        );
        if (response.status === 404) return { kind: 'hidden', usage: null };
        if (!response.ok) return { kind: 'unavailable', usage: null };

        const data = await response.json();
        const raw = (data as { usage?: unknown }).usage as Record<string, unknown> | null;
        if (!raw || typeof raw !== 'object') return { kind: 'unavailable', usage: null };

        const num = (v: unknown): number | null =>
            typeof v === 'number' && Number.isFinite(v) ? v : null;

        const credits = num(raw.credits_charged);
        const costRaw = raw.cost;
        const cost = costRaw && typeof costRaw === 'object'
            && typeof (costRaw as Record<string, unknown>).amount === 'string'
            && typeof (costRaw as Record<string, unknown>).currency === 'string'
            ? {
                amount: (costRaw as Record<string, unknown>).amount as string,
                currency: (costRaw as Record<string, unknown>).currency as string,
            }
            : null;

        return {
            kind: 'ok',
            usage: {
                compute_seconds: num(raw.compute_seconds) ?? 0,
                nodes: num(raw.nodes) ?? 0,
                final: raw.final === true,
                ...(raw.wall_seconds !== undefined && raw.wall_seconds !== null
                    ? { wall_seconds: num(raw.wall_seconds) }
                    : {}),
                ...(credits !== null ? { credits_charged: credits } : {}),
                ...(cost ? { cost } : {}),
            },
        };
    } catch {
        return { kind: 'unavailable', usage: null };
    }
}

/** The "Usage" block (data-testid="run-usage"). Money rows appear ONLY when
 *  billing is on AND the key is present; in CE no money words render at all.
 *  `cost.amount` is printed verbatim — no parse, no rounding. */
export function RunUsageBlock({
    entry,
    billingEnabled,
}: {
    entry: UsageEntry | undefined;
    billingEnabled: boolean;
}): React.JSX.Element | null {
    if (!entry || entry.result.kind === 'hidden') return null;

    if (entry.result.kind === 'unavailable' || !entry.result.usage) {
        return (
            <div data-testid="run-usage" className="text-xs text-gray-500">
                Usage unavailable
            </div>
        );
    }

    const usage = entry.result.usage;
    const lines: string[] = [
        `Compute time ${formatUsageDuration(usage.compute_seconds)}`,
    ];
    if (usage.wall_seconds !== undefined && usage.wall_seconds !== null) {
        lines.push(`Wall time ${formatUsageDuration(usage.wall_seconds)}`);
    }
    lines.push(`Nodes run ${usage.nodes}`);
    if (billingEnabled && usage.credits_charged !== undefined && usage.credits_charged !== null) {
        lines.push(`Credits ${usage.credits_charged}`);
    }
    if (billingEnabled && usage.cost) {
        lines.push(`Cost ${usage.cost.amount} ${usage.cost.currency}`);
    }
    if (!usage.final) lines.push('(running, partial)');

    return (
        <div data-testid="run-usage" className="text-xs text-gray-600">
            {lines.map((line) => (
                <div key={line} className="text-xs">
                    {line}
                </div>
            ))}
        </div>
    );
}

interface WorkflowQueueDialogProps {
    isOpen: boolean;
    onClose: () => void;
    accessToken: string | null;
    /** Click "Watch" on a live (running/queued) run: parent loads the
     *  run's workflow + version onto the canvas and pins the canvas
     *  poll-overlay to this run id so the user can watch it execute
     *  in place. Hidden when null/undefined. */
    onWatch?: (workflowId: number, versionId: number, runId: number) => void;
}

// Status badge colors matching Dashboard
const getStatusBadge = (status: string) => {
    const statusColors: Record<string, string> = {
        completed: 'bg-green-100 text-green-800',
        running: 'bg-blue-100 text-blue-800',
        failed: 'bg-red-100 text-red-800',
        queued: 'bg-yellow-100 text-yellow-800',
    };
    const colorClass = statusColors[status] || 'bg-gray-100 text-gray-800';
    return (
        <span className={`px-2 py-1 text-xs font-medium rounded-full ${colorClass}`}>
            {status}
        </span>
    );
};

// Format duration
const formatDuration = (startDate: string | null, endDate: string | null): string => {
    if (!startDate) return '-';
    const start = new Date(startDate);
    const end = endDate ? new Date(endDate) : new Date();
    const diffMs = end.getTime() - start.getTime();
    const diffSeconds = Math.floor(diffMs / 1000);

    if (diffSeconds < 60) return `${diffSeconds}s`;
    const minutes = Math.floor(diffSeconds / 60);
    const seconds = diffSeconds % 60;
    if (minutes < 60) return `${minutes}m ${seconds}s`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return `${hours}h ${mins}m`;
};

// Format date for display
const formatDate = (dateStr: string | null): string => {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString();
};

export const WorkflowQueueDialog = ({
    isOpen,
    onClose,
    accessToken,
    onWatch,
}: WorkflowQueueDialogProps) => {
    const [runs, setRuns] = useState<WorkflowRun[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [page, setPage] = useState(0);
    const [hasMore, setHasMore] = useState(false);
    const [cancellingRuns, setCancellingRuns] = useState<Set<number>>(new Set());
    const [restartingRuns, setRestartingRuns] = useState<Set<number>>(new Set());

    // Logs viewer state
    const [selectedRun, setSelectedRun] = useState<WorkflowRun | null>(null);
    const [logs, setLogs] = useState<string>('');
    const [isLoadingLogs, setIsLoadingLogs] = useState(false);
    const [logsError, setLogsError] = useState<string | null>(null);
    // Copy-logs button state (Fix 26). Brief "Copied" confirmation.
    const [logsCopied, setLogsCopied] = useState(false);

    // 30.6.15 (cUI-3) — what the run used. One entry per run id; `terminal`
    // records whether that entry was fetched at a terminal status, so the
    // second (final) fetch happens exactly once when the run settles.
    const [usageEntries, setUsageEntries] = useState<Record<number, UsageEntry>>({});
    // CE switch: lib/platform-features.ts:24 `billing_enabled`, read through
    // getPlatformFeatures(). False (the default) means no money rows at all.
    const [billingEnabled, setBillingEnabled] = useState(false);

    const isTerminalStatus = (status: string): boolean =>
        status === 'completed' || status === 'failed';

    const loadUsage = async (runId: number, terminal: boolean) => {
        if (!accessToken) return;
        const prior = usageEntries[runId];
        if (prior && prior.terminal === terminal) return;
        const result = await fetchRunUsage(runId, accessToken);
        setUsageEntries((prev) => {
            const existing = prev[runId];
            if (existing && existing.terminal === terminal) return prev;
            const next: Record<number, UsageEntry> = { ...prev };
            next[runId] = {
                result,
                terminal,
                fetches: (existing?.fetches ?? 0) + 1,
            };
            return next;
        });
    };

    const refreshBilling = () => {
        try {
            getPlatformFeatures()
                .then((features) => setBillingEnabled(features?.billing_enabled === true))
                .catch(() => setBillingEnabled(false));
        } catch {
            setBillingEnabled(false);
        }
    };

    // Copy the full log/progress text to the clipboard. Uses the async
    // Clipboard API when available, with a legacy execCommand fallback for
    // insecure-origin / older-browser cases so the button never no-ops.
    const copyLogs = useCallback(async () => {
        const text = logs || '';
        if (!text) return;
        try {
            if (navigator?.clipboard?.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            setLogsCopied(true);
            window.setTimeout(() => setLogsCopied(false), 1500);
        } catch {
            /* clipboard blocked — leave the button unchanged */
        }
    }, [logs]);

    const ITEMS_PER_PAGE = 10;

    // Fetch workflow runs from our API
    const fetchRuns = useCallback(async () => {
        if (!accessToken) return;

        setIsLoading(true);
        try {
            const offset = page * ITEMS_PER_PAGE;
            const response = await fetch(
                `${apiUrl()}/road/runs?limit=${ITEMS_PER_PAGE + 1}&offset=${offset}`,
                {
                    headers: {
                        Authorization: `Bearer ${accessToken}`,
                        Accept: 'application/json',
                    },
                }
            );

            if (!response.ok) throw new Error('Failed to fetch workflow runs');

            const data: WorkflowRun[] = await response.json();

            // Check if there are more pages
            setHasMore(data.length > ITEMS_PER_PAGE);
            setRuns(data.slice(0, ITEMS_PER_PAGE));
        } catch (error) {
            console.error('Error fetching workflow runs:', error);
            setRuns([]);
        } finally {
            setIsLoading(false);
        }
    }, [accessToken, page]);

    // Cancel a workflow run (cancels pending/running ApiTaskQueue tasks and marks the run FAILED)
    const cancelWorkflow = async (run: WorkflowRun, e?: React.MouseEvent) => {
        if (e) {
            e.stopPropagation();
        }
        if (!accessToken) return;

        setCancellingRuns(prev => new Set(prev).add(run.id));

        try {
            // Call the cancel endpoint which cancels active ApiTaskQueue tasks and updates our DB
            const response = await fetch(
                `${apiUrl()}/road/workflow-runs/${run.id}/cancel`,
                {
                    method: 'POST',
                    headers: {
                        Authorization: `Bearer ${accessToken}`,
                    },
                }
            );

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || 'Failed to cancel workflow');
            }

            // Refresh the runs list to show updated status
            await fetchRuns();
        } catch (error) {
            console.error('Error cancelling workflow:', error);
        } finally {
            setCancellingRuns(prev => {
                const newSet = new Set(prev);
                newSet.delete(run.id);
                return newSet;
            });
        }
    };

    // Check if a run can be cancelled (only running or queued)
    const canCancel = (status: string) => {
        return status === 'running' || status === 'queued';
    };

    // Check if a run can be restarted (failed or completed, not running/queued)
    const canRestart = (status: string) => {
        return status === 'failed' || status === 'completed';
    };

    // Restart a workflow run (creates a new run with same parameters)
    // resumeMode kept on the API surface; the only caller now is the
    // Restart button which always passes 'full'.
    const restartWorkflow = async (run: WorkflowRun, resumeMode: 'full' | 'failed_only' = 'full', e?: React.MouseEvent) => {
        if (e) {
            e.stopPropagation();
        }
        if (!accessToken) return;

        setRestartingRuns(prev => new Set(prev).add(run.id));

        try {
            const response = await fetch(
                `${apiUrl()}/road/workflow-runs/${run.id}/restart`,
                {
                    method: 'POST',
                    headers: {
                        Authorization: `Bearer ${accessToken}`,
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ resume_mode: resumeMode }),
                }
            );

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || 'Failed to restart workflow');
            }

            // Refresh the runs list to show the new run
            await fetchRuns();
        } catch (error) {
            console.error('Error restarting workflow:', error);
            // Could add toast notification here
        } finally {
            setRestartingRuns(prev => {
                const newSet = new Set(prev);
                newSet.delete(run.id);
                return newSet;
            });
        }
    };

    // Fetch logs for a workflow run
    const fetchLogs = async (run: WorkflowRun) => {
        if (!accessToken) return;

        setIsLoadingLogs(true);
        setLogsError(null);

        try {
            // Use the progress endpoint to get run details/logs
            const response = await fetch(
                `${apiUrl()}/road/workflow-runs/${run.id}/progress`,
                {
                    headers: {
                        Authorization: `Bearer ${accessToken}`,
                        Accept: 'application/json',
                    },
                }
            );

            if (!response.ok) {
                if (response.status === 404) {
                    setLogs('No progress information available yet.');
                } else {
                    throw new Error('Failed to fetch progress');
                }
            } else {
                const data = await response.json();
                // Format progress as logs
                const logLines = [
                    `Workflow: ${run.run_name || run.workflow_name || 'Unknown'}`,
                    `Status: ${run.status}`,
                    `Started: ${formatDate(run.started_at)}`,
                    run.completed_at ? `Completed: ${formatDate(run.completed_at)}` : '',
                    '',
                    '--- Files ---',
                    ...run.run_files.map(f => `${f.title}: ${f.status || 'pending'}`),
                    '',
                    '--- Progress ---',
                    JSON.stringify(data, null, 2)
                ].filter(Boolean);
                setLogs(logLines.join('\n'));
            }
        } catch (error) {
            console.error('Error fetching logs:', error);
            setLogsError('Failed to load progress. Please try again.');
            setLogs('');
        } finally {
            setIsLoadingLogs(false);
        }
    };

    // Auto-refresh runs when dialog is open
    useEffect(() => {
        if (isOpen && accessToken) {
            fetchRuns();

            // Poll every 5 seconds
            const interval = setInterval(fetchRuns, 5000);
            return () => clearInterval(interval);
        }
    }, [isOpen, accessToken, fetchRuns]);

    // Fetch logs when run is selected
    useEffect(() => {
        if (selectedRun) {
            fetchLogs(selectedRun);
        }
    }, [selectedRun]);

    // 30.6.15 (cUI-3) — usage for the open run. No poll loop of my own: this
    // re-runs only when the selected run (or its status) changes, i.e. once
    // when the dialog opens and once more when the dialog's existing 5 s
    // refresh tick (WorkflowQueueDialog.tsx:349 setInterval(fetchRuns, 5000))
    // flips the run to a terminal status.
    useEffect(() => {
        if (selectedRun && accessToken) {
            void loadUsage(selectedRun.id, isTerminalStatus(selectedRun.status));
        }
    }, [selectedRun, accessToken]);

    // CE/SaaS money switch (lib/platform-features.ts:24 billing_enabled).
    useEffect(() => {
        refreshBilling();
    }, []);

    // Handle view logs click
    const handleViewLogs = (run: WorkflowRun) => {
        setSelectedRun(run);
        setLogs('');
        setLogsError(null);
    };

    // Handle back to list
    const handleBackToList = () => {
        setSelectedRun(null);
        setLogs('');
        setLogsError(null);
    };

    // Reset page when dialog closes
    useEffect(() => {
        if (!isOpen) {
            setPage(0);
            setSelectedRun(null);
            setLogs('');
        }
    }, [isOpen]);

    // Get display name for workflow run
    const getRunDisplayName = (run: WorkflowRun): string => {
        if (run.run_name) return run.run_name;
        if (run.workflow_name) {
            return `${run.workflow_name}${run.version_number ? ` v${run.version_number}` : ''}`;
        }
        return `Run #${run.id}`;
    };

    return (
        <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
            <DialogContent className="max-w-7xl w-[95vw] max-h-[85vh] flex flex-col">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        {selectedRun ? (
                            <>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={handleBackToList}
                                    aria-label="Back to run list"
                                    className="p-1 h-auto"
                                >
                                    <ChevronLeft className="h-4 w-4" />
                                </Button>
                                Progress: {getRunDisplayName(selectedRun)}
                            </>
                        ) : (
                            <>
                                <Activity className="h-5 w-5" aria-hidden="true" />
                                {wbNavLabel('wb-nav-process-queue', 'Process Queue')}
                            </>
                        )}
                    </DialogTitle>
                    <DialogDescription>
                        {selectedRun
                            ? `Run ID: ${selectedRun.id}`
                            : 'View recent workflow runs and their status.'
                        }
                    </DialogDescription>
                </DialogHeader>

                <div className="flex-1 overflow-hidden flex flex-col min-h-0">
                    {selectedRun ? (
                        // Logs/Progress View
                        <div className="flex flex-col h-full min-h-0">
                            {/* 30.6.15 (cUI-3) — what the run used, between the
                                status row (DialogHeader, Run ID) and the Files
                                list. Hidden on 404; says "Usage unavailable" on
                                any other fetch failure. */}
                            {RunUsageBlock({
                                entry: usageEntries[selectedRun.id],
                                billingEnabled,
                            })}

                            {/* Files list */}
                            {selectedRun.run_files.length > 0 && (
                                <div className="mb-3">
                                    <label className="text-sm font-medium">Files ({selectedRun.run_files.length}):</label>
                                    <div className="flex flex-wrap gap-1 mt-1">
                                        {selectedRun.run_files.slice(0, 5).map((file) => (
                                            <span key={file.id} className="text-xs bg-gray-100 px-2 py-1 rounded">
                                                {file.title}
                                            </span>
                                        ))}
                                        {selectedRun.run_files.length > 5 && (
                                            <span className="text-xs text-gray-500">
                                                +{selectedRun.run_files.length - 5} more
                                            </span>
                                        )}
                                    </div>
                                </div>
                            )}

                            {/* Log toolbar — Copy the full progress/log text
                                to the clipboard (Fix 26 / Fix A). A labelled,
                                solid, always-visible button so it can't be
                                missed the way the earlier small greyed outline
                                one was. Rendered whenever the log view is open. */}
                            <div className="flex items-center justify-between mb-2 flex-shrink-0 border-b pb-2">
                                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    Progress log
                                </span>
                                <Button
                                    variant="default"
                                    size="sm"
                                    onClick={copyLogs}
                                    disabled={!logs || isLoadingLogs}
                                    className="h-8"
                                    title="Copy the full log to the clipboard"
                                >
                                    {logsCopied ? (
                                        <>
                                            <Check className="h-4 w-4 mr-1" />
                                            Copied
                                        </>
                                    ) : (
                                        <>
                                            <Copy className="h-4 w-4 mr-1" />
                                            Copy log
                                        </>
                                    )}
                                </Button>
                            </div>

                            {/* Progress content */}
                            <div className="flex-1 min-h-0 bg-gray-900 rounded-lg overflow-auto">
                                {isLoadingLogs ? (
                                    <div className="flex items-center justify-center h-full text-gray-400">
                                        <Loader2 className="h-6 w-6 animate-spin mr-2" />
                                        Loading progress...
                                    </div>
                                ) : logsError ? (
                                    <div className="flex items-center justify-center h-full text-red-400">
                                        {logsError}
                                    </div>
                                ) : (
                                    <pre className="p-4 text-green-400 font-mono text-xs whitespace-pre-wrap">
                                        {logs}
                                    </pre>
                                )}
                            </div>
                        </div>
                    ) : (
                        // Runs List View
                        <div className="flex flex-col h-full min-h-0">
                            {isLoading && runs.length === 0 ? (
                                <div className="flex items-center justify-center h-40 text-gray-500">
                                    <Loader2 className="h-6 w-6 animate-spin mr-2" />
                                    Loading...
                                </div>
                            ) : runs.length === 0 ? (
                                <div className="flex items-center justify-center h-40 text-gray-500">
                                    No workflow runs found
                                </div>
                            ) : (
                                <>
                                    {/* Table */}
                                    <div className="flex-1 overflow-auto min-h-0">
                                        <table className="w-full text-sm">
                                            <thead className="bg-gray-50 sticky top-0">
                                                <tr>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Workflow</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Status</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Started</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Duration</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Cost</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Files</th>
                                                    <th className="px-4 py-2 text-left font-medium text-gray-600">Actions</th>
                                                </tr>
                                            </thead>
                                            <tbody className="divide-y divide-gray-200">
                                                {runs.map((run) => {
                                                    const displayName = getRunDisplayName(run);

                                                    return (
                                                        <tr
                                                            key={run.id}
                                                            className="hover:bg-gray-50"
                                                        >
                                                            <td className="px-4 py-3 font-medium">
                                                                {displayName}
                                                            </td>
                                                            <td className="px-4 py-3">
                                                                {getStatusBadge(run.status)}
                                                            </td>
                                                            <td className="px-4 py-3 text-gray-600">
                                                                {formatDate(run.started_at)}
                                                            </td>
                                                            <td className="px-4 py-3 text-gray-600">
                                                                {formatDuration(run.started_at, run.completed_at)}
                                                            </td>
                                                            <td className="px-4 py-3 text-gray-600">
                                                                {renderRunCost(run)}
                                                            </td>
                                                            <td className="px-4 py-3 text-gray-600">
                                                                {run.run_files.length}
                                                            </td>
                                                            <td className="px-4 py-3">
                                                                <div className="flex gap-1">
                                                                    <Button
                                                                        variant="outline"
                                                                        size="sm"
                                                                        onClick={() => handleViewLogs(run)}
                                                                        className="h-7 text-xs"
                                                                    >
                                                                        <FileText className="h-3 w-3 mr-1" />
                                                                        Progress
                                                                    </Button>
                                                                    {/* LDF-4 #5 — Watch on canvas. Only live runs
                                                                        with a known workflow + version can be watched
                                                                        (we need both to load the right graph). */}
                                                                    {onWatch
                                                                        && (run.status === 'running' || run.status === 'queued')
                                                                        && run.workflow_id != null
                                                                        && run.workflow_version_id != null && (
                                                                        <Button
                                                                            variant="outline"
                                                                            size="sm"
                                                                            onClick={(e) => {
                                                                                e.stopPropagation();
                                                                                onClose();
                                                                                onWatch(
                                                                                    run.workflow_id as number,
                                                                                    run.workflow_version_id as number,
                                                                                    run.id,
                                                                                );
                                                                            }}
                                                                            className="h-7 text-xs"
                                                                            title="Open this run on the canvas and watch it execute live"
                                                                        >
                                                                            <Eye className="h-3 w-3 mr-1" />
                                                                            Watch
                                                                        </Button>
                                                                    )}
                                                                    {canCancel(run.status) && (
                                                                        <Button
                                                                            variant="destructive"
                                                                            size="sm"
                                                                            onClick={(e) => cancelWorkflow(run, e)}
                                                                            disabled={cancellingRuns.has(run.id)}
                                                                            className="h-7 text-xs"
                                                                        >
                                                                            {cancellingRuns.has(run.id) ? (
                                                                                <Loader2 className="h-3 w-3 animate-spin" />
                                                                            ) : (
                                                                                <>
                                                                                    <XCircle className="h-3 w-3 mr-1" />
                                                                                    Cancel
                                                                                </>
                                                                            )}
                                                                        </Button>
                                                                    )}
                                                                    {canRestart(run.status) && (
                                                                        <Button
                                                                            variant="outline"
                                                                            size="sm"
                                                                            onClick={(e) => restartWorkflow(run, 'full', e)}
                                                                            disabled={restartingRuns.has(run.id)}
                                                                            className="h-7 text-xs"
                                                                            title="Restart: Reprocess all files from scratch"
                                                                        >
                                                                            {restartingRuns.has(run.id) ? (
                                                                                <Loader2 className="h-3 w-3 animate-spin" />
                                                                            ) : (
                                                                                <>
                                                                                    <RotateCcw className="h-3 w-3 mr-1" />
                                                                                    Restart
                                                                                </>
                                                                            )}
                                                                        </Button>
                                                                    )}
                                                                </div>
                                                            </td>
                                                        </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>

                                    {/* Pagination */}
                                    <div className="flex items-center justify-between pt-4 border-t mt-4">
                                        <div className="text-sm text-gray-600">
                                            Page {page + 1}
                                            {isLoading && <Loader2 className="h-3 w-3 animate-spin inline ml-2" />}
                                        </div>
                                        <div className="flex gap-2">
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={() => setPage(p => Math.max(0, p - 1))}
                                                disabled={page === 0}
                                            >
                                                <ChevronLeft className="h-4 w-4 mr-1" />
                                                Previous
                                            </Button>
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={() => setPage(p => p + 1)}
                                                disabled={!hasMore}
                                            >
                                                Next
                                                <ChevronRight className="h-4 w-4 ml-1" />
                                            </Button>
                                        </div>
                                    </div>
                                </>
                            )}
                        </div>
                    )}
                </div>
            </DialogContent>
        </Dialog>
    );
};
