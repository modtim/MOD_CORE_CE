"use client";

import { useEffect, useState } from "react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogFooter,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Users, Check, AlertCircle } from "lucide-react";

interface TenantUser {
    id: number;
    email?: string | null;
    username?: string | null;
}

type ShareLevel = "viewer" | "editor" | "owner";

interface WorkflowShareDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    workflowId: number;
    workflowName?: string;
    accessToken?: string | null;
    apiUrl: string;
}

/**
 * Share a WORKFLOW (not a run-link app) with another user in the tenant.
 * Grants the capability set for the chosen level via the API's
 * POST /permissions/workflows/{workflow_id} endpoint so the recipient can
 * see, open and (editor+) run the workflow. Distinct from ShareAppDialog,
 * which creates embeddable public run-links.
 */
export function WorkflowShareDialog({
    open,
    onOpenChange,
    workflowId,
    workflowName,
    accessToken,
    apiUrl,
}: WorkflowShareDialogProps) {
    const [users, setUsers] = useState<TenantUser[]>([]);
    const [loadingUsers, setLoadingUsers] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [selectedUserId, setSelectedUserId] = useState<string>("");
    const [level, setLevel] = useState<ShareLevel>("editor");
    const [submitting, setSubmitting] = useState(false);
    const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

    const authHeaders = (): Record<string, string> => ({
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    });

    // Load tenant users when the dialog opens so the user can pick a recipient.
    useEffect(() => {
        if (!open) return;
        setResult(null);
        setLoadError(null);
        setLoadingUsers(true);
        (async () => {
            try {
                const r = await fetch(`${apiUrl}/users/?limit=200`, {
                    headers: authHeaders(),
                });
                if (!r.ok) {
                    throw new Error(`Failed to load users (${r.status})`);
                }
                const data = await r.json();
                const list: TenantUser[] = Array.isArray(data) ? data : [];
                setUsers(list);
            } catch (e) {
                setLoadError((e as Error)?.message || "Could not load users");
            } finally {
                setLoadingUsers(false);
            }
        })();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, apiUrl, accessToken]);

    const userLabel = (u: TenantUser): string =>
        u.email || u.username || `User #${u.id}`;

    async function handleShare(): Promise<void> {
        if (!selectedUserId) {
            setResult({ ok: false, message: "Pick a user to share with." });
            return;
        }
        setSubmitting(true);
        setResult(null);
        try {
            const r = await fetch(`${apiUrl}/permissions/workflows/${workflowId}`, {
                method: "POST",
                headers: authHeaders(),
                body: JSON.stringify({
                    user_id: Number(selectedUserId),
                    level,
                }),
            });
            if (!r.ok) {
                let detail = `Share failed (${r.status})`;
                try {
                    const body = await r.json();
                    if (body?.detail) {
                        detail =
                            typeof body.detail === "string"
                                ? body.detail
                                : body.detail?.message || detail;
                    }
                } catch {
                    /* keep default detail */
                }
                throw new Error(detail);
            }
            const picked = users.find((u) => String(u.id) === selectedUserId);
            setResult({
                ok: true,
                message: `Shared with ${
                    picked ? userLabel(picked) : `user #${selectedUserId}`
                } as ${level}.`,
            });
        } catch (e) {
            setResult({ ok: false, message: (e as Error)?.message || "Share failed" });
        } finally {
            setSubmitting(false);
        }
    }

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <Users className="h-5 w-5" />
                        Share workflow
                    </DialogTitle>
                    <DialogDescription>
                        Give another member of your organization access to
                        {workflowName ? ` "${workflowName}"` : " this workflow"}.
                        Editors can open and run it; viewers can only see it.
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-4 py-2">
                    <div className="space-y-1.5">
                        <Label htmlFor="wf-share-user">User</Label>
                        {loadingUsers ? (
                            <p className="text-sm text-gray-500">Loading users…</p>
                        ) : loadError ? (
                            <p className="text-sm text-red-600 flex items-center gap-1">
                                <AlertCircle className="h-4 w-4" />
                                {loadError}
                            </p>
                        ) : (
                            <select
                                id="wf-share-user"
                                value={selectedUserId}
                                onChange={(e) => setSelectedUserId(e.target.value)}
                                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                            >
                                <option value="">Select a user…</option>
                                {users.map((u) => (
                                    <option key={u.id} value={String(u.id)}>
                                        {userLabel(u)}
                                    </option>
                                ))}
                            </select>
                        )}
                    </div>

                    <div className="space-y-1.5">
                        <Label htmlFor="wf-share-level">Permission level</Label>
                        <select
                            id="wf-share-level"
                            value={level}
                            onChange={(e) => setLevel(e.target.value as ShareLevel)}
                            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                        >
                            <option value="viewer">Viewer — can see</option>
                            <option value="editor">Editor — can see, open &amp; run</option>
                            <option value="owner">Owner — full control</option>
                        </select>
                    </div>

                    {result && (
                        <p
                            className={`text-sm flex items-center gap-1 ${
                                result.ok ? "text-green-600" : "text-red-600"
                            }`}
                        >
                            {result.ok ? (
                                <Check className="h-4 w-4" />
                            ) : (
                                <AlertCircle className="h-4 w-4" />
                            )}
                            {result.message}
                        </p>
                    )}
                </div>

                <DialogFooter>
                    <Button variant="outline" onClick={() => onOpenChange(false)}>
                        Cancel
                    </Button>
                    <Button
                        onClick={handleShare}
                        disabled={submitting || loadingUsers || !selectedUserId}
                    >
                        {submitting ? "Sharing…" : "Share"}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
