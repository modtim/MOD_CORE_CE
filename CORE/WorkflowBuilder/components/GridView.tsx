"use client";

// Airflow-style grid view: rows = workflow nodes, columns = recent runs, each
// cell colored by that node's status in that run. Click a run column to bind the
// canvas to it. Self-polls so active runs animate + new runs appear.
import { useCallback, useEffect, useState } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { LayoutGrid, Loader2 } from "lucide-react";

interface GridNode { graph_node_id: string; name: string; }
interface GridRun {
    id: number;
    run_name?: string | null;
    status: string;
    started_at?: string | null;
    version_number?: number | null;
    cells: Record<string, string>;
}
interface GridData { nodes: GridNode[]; runs: GridRun[]; }

interface GridViewProps {
    workflowId: number | null;
    accessToken: string | null;
    apiUrl: string;
    selectedRunId: number | null;
    onSelectRun: (runId: number) => void;
}

const ACTIVE = new Set(["running", "queued", "pending", "pending_action", "claimed", "assigned"]);

// Tailwind bg for a cell/run status.
function cellClass(status?: string): string {
    switch ((status || "").toLowerCase()) {
        case "success":
        case "completed": return "bg-green-500";
        case "running": return "bg-amber-400 animate-pulse";
        case "pending":
        case "queued":
        case "claimed":
        case "assigned":
        case "pending_action": return "bg-slate-300 dark:bg-slate-600";
        case "failure":
        case "failed": return "bg-red-500";
        case "blocked": return "bg-orange-400";
        default: return "bg-slate-200 dark:bg-slate-700";
    }
}

function relTime(iso?: string | null): string {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
}

export default function GridView({ workflowId, accessToken, apiUrl, selectedRunId, onSelectRun }: GridViewProps) {
    const [open, setOpen] = useState(false);
    const [data, setData] = useState<GridData | null>(null);
    const [loading, setLoading] = useState(false);

    const fetchGrid = useCallback(async () => {
        if (!workflowId || !accessToken) return;
        const res = await fetch(`${apiUrl}/road/${workflowId}/runs-grid?limit=30`, {
            headers: { Authorization: `Bearer ${accessToken}` },
        });
        const err = pollErrorForResponse(res);
        if (err) throw err;
        const j = await res.json();
        if (j && Array.isArray(j.nodes) && Array.isArray(j.runs)) setData(j);
    }, [workflowId, accessToken, apiUrl]);

    // Show the spinner on open; the poller drives the recurring refresh.
    useEffect(() => {
        if (!open) return;
        setLoading(true);
        fetchGrid().catch(() => {}).finally(() => setLoading(false));
    }, [open, fetchGrid]);
    // Poll the grid while the dialog is open (shared helper: backoff / stop /
    // pause / single-flight).
    usePollingBackoff(fetchGrid, { baseMs: 4000, enabled: open }, [open]);

    if (!workflowId) return null;

    const nodes = data?.nodes ?? [];
    const runs = data?.runs ?? [];

    return (
        <>
            <Button
                size="sm"
                variant="outline"
                className="gap-1.5"
                onClick={() => setOpen(true)}
                title="Grid view — all runs of this workflow at a glance"
            >
                <LayoutGrid className="h-4 w-4" />Grid
            </Button>

            <Dialog open={open} onOpenChange={setOpen}>
                <DialogContent className="max-w-[95vw] w-[95vw] max-h-[90vh]">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            Run grid
                            {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
                        </DialogTitle>
                        <DialogDescription>
                            Rows are nodes, columns are runs (newest first). Click a run to view it on the canvas.
                            <span className="ml-3 inline-flex items-center gap-2 text-xs">
                                <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-green-500" />done</span>
                                <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-amber-400" />running</span>
                                <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-red-500" />failed</span>
                                <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-slate-300" />pending</span>
                            </span>
                        </DialogDescription>
                    </DialogHeader>

                    {runs.length === 0 ? (
                        <div className="py-16 text-center text-sm text-muted-foreground">
                            {loading ? "Loading…" : "No runs yet. Press Execute to start one."}
                        </div>
                    ) : (
                        <div className="overflow-auto max-h-[70vh] border rounded-md">
                            <table className="border-collapse text-sm">
                                <thead>
                                    <tr>
                                        <th className="sticky left-0 top-0 z-20 bg-background border-b border-r px-3 py-2 text-left font-medium min-w-[160px]">
                                            Node
                                        </th>
                                        {runs.map((r) => {
                                            const sel = r.id === selectedRunId;
                                            return (
                                                <th
                                                    key={r.id}
                                                    className={`sticky top-0 z-10 border-b px-2 py-1.5 text-center cursor-pointer whitespace-nowrap ${sel ? "bg-accent" : "bg-background hover:bg-muted"}`}
                                                    onClick={() => { onSelectRun(r.id); setOpen(false); }}
                                                    title={`View run #${r.id} on the canvas`}
                                                >
                                                    <div className="flex items-center justify-center gap-1.5">
                                                        <span className={`inline-block h-2.5 w-2.5 rounded-sm ${cellClass(r.status)}`} />
                                                        <span className="text-xs font-medium">#{r.id}</span>
                                                    </div>
                                                    <div className="text-[10px] text-muted-foreground">
                                                        {r.version_number != null ? `v${r.version_number} · ` : ""}{relTime(r.started_at)}
                                                    </div>
                                                </th>
                                            );
                                        })}
                                    </tr>
                                </thead>
                                <tbody>
                                    {nodes.map((n) => (
                                        <tr key={n.graph_node_id} className="hover:bg-muted/40">
                                            <td className="sticky left-0 z-10 bg-background border-r px-3 py-1.5 whitespace-nowrap">
                                                <div className="truncate max-w-[220px]" title={`${n.name} (${n.graph_node_id})`}>{n.name}</div>
                                            </td>
                                            {runs.map((r) => {
                                                const st = r.cells[n.graph_node_id];
                                                return (
                                                    <td key={r.id} className="px-2 py-1.5 text-center">
                                                        <span
                                                            className={`inline-block h-4 w-6 rounded-sm ${st ? cellClass(st) : "bg-transparent border border-dashed border-muted-foreground/20"}`}
                                                            title={st ? `${n.name}: ${st} (run #${r.id})` : `${n.name}: not run in #${r.id}`}
                                                        />
                                                    </td>
                                                );
                                            })}
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </DialogContent>
            </Dialog>
        </>
    );
}
