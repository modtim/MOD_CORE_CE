"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

/**
 * Gate 5 — the surface a person answers a paused pipeline from.
 *
 * A workflow that reaches a `human_approval` node stops without dispatching
 * anything: no container, no worker slot, just a row waiting on a decision.
 * This is where that decision gets made.
 *
 * Approving resumes the run through the ordinary DAG advance, so the next node
 * dispatches exactly as if a container had finished. Rejecting fails the run —
 * the point of asking was that the answer might be no, and a "no" that let
 * delivery proceed would make the gate decorative.
 *
 * Notes are carried on both paths. On a rejection they become the run's
 * error_message, which is the only place the next person looking at a failed
 * run will think to check.
 */

import React, { useState, useEffect, useCallback } from "react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { CheckCircle2, Loader2, RefreshCw, XCircle, Inbox, ExternalLink } from "lucide-react";

const API_URL = apiUrl();

interface Intervention {
    id: number;
    node_run_id: number | null;
    workflow_run_id: number | null;
    status: string;
    instructions: string | null;
    completion_notes: string | null;
    assigned_to_user_id: number | null;
    assigned_at: string | null;
    workflow_id: number | null;
    workflow_version_id: number | null;
}

interface PendingApprovalsDialogProps {
    isOpen: boolean;
    onClose: () => void;
    accessToken: string | null;
    /** Fired after a decision lands so the parent can refresh run views —
     *  an approval changes a run's status, and a stale queue beside a
     *  freshly-resumed run is the confusing part. */
    onDecided?: () => void;
    /** Load the paused run onto the canvas. Deciding without being able to
     *  look at what you are approving is a worse version of the problem the
     *  gate exists to solve. */
    onOpenWorkflow?: (workflowId: number, versionId: number, runId: number) => void;
}

function formatWhen(iso: string | null): string {
    if (!iso) return "";
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export function PendingApprovalsDialog({
    isOpen,
    onClose,
    accessToken,
    onDecided,
    onOpenWorkflow,
}: PendingApprovalsDialogProps): React.JSX.Element {
    const [items, setItems] = useState<Intervention[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [notes, setNotes] = useState<Record<number, string>>({});
    // Per-row rather than a single flag: two approvals in flight at once
    // would otherwise disable each other's buttons.
    const [busyId, setBusyId] = useState<number | null>(null);
    const [error, setError] = useState<string | null>(null);

    const fetchItems = useCallback(async () => {
        if (!accessToken) return;
        setIsLoading(true);
        setError(null);
        try {
            const response = await fetch(`${API_URL}/interventions`, {
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    Accept: 'application/json',
                },
            });
            if (!response.ok) throw new Error(`Failed to load approvals (${response.status})`);
            const data: Intervention[] = await response.json();
            setItems(data);
        } catch (err) {
            setError((err as Error)?.message || 'Failed to load approvals');
            setItems([]);
        } finally {
            setIsLoading(false);
        }
    }, [accessToken]);

    useEffect(() => {
        if (isOpen) fetchItems();
    }, [isOpen, fetchItems]);

    const decide = useCallback(async (id: number, action: 'approve' | 'reject') => {
        if (!accessToken) return;
        setBusyId(id);
        setError(null);
        try {
            const response = await fetch(`${API_URL}/interventions/${id}/${action}`, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                },
                body: JSON.stringify({ notes: notes[id] || null }),
            });
            if (!response.ok) {
                // 409 is the one worth naming: somebody already decided this,
                // and the run has moved on. Reporting it as a generic failure
                // would leave the user retrying a decision that already stuck.
                let detail = `Failed to ${action} (${response.status})`;
                try {
                    const body = await response.json();
                    const d = body?.detail;
                    if (d) detail = (typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
                } catch {
                    // Non-JSON error body — the status text is all there is.
                }
                throw new Error(detail);
            }
            setItems((prev) => prev.filter((i) => i.id !== id));
            setNotes((prev) => {
                const next = { ...prev };
                delete next[id];
                return next;
            });
            onDecided?.();
        } catch (err) {
            setError((err as Error)?.message || `Failed to ${action}`);
            // Refresh on failure: if this was a 409 the row is stale, and
            // leaving it on screen invites the same doomed click again.
            fetchItems();
        } finally {
            setBusyId(null);
        }
    }, [accessToken, notes, onDecided, fetchItems]);

    return (
        <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        {wbNavLabel('wb-nav-approvals', 'Pending Approvals')}
                        {items.length > 0 && (
                            <span className="text-xs font-normal px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600">
                                {items.length}
                            </span>
                        )}
                    </DialogTitle>
                    <DialogDescription>
                        Workflows paused waiting on your decision. Approving resumes the
                        run from where it stopped; rejecting fails it.
                    </DialogDescription>
                </DialogHeader>

                {error && (
                    <div className="text-sm rounded-md border border-red-500/30 bg-red-500/10 text-red-600 px-3 py-2">
                        {error}
                    </div>
                )}

                {isLoading && (
                    <div className="flex items-center justify-center py-10 text-muted-foreground">
                        <Loader2 className="h-5 w-5 animate-spin mr-2" />
                        Loading…
                    </div>
                )}

                {!isLoading && items.length === 0 && (
                    <div className="flex flex-col items-center justify-center py-10 text-muted-foreground">
                        <Inbox className="h-8 w-8 mb-2 opacity-50" />
                        <p className="text-sm">Nothing is waiting on you.</p>
                    </div>
                )}

                {!isLoading && items.map((item) => (
                    <div key={item.id} className="rounded-lg border p-4 space-y-3">
                        <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                                <p className="font-medium break-words">
                                    {item.instructions || `Approval #${item.id}`}
                                </p>
                                <p className="text-xs text-muted-foreground mt-1">
                                    {item.workflow_run_id !== null && (
                                        <>Run #{item.workflow_run_id} · </>
                                    )}
                                    Assigned {formatWhen(item.assigned_at)}
                                </p>
                            </div>
                        </div>

                        <textarea
                            className="w-full text-sm rounded-md border bg-transparent px-3 py-2 min-h-[60px]"
                            placeholder="Notes (optional — kept on the run, and shown as the failure reason if you reject)"
                            value={notes[item.id] || ''}
                            onChange={(e) => setNotes((p) => ({ ...p, [item.id]: e.target.value }))}
                            disabled={busyId === item.id}
                        />

                        <div className="flex items-center gap-2">
                            <Button
                                size="sm"
                                onClick={() => decide(item.id, 'approve')}
                                disabled={busyId === item.id}
                            >
                                {busyId === item.id ? (
                                    <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                ) : (
                                    <CheckCircle2 className="h-4 w-4 mr-1" />
                                )}
                                Approve
                            </Button>
                            <Button
                                size="sm"
                                variant="destructive"
                                onClick={() => decide(item.id, 'reject')}
                                disabled={busyId === item.id}
                            >
                                <XCircle className="h-4 w-4 mr-1" />
                                Reject
                            </Button>
                            {onOpenWorkflow
                                && item.workflow_id != null
                                && item.workflow_version_id != null
                                && item.workflow_run_id != null && (
                                <Button
                                    size="sm"
                                    variant="outline"
                                    className="ml-auto"
                                    onClick={() => onOpenWorkflow(
                                        item.workflow_id as number,
                                        item.workflow_version_id as number,
                                        item.workflow_run_id as number,
                                    )}
                                >
                                    <ExternalLink className="h-4 w-4 mr-1" />
                                    Open workflow
                                </Button>
                            )}
                        </div>
                    </div>
                ))}

                <div className="flex justify-end pt-2">
                    <Button variant="ghost" size="sm" onClick={fetchItems} disabled={isLoading}>
                        <RefreshCw className="h-4 w-4 mr-1" />
                        Refresh
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
