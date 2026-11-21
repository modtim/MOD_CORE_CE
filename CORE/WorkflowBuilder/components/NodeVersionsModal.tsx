"use client";

// Output versions viewer (node-run-controls, keep + version). Every attempt of a
// node in a run is kept — re-runs never overwrite — so you can open any past
// attempt and download its outputs to see what went wrong. Newest first.
// Reuses AssetPreview (the same widget the canvas nodes use).
import { useCallback, useEffect, useState } from "react";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { AssetPreview, type PreviewAsset } from "@/components/nodes/asset-preview";
import { Loader2, CheckCircle2, XCircle, Clock, History, Download } from "lucide-react";

type OutputAsset = PreviewAsset & { download_url?: string };
interface Version {
    task_id: number;
    attempt: string;
    label: string;
    status: string;
    created_at?: string | null;
    completed_at?: string | null;
    error_message?: string | null;
    outputs: OutputAsset[];
}
interface VersionsResponse {
    run_id: number;
    node_id: string;
    attempts: number;
    versions: Version[];
}

interface Props {
    open: boolean;
    onClose: () => void;
    runId: number | null;
    nodeId: string | null;
    nodeName?: string | null;
    apiUrl: string;
    accessToken: string | null;
}

const DONE = new Set(["success", "completed"]);
const FAILED = new Set(["failure", "failed", "cancelled"]);

function StatusPill({ status }: { status: string }) {
    const s = (status || "").toLowerCase();
    if (DONE.has(s)) return <span className="flex items-center gap-1 text-[11px] text-green-600 font-medium"><CheckCircle2 className="h-3.5 w-3.5" /> done</span>;
    if (FAILED.has(s)) return <span className="flex items-center gap-1 text-[11px] text-red-600 font-medium"><XCircle className="h-3.5 w-3.5" /> {s}</span>;
    if (s === "running" || s === "pending_action") return <span className="flex items-center gap-1 text-[11px] text-blue-600 font-medium"><Loader2 className="h-3.5 w-3.5 animate-spin" /> running</span>;
    return <span className="flex items-center gap-1 text-[11px] text-muted-foreground font-medium"><Clock className="h-3.5 w-3.5" /> {s || "queued"}</span>;
}

function relTime(iso?: string | null): string {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    return new Date(t).toLocaleString();
}

export default function NodeVersionsModal({ open, onClose, runId, nodeId, nodeName, apiUrl, accessToken }: Props) {
    const [data, setData] = useState<VersionsResponse | null>(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        if (!open || !runId || !nodeId || !accessToken) return;
        try {
            const r = await fetch(
                `${apiUrl}/runs/${runId}/node-versions/${encodeURIComponent(nodeId)}`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } }
            );
            if (!r.ok) return;
            setData(await r.json());
        } catch { /* transient */ }
    }, [open, runId, nodeId, apiUrl, accessToken]);

    useEffect(() => {
        if (!open) { setData(null); return; }
        setLoading(true);
        load().finally(() => setLoading(false));
    }, [open, runId, nodeId, load]);

    const versions = data?.versions || [];
    const title = nodeName || nodeId || "Node";

    return (
        <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
            <DialogContent className="max-w-3xl max-h-[85vh] flex flex-col">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2 flex-wrap">
                        <History className="h-4 w-4 text-blue-500" />
                        <span className="truncate">Output versions · {title}</span>
                        <span className="rounded-full bg-blue-500/15 text-blue-600 text-[11px] px-2 py-0.5 leading-none">
                            {versions.length} attempt{versions.length === 1 ? "" : "s"}
                        </span>
                    </DialogTitle>
                    <p className="text-xs text-muted-foreground">Every attempt is kept — nothing is overwritten or deleted. Open any past attempt to download its outputs and see what went wrong.</p>
                </DialogHeader>

                {/* Native overflow container, not a Radix ScrollArea: the Radix
                    viewport's h-full resolves against auto height in this flex
                    column so it never scrolled (only the first few attempts were
                    reachable) — same bug as RunRail/FanoutModal. flex-1 min-h-0
                    gives it a real bounded height under max-h-[85vh]. */}
                <div className="flex-1 min-h-0 overflow-y-auto -mx-1 px-1">
                    <div className="space-y-3 pb-1">
                        {versions.map((v) => (
                            <div key={v.task_id} className="rounded-lg border bg-card p-3">
                                <div className="flex items-center justify-between mb-2 gap-2">
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm font-semibold">{v.label}</span>
                                        <span className="text-[11px] text-muted-foreground">{relTime(v.completed_at || v.created_at)}</span>
                                    </div>
                                    <StatusPill status={v.status} />
                                </div>
                                {v.outputs && v.outputs.length ? (
                                    <>
                                        <AssetPreview assets={v.outputs} />
                                        <div className="mt-2 flex flex-wrap gap-2">
                                            {v.outputs.map((o, i) => (
                                                <a key={i} href={o.download_url || o.url} target="_blank" rel="noreferrer"
                                                   className="inline-flex items-center gap-1 text-[11px] text-blue-600 hover:underline">
                                                    <Download className="h-3 w-3" /> {o.logical_name || `${o.content_hash.slice(0, 8)}.${o.file_extension}`}
                                                </a>
                                            ))}
                                        </div>
                                    </>
                                ) : (
                                    <div className="h-16 flex items-center justify-center rounded bg-muted/50 text-[11px] text-muted-foreground text-center px-2">
                                        {FAILED.has((v.status || "").toLowerCase())
                                            ? (v.error_message || "failed — no outputs")
                                            : "no outputs for this attempt"}
                                    </div>
                                )}
                            </div>
                        ))}
                        {!loading && versions.length === 0 && (
                            <div className="py-12 text-center text-sm text-muted-foreground">No attempts recorded for this node yet.</div>
                        )}
                        {loading && versions.length === 0 && (
                            <div className="py-12 flex items-center justify-center text-sm text-muted-foreground gap-2">
                                <Loader2 className="h-4 w-4 animate-spin" /> Loading versions…
                            </div>
                        )}
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
