"use client";

// Multi-run switcher ("run rail"): a compact dropdown of this workflow's recent
// + active runs. Picking one binds the canvas to it (live if running, final
// state if done) — Airflow-grid-style, but on your DAG. Self-polls the run
// history so active runs animate and new runs appear without a manual refresh.
import { useCallback, useEffect, useRef, useState } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import { Button } from "@/components/ui/button";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuLabel,
    DropdownMenuSeparator,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Loader2, CheckCircle2, XCircle, Clock, ChevronDown, ListChecks, Info } from "lucide-react";
import { DataLabelChip } from "@/components/data-label";

interface RunSummary {
    id: number;
    run_name?: string | null;
    status: string;
    paused?: boolean;
    paused_nodes?: string[];
    started_at?: string | null;
    completed_at?: string | null;
    version_number?: number | null;
    // GY.C7 — effective data-classification label (derived server-side from
    // the run's input cogs). Absent on legacy payloads → no chip.
    data_classification?: string | null;
}

interface RunRailProps {
    workflowId: number | null;
    accessToken: string | null;
    apiUrl: string;
    selectedRunId: number | null;
    onSelectRun: (runId: number) => void;
    // Syncs the selected run's per-node pause points to the canvas (from the DB),
    // even after switching runs or an external pause.
    onPausedNodesChange?: (nodes: string[]) => void;
    onExplainRun?: (runId: number) => void;
}

const ACTIVE = new Set(["running", "queued", "pending_action", "claimed", "assigned"]);
const SUCCESS = new Set(["success", "completed"]);
const FAIL = new Set(["failure", "failed"]);

function statusIcon(status: string) {
    const s = (status || "").toLowerCase();
    if (ACTIVE.has(s)) return <Loader2 className="h-3.5 w-3.5 text-green-500 animate-spin shrink-0" />;
    if (SUCCESS.has(s)) return <CheckCircle2 className="h-3.5 w-3.5 text-green-600 shrink-0" />;
    if (FAIL.has(s)) return <XCircle className="h-3.5 w-3.5 text-red-600 shrink-0" />;
    return <Clock className="h-3.5 w-3.5 text-gray-400 shrink-0" />;
}

function relTime(iso?: string | null): string {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

export default function RunRail({ workflowId, accessToken, apiUrl, selectedRunId, onSelectRun, onPausedNodesChange, onExplainRun }: RunRailProps) {
    const [runs, setRuns] = useState<RunSummary[]>([]);
    const [open, setOpen] = useState(false);
    // 2400UI126b: track which workflow id was 404'd so we stop polling for it
    // inside RunRail — no unwired callback needed.
    const [goneId, setGoneId] = useState<number | string | null>(null);

    // AbortController: cancel the in-flight runs fetch when workflowId changes
    // (e.g. the user deleted the current workflow) so the request never lands
    // and no 404 response can be observed. Replacing the signal alone never
    // aborts anything — the PREVIOUS controller must be .abort()ed.
    const abortRef = useRef<AbortController>(new AbortController());
    useEffect(() => {
        abortRef.current.abort();
        abortRef.current = new AbortController();
        // Unmount too: an in-flight fetch must not land after the component is gone.
        return () => abortRef.current.abort();
    }, [workflowId]);

    // Throws on failure (via pollErrorForResponse) so the shared poller can back
    // off instead of hammering the API — keeps the last list on a transient blip.
    const fetchRuns = useCallback(async () => {
        if (!workflowId || !accessToken) return;
        let res;
        try {
            res = await fetch(
                `${apiUrl}/road/${workflowId}/runs?limit=25&order=desc&sort_by=started_at`,
                { headers: { Authorization: `Bearer ${accessToken}` }, signal: abortRef.current.signal }
            );
        } catch (abortErr) {
            // Aborted because workflowId just changed (the workflow was deleted):
            // silent — no response was received, nothing to back off or report.
            if (abortErr instanceof DOMException && abortErr.name === "AbortError") return;
            throw abortErr;
        }
        const err = pollErrorForResponse(res);
        if (err) {
            if (res.status === 404) {
                setGoneId(workflowId);
                setRuns([]);
            }
            throw err;
        }
        const data = await res.json();
        if (Array.isArray(data)) setRuns(data);
    }, [workflowId, accessToken, apiUrl]);

    // Poll while there's a workflow: 4s base so active runs animate + new runs
    // appear — but through the shared helper (backoff on 5xx, stop-after-N with
    // the reconnect banner, pause when the tab is hidden, single-flight). The
    // helper fires an immediate tick on start, so no manual first fetch.
    usePollingBackoff(
        fetchRuns,
        { baseMs: 4000, enabled: !!(workflowId && accessToken && goneId !== workflowId) },
        [workflowId, accessToken]
    );

    // Refetch immediately when the menu opens (best-effort; poller owns errors).
    useEffect(() => {
        if (open) fetchRuns().catch(() => {});
    }, [open, fetchRuns]);

    // Sync the selected run's per-node pause points up to the canvas.
    useEffect(() => {
        if (!onPausedNodesChange || selectedRunId == null) return;
        const sel = runs.find((r) => r.id === selectedRunId);
        if (sel) onPausedNodesChange(sel.paused_nodes || []);
    }, [runs, selectedRunId, onPausedNodesChange]);

    if (!workflowId) return null;

    const selected = runs.find((r) => r.id === selectedRunId) || null;
    const activeCount = runs.filter((r) => ACTIVE.has((r.status || "").toLowerCase())).length;

    return (
        <DropdownMenu open={open} onOpenChange={setOpen}>
            <DropdownMenuTrigger asChild>
                <Button
                    size="sm"
                    variant="outline"
                    className="gap-1.5"
                    title="View and switch between runs of this workflow"
                >
                    {selected ? statusIcon(selected.status) : <ListChecks className="h-4 w-4" />}
                    {selected ? (
                        <span className="flex items-center gap-1 max-w-[200px]">
                            <span className="font-semibold shrink-0">#{selected.id}</span>
                            {selected.run_name && (
                                <span className="truncate text-muted-foreground">{selected.run_name}</span>
                            )}
                        </span>
                    ) : (
                        <span>Runs</span>
                    )}
                    {activeCount > 0 && (
                        <span className="ml-0.5 rounded-full bg-green-500/15 text-green-600 text-[10px] px-1.5 py-0.5 leading-none">
                            {activeCount} live
                        </span>
                    )}
                    <ChevronDown className="h-3.5 w-3.5 opacity-60" />
                </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-72">
                <DropdownMenuLabel className="flex items-center justify-between">
                    <span>Runs</span>
                    <span className="text-xs font-normal text-muted-foreground">
                        {runs.length ? `${runs.length} recent` : "none yet"}
                    </span>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {runs.length === 0 ? (
                    <div className="px-2 py-6 text-center text-sm text-muted-foreground">
                        No runs yet. Press Execute to start one.
                    </div>
                ) : (
                    <div className="max-h-80 overflow-y-auto">
                        {/* Native overflow container: a Radix ScrollArea with only
                            max-h didn't scroll (its viewport h-full resolves against
                            auto height), so 25 runs were cut off with no scrollbar. */}
                        {runs.map((r) => {
                            const isSel = r.id === selectedRunId;
                            return (
                                <DropdownMenuItem
                                    key={r.id}
                                    onSelect={(e) => {
                                        e.preventDefault();
                                        onSelectRun(r.id);
                                        setOpen(false);
                                    }}
                                    className={`flex items-center gap-2 ${isSel ? "bg-accent" : ""}`}
                                >
                                    {statusIcon(r.status)}
                                    <div className="min-w-0 flex-1">
                                        <div className="truncate text-sm">
                                            <span className="font-semibold">#{r.id}</span>
                                            {r.run_name ? <span className="text-muted-foreground"> · {r.run_name}</span>
                                                : (r.version_number != null ? <span className="text-muted-foreground"> · v{r.version_number}</span> : null)}
                                        </div>
                                        <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                                            <span>{(r.status || "").toLowerCase()} · {relTime(r.started_at)}</span>
                                            {/* GY.C7 — run-level data-label chip. */}
                                            <DataLabelChip label={r.data_classification} subtleDefault={false} />
                                        </div>
                                    </div>
                                    {isSel && (
                                        <span className="text-[10px] text-muted-foreground shrink-0">viewing</span>
                                    )}
                                    {onExplainRun && !ACTIVE.has((r.status || "").toLowerCase()) && (
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            className="h-5 w-5 shrink-0"
                                            aria-label={`Explain run ${r.id}`}
                                            onClick={(e) => {
                                                e.preventDefault();
                                                e.stopPropagation();
                                                onExplainRun(r.id);
                                            }}
                                        >
                                            <Info className="h-3.5 w-3.5" />
                                        </Button>
                                    )}
                                </DropdownMenuItem>
                            );
                        })}
                    </div>
                )}
            </DropdownMenuContent>
        </DropdownMenu>
    );
}
