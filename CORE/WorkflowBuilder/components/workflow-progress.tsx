"use client";
import { apiUrl } from "@/lib/runtime-config";

import React, { useState, useEffect, useCallback } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { CopyLogButton } from "@/components/ui/copy-log-button";
import { CheckCircle2, XCircle, Clock, Loader2 } from "lucide-react";
import { parseBlockedRunError } from "@/lib/blocked-run";
import BlockedRunPanel from "@/components/workflow/BlockedRunPanel";

interface WorkflowProgressProps {
  workflowRunId: number;
  accessToken: string | null;
  onComplete?: () => void;
  isAdmin?: boolean;
}

interface NodeStatus {
  node_id: string;
  node_name: string;
  node_kind: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  total_cost: number;
  files_processed: number;
  errors: any[];
  // AI scheduler — per-node ETA stamped at run creation; populated by the
  // /node-status endpoint. `actual_seconds` becomes non-null on completion
  // so the UI can show predicted-vs-actual delta.
  estimated_seconds?: number | null;
  actual_seconds?: number | null;
  node_run_status?: string | null;
}

interface ProgressData {
  workflow_run_id: number;
  status: string;
  progress_percentage: number;
  completed_nodes: number;
  total_nodes: number;
  current_node: string | null;
  total_cost: number;
  started_at: string;
  estimated_seconds_remaining: number | null;
  // AI scheduler — predictor-driven critical-path total when available;
  // null for legacy runs predating Bucket D. eta_source labels which
  // estimator produced the value: "predicted" (ML), "elapsed_extrapolation"
  // (linear from progress so far), or null.
  estimated_total_seconds?: number | null;
  eta_source?: string | null;
  // Populated by the orchestrator when a run fails before any node
  // starts (pending_scan blocker, missing secret, circular dep, etc).
  // API returns it from /road/workflow-runs/{id}/progress.
  error_message?: string | null;
}

export function WorkflowProgress({ workflowRunId, accessToken, onComplete, isAdmin = false }: WorkflowProgressProps) {
  const [progress, setProgress] = useState<ProgressData | null>(null);
  const [nodeStatus, setNodeStatus] = useState<NodeStatus[]>([]);
  const [isPolling, setIsPolling] = useState(true);

  const pollProgress = useCallback(async () => {
    if (!workflowRunId || !accessToken) return;
    const API_URL = apiUrl();

    // Fetch progress (primary) — throw on transient failure so the shared poller
    // backs off instead of hammering a struggling API.
    const progressResponse = await fetch(
      `${API_URL}/road/workflow-runs/${workflowRunId}/progress`,
      { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } }
    );
    const perr = pollErrorForResponse(progressResponse);
    if (perr) throw perr;

    const progressData = await progressResponse.json();
    setProgress(progressData);
    // Stop polling if completed or failed (flips `enabled` false → poller stops).
    if (progressData.status === "success" || progressData.status === "failure") {
      setIsPolling(false);
      if (onComplete) onComplete();
    }

    // Fetch node status (secondary) — best-effort; do not storm on its failure.
    const nodeResponse = await fetch(
      `${API_URL}/road/workflow-runs/${workflowRunId}/node-status`,
      { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } }
    );
    if (nodeResponse.ok) {
      const nodeData = await nodeResponse.json();
      setNodeStatus(nodeData.nodes || []);
    }
  }, [workflowRunId, accessToken, onComplete]);

  // Poll workflow progress (3s base) through the shared helper (backoff / stop /
  // pause / single-flight). Stops when the run reaches a terminal state.
  usePollingBackoff(
    pollProgress,
    { baseMs: 3000, enabled: !!(workflowRunId && accessToken && isPolling) },
    [workflowRunId, accessToken, isPolling]
  );

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "success":
        return <CheckCircle2 className="h-4 w-4 text-green-600" />;
      case "failure":
        return <XCircle className="h-4 w-4 text-red-600" />;
      case "running":
        return <Loader2 className="h-4 w-4 text-green-500 animate-spin" />;
      default:
        return <Clock className="h-4 w-4 text-gray-400" />;
    }
  };

  const getStatusBadge = (status: string) => {
    const variants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
      success: "default",
      failure: "destructive",
      running: "secondary",
      pending: "outline",
    };

    return (
      <Badge variant={variants[status] || "outline"} className="text-xs">
        {status}
      </Badge>
    );
  };

  const formatTime = (seconds: number | null) => {
    if (seconds === null) return "Calculating...";

    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);

    if (mins > 0) {
      return `${mins}m ${secs}s`;
    }
    return `${secs}s`;
  };

  if (!progress) {
    return (
      <Card className="w-full">
        <CardContent className="flex items-center justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
          <span className="ml-2 text-sm text-gray-600">Loading workflow progress...</span>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* Overall Progress */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <span>Workflow Execution Progress</span>
            {getStatusBadge(progress.status)}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">
                {progress.completed_nodes} / {progress.total_nodes} nodes completed
              </span>
              <span className="font-semibold">{progress.progress_percentage}%</span>
            </div>
            <Progress value={progress.progress_percentage} className="h-2" />
          </div>

          {progress.current_node && (
            <div className="flex items-center gap-2 text-sm">
              <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
              <span className="text-gray-600">
                Currently processing: <span className="font-semibold">{progress.current_node}</span>
              </span>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4 pt-2 border-t">
            <div>
              <div className="text-xs text-gray-500">Total Cost</div>
              <div className="text-lg font-semibold">{progress.total_cost} credits</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">
                Est. Time Remaining
                {progress.eta_source === "predicted" && (
                  <span className="ml-1 text-xs text-blue-600">(ML)</span>
                )}
                {progress.eta_source === "elapsed_extrapolation" && (
                  <span className="ml-1 text-xs text-gray-400">(from progress)</span>
                )}
              </div>
              <div className="text-lg font-semibold">
                {formatTime(progress.estimated_seconds_remaining)}
              </div>
            </div>
          </div>

          {/* Orchestrator-level failure reason. Surfaces when the run
              never made it past dispatch — pending_scan node blocking
              it, missing secret, circular dep, etc. Matches the dash's
              Failure Reason panel so operators see the real cause
              regardless of which UI they're looking at. */}
          {progress.error_message && String(progress.status).toLowerCase() === 'failed' && (
            <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded-lg">
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2 font-semibold text-red-800">
                  <XCircle className="h-4 w-4" />
                  <span>Failure reason</span>
                </div>
                <CopyLogButton
                  getText={() => progress.error_message || ""}
                  title="Copy the failure reason to the clipboard"
                />
              </div>
              <div className="text-sm text-red-900 font-mono whitespace-pre-wrap break-words">
                {progress.error_message}
              </div>
            </div>
          )}

          {/* GG.UI.6 — the failure was a paid-item entitlement refusal:
              offer Buy / Try / Ask-admin CTAs to the store instead of only
              the raw error string. */}
          {String(progress.status).toLowerCase() === 'failed' && (
            <BlockedRunPanel
              reasons={
                progress.error_message
                  ? [parseBlockedRunError(progress.error_message)].filter(
                      (r): r is NonNullable<ReturnType<typeof parseBlockedRunError>> => r !== null,
                    )
                  : []
              }
              isAdmin={isAdmin}
            />
          )}
        </CardContent>
      </Card>

      {/* Node Status */}
      {nodeStatus.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Node Execution Status</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {nodeStatus.map((node) => (
                <div
                  key={node.node_id}
                  className="flex items-center justify-between p-3 border rounded-lg hover:bg-gray-50"
                >
                  <div className="flex items-center gap-3 flex-1">
                    {getStatusIcon(node.status)}
                    <div className="flex-1">
                      <div className="font-medium text-sm">{node.node_name}</div>
                      <div className="text-xs text-gray-500">{node.node_kind}</div>
                    </div>
                  </div>

                  <div className="flex items-center gap-4">
                    <div className="text-right">
                      <div className="text-xs text-gray-500">Files Processed</div>
                      <div className="text-sm font-semibold">{node.files_processed}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-xs text-gray-500">Cost</div>
                      <div className="text-sm font-semibold">{node.total_cost} credits</div>
                    </div>
                    {/* AI scheduler — per-node ETA badge. Pre-completion shows
                        the predicted duration; on completion shows the actual
                        and a delta vs prediction (green when faster, amber when
                        slower than predicted by ≥20%). */}
                    {(node.estimated_seconds != null || node.actual_seconds != null) && (
                      <div className="text-right">
                        <div className="text-xs text-gray-500">
                          {node.actual_seconds != null ? "Actual / Est." : "ETA"}
                        </div>
                        <div className="text-sm font-semibold">
                          {node.actual_seconds != null ? (
                            <>
                              <span>{formatTime(node.actual_seconds)}</span>
                              {node.estimated_seconds != null && (() => {
                                const delta = node.actual_seconds! - node.estimated_seconds;
                                const pct = node.estimated_seconds > 0
                                  ? Math.abs(delta) / node.estimated_seconds
                                  : 0;
                                const colour = delta < 0
                                  ? "text-green-600"
                                  : pct >= 0.2
                                    ? "text-amber-600"
                                    : "text-gray-500";
                                const sign = delta >= 0 ? "+" : "";
                                return (
                                  <span className={`ml-1 text-xs ${colour}`}>
                                    ({sign}{Math.round(delta)}s vs {formatTime(node.estimated_seconds)})
                                  </span>
                                );
                              })()}
                            </>
                          ) : (
                            formatTime(node.estimated_seconds ?? null)
                          )}
                        </div>
                      </div>
                    )}
                    {getStatusBadge(node.status)}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
