"use client";

// Fan-out inspector. When a node runs as a batch fan-out (one task per input
// file), its outgoing edge shows a "FAN ×N" badge; clicking it opens this
// modal. Each row is one fanned item, paired EXACTLY by its source cog id
// (the per-file lineage key the backend carries on every task): the LEFT block
// is that file's input, the RIGHT block is that file's own output. Live —
// statuses and outputs fill in as each fanned task completes.
//
// Reuses the node canvas's AssetPreview so the blocks look and behave exactly
// like the previews on the nodes themselves (image / video / 3D / audio / …).
import { useCallback, useEffect, useState } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { AssetPreview, type PreviewAsset } from "@/components/nodes/asset-preview";
import { Loader2, CheckCircle2, XCircle, Clock, ArrowRight, GitBranch, Ban } from "lucide-react";

interface FanoutItem {
    cog_id: number | null;
    name: string;
    status: string;
    task_id: number;
    input: PreviewAsset | null;
    output: PreviewAsset | null;
    outputs: PreviewAsset[];
    error_message?: string | null;
}

interface FanoutResponse {
    run_id: number;
    node_id: string;
    name: string;
    fanned: boolean;
    item_count: number;
    items: FanoutItem[];
}

interface FanoutModalProps {
    open: boolean;
    onClose: () => void;
    runId: number | null;
    nodeId: string | null;
    nodeName?: string | null;
    apiUrl: string;
    accessToken: string | null;
}

const TERMINAL = new Set(["success", "completed", "failure", "failed", "cancelled", "blocked"]);
const DONE = new Set(["success", "completed"]);
const FAILED = new Set(["failure", "failed", "cancelled"]);

function StatusPill({ status }: { status: string }) {
    const s = (status || "").toLowerCase();
    if (DONE.has(s))
        return (
            <span className="flex items-center gap-1 text-[11px] text-green-600 font-medium">
                <CheckCircle2 className="h-3.5 w-3.5" /> done
            </span>
        );
    if (FAILED.has(s))
        return (
            <span className="flex items-center gap-1 text-[11px] text-red-600 font-medium">
                <XCircle className="h-3.5 w-3.5" /> {s === "cancelled" ? "cancelled" : "failed"}
            </span>
        );
    if (s === "blocked")
        return (
            <span className="flex items-center gap-1 text-[11px] text-amber-600 font-medium">
                <Ban className="h-3.5 w-3.5" /> blocked
            </span>
        );
    if (s === "running" || s === "pending_action")
        return (
            <span className="flex items-center gap-1 text-[11px] text-blue-600 font-medium">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> running
            </span>
        );
    return (
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground font-medium">
            <Clock className="h-3.5 w-3.5" /> {s || "queued"}
        </span>
    );
}

function EmptyBlock({ label }: { label: string }) {
    return (
        <div className="mt-2 w-full h-24 flex items-center justify-center rounded bg-muted/50 text-[11px] text-muted-foreground text-center px-2">
            {label}
        </div>
    );
}

function PendingBlock({ status }: { status: string }) {
    const s = (status || "").toLowerCase();
    const running = s === "running" || s === "pending_action";
    return (
        <div className="mt-2 w-full h-24 flex flex-col items-center justify-center gap-1 rounded bg-muted/50 text-[11px] text-muted-foreground">
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Clock className="h-4 w-4" />}
            {running ? "processing…" : "waiting…"}
        </div>
    );
}

export default function FanoutModal({
    open,
    onClose,
    runId,
    nodeId,
    nodeName,
    apiUrl,
    accessToken,
}: FanoutModalProps) {
    const [data, setData] = useState<FanoutResponse | null>(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        if (!open || !runId || !nodeId || !accessToken) return;
        const r = await fetch(
            // clean-v2-api: /v1/runs (was /v1/node-runs; old path kept as a
            // back-compat alias until every caller is repointed — see
            // CORE/API/routers/runs.py).
            `${apiUrl}/runs/${runId}/node-fanout/${encodeURIComponent(nodeId)}`,
            { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } }
        );
        const err = pollErrorForResponse(r);
        if (err) throw err;
        const d: FanoutResponse = await r.json();
        setData(d);
    }, [open, runId, nodeId, apiUrl, accessToken]);

    // Initial load whenever the modal opens or the target changes.
    useEffect(() => {
        if (!open) {
            setData(null);
            return;
        }
        setLoading(true);
        load().catch(() => {}).finally(() => setLoading(false));
    }, [open, runId, nodeId, load]);

    // Poll while any item is still non-terminal (through the shared helper), so
    // outputs appear as each fanned task finishes; stops once all are terminal.
    const anyLive =
        open && (!data || data.items.some((it) => !TERMINAL.has((it.status || "").toLowerCase())));
    usePollingBackoff(load, { baseMs: 3000, enabled: anyLive }, [runId, nodeId]);

    const items = data?.items || [];
    const doneCount = items.filter((it) => TERMINAL.has((it.status || "").toLowerCase())).length;
    const title = nodeName || data?.name || nodeId || "Fan-out";

    return (
        <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
            <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2 flex-wrap">
                        <GitBranch className="h-4 w-4 text-amber-500" />
                        <span className="truncate">Fan-out · {title}</span>
                        <span className="rounded-full bg-amber-500/15 text-amber-600 text-[11px] px-2 py-0.5 leading-none">
                            {items.length} items
                        </span>
                        {items.length > 0 && doneCount < items.length && (
                            <span className="text-[11px] font-normal text-muted-foreground">
                                {doneCount}/{items.length} done
                            </span>
                        )}
                    </DialogTitle>
                </DialogHeader>

                {/* Native overflow container, not a Radix ScrollArea: the Radix
                    viewport's h-full resolves against auto height inside this flex
                    column, so it never scrolled and only the first few items were
                    reachable (same bug as RunRail). flex-1 min-h-0 gives it a real
                    bounded height under DialogContent's max-h-[85vh]. */}
                <div className="flex-1 min-h-0 overflow-y-auto -mx-1 px-1">
                    <div className="space-y-2 pb-1">
                        {items.map((it) => {
                            const st = (it.status || "").toLowerCase();
                            const outAssets = it.outputs && it.outputs.length ? it.outputs : it.output ? [it.output] : [];
                            return (
                                <div key={it.task_id} className="rounded-lg border bg-card p-2">
                                    <div className="flex items-center justify-between mb-1 gap-2">
                                        <div className="truncate text-sm font-medium" title={it.name}>
                                            {it.name}
                                        </div>
                                        <StatusPill status={it.status} />
                                    </div>
                                    <div className="grid grid-cols-[1fr_auto_1fr] gap-2 items-start">
                                        <div className="min-w-0">
                                            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                                                Input
                                            </div>
                                            {it.input ? (
                                                <AssetPreview assets={[it.input]} />
                                            ) : (
                                                <EmptyBlock label="no input preview" />
                                            )}
                                        </div>
                                        <div className="flex items-center h-24">
                                            <ArrowRight className="h-4 w-4 text-muted-foreground shrink-0" />
                                        </div>
                                        <div className="min-w-0">
                                            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                                                Output
                                            </div>
                                            {outAssets.length ? (
                                                <AssetPreview assets={outAssets} />
                                            ) : FAILED.has(st) ? (
                                                <EmptyBlock label={it.error_message || "failed — no output"} />
                                            ) : DONE.has(st) ? (
                                                <EmptyBlock label="no output produced" />
                                            ) : (
                                                <PendingBlock status={it.status} />
                                            )}
                                        </div>
                                    </div>
                                </div>
                            );
                        })}

                        {!loading && items.length === 0 && (
                            <div className="py-12 text-center text-sm text-muted-foreground">
                                No fanned items for this node.
                            </div>
                        )}
                        {loading && items.length === 0 && (
                            <div className="py-12 flex items-center justify-center text-sm text-muted-foreground gap-2">
                                <Loader2 className="h-4 w-4 animate-spin" /> Loading fan-out…
                            </div>
                        )}
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
