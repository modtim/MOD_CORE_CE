"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

// Triggers popup — moved into the WorkflowBuilder "functions" menu (between
// Process Queue and Node Queue). Mirrors the Dashboard Triggers page: list
// existing triggers, add one (schedule gets a frequency form; every other type
// uses a guided JSON config box), toggle active, delete. Same Radix Dialog
// theme as the Node/Process queues + wizards.

import React, { useState, useEffect, useCallback } from "react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Zap, Plus, Trash2, Pause, Play, Loader2, ArrowLeft } from "lucide-react";


// All 23 trigger types the backend supports. `schedule` gets the rich frequency
// form; the rest use the guided JSON config box prefilled from TYPE_HINTS.
const TRIGGER_TYPES = [
    "schedule", "interval", "one_shot", "workflow_completion", "node_completion",
    "fan_in", "webhook", "api_call", "email", "chatops", "git",
    "file_event", "db_row_event", "system_threshold", "log_pattern_alert",
    "queue_empty", "manual", "approval", "form", "composite", "message_bus", "poll",
] as const;

const TYPE_LABELS: Record<string, string> = {
    schedule: "Schedule", interval: "Interval", one_shot: "One-shot",
    workflow_completion: "Workflow completion", node_completion: "Node completion",
    fan_in: "Fan-in", webhook: "Webhook", api_call: "API call", email: "Email",
    chatops: "ChatOps", git: "Git push", file_event: "File event",
    db_row_event: "DB row event", system_threshold: "System threshold",
    log_pattern_alert: "Log pattern", queue_empty: "Queue empty", manual: "Manual",
    approval: "Approval", form: "Form submit", composite: "Composite",
    message_bus: "Message bus", poll: "Poll",
};

const TYPE_HINTS: Record<string, string> = {
    schedule: '{"cron":"0 9 * * 1","timezone":"UTC"}',
    interval: '{"interval_seconds":900}',
    one_shot: '{"run_at":"2026-07-01T09:00:00Z"}',
    workflow_completion: '{"source_workflow_id":1,"on_status":"succeeded","forward_outputs":true}',
    node_completion: '{"source_workflow_id":1,"workflow_node_id":5,"on_status":"succeeded"}',
    fan_in: '{"sources":[1,2],"window_seconds":3600}',
    webhook: "{}", api_call: "{}", email: '{"address":"intake@flows.example.com"}',
    chatops: '{"provider":"slack","signing_secret":"…","command":"/run"}',
    git: '{"provider":"github","signing_secret":"…","branch":"main"}',
    file_event: '{"source":"s3","bucket":"tenant-1-files","suffix":".psd","event_type":"created"}',
    db_row_event: '{"table":"orders","op":"insert","filter":{"status":"paid"}}',
    system_threshold: '{"metric":"queue_depth","op":"gt","value":50,"rearm_value":20}',
    log_pattern_alert: '{"pattern":"OutOfMemory","level":"ERROR"}',
    queue_empty: '{"scope":"tenant"}', manual: "{}",
    approval: '{"approvers":[1,2]}', form: '{"fields":["name","email"]}',
    composite: '{"op":"and","constituents":[10,11],"window_seconds":3600}',
    message_bus: '{"provider":"redis","topic":"events"}',
    poll: '{"kind":"http","url":"https://example.com/feed.json","interval_seconds":300}',
};

// Cron presets for the schedule form (value = cron, "custom" = free text).
const FREQ_PRESETS: { label: string; value: string }[] = [
    { label: "Every day at 9am UTC", value: "0 9 * * *" },
    { label: "Every hour", value: "0 * * * *" },
    { label: "Every Monday 9am UTC", value: "0 9 * * 1" },
    { label: "First of month, midnight", value: "0 0 1 * *" },
    { label: "Custom cron…", value: "custom" },
];

interface TriggerRow {
    id: number;
    workflow_id: number;
    trigger_type: string;
    configuration: Record<string, unknown> | null;
    is_active: boolean;
    last_triggered_at: string | null;
    trigger_count: number | null;
}

interface WorkflowRow {
    id: number;
    name: string;
}

interface TriggersDialogProps {
    isOpen: boolean;
    onClose: () => void;
    accessToken: string | null;
    /** The workflow currently open on the canvas — the Add form defaults to it. */
    currentWorkflowId?: number | null;
    currentWorkflowName?: string | null;
}

function authHeaders(token: string | null): HeadersInit {
    return token ? { Authorization: `Bearer ${token}` } : {};
}

function statusBadge(active: boolean) {
    return active ? (
        <span className="px-2 py-1 text-xs font-medium rounded-full bg-green-100 text-green-800">Active</span>
    ) : (
        <span className="px-2 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-600">Paused</span>
    );
}

function detailsFor(t: TriggerRow): string {
    const cfg = t.configuration || {};
    if (t.trigger_type === "schedule") return String((cfg as { cron?: string }).cron || "—");
    const s = JSON.stringify(cfg);
    return s === "{}" ? "—" : s.length > 60 ? s.slice(0, 57) + "…" : s;
}

export const TriggersDialog = ({
    isOpen,
    onClose,
    accessToken,
    currentWorkflowId,
    currentWorkflowName,
}: TriggersDialogProps) => {
    const [triggers, setTriggers] = useState<TriggerRow[]>([]);
    const [workflows, setWorkflows] = useState<WorkflowRow[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [showAdd, setShowAdd] = useState(false);
    const [busyId, setBusyId] = useState<number | null>(null);

    // Add-form state
    const [formWorkflowId, setFormWorkflowId] = useState<string>("");
    const [formType, setFormType] = useState<string>("schedule");
    const [formFreq, setFormFreq] = useState<string>("0 9 * * *");
    const [formCustomCron, setFormCustomCron] = useState<string>("");
    const [formConfig, setFormConfig] = useState<string>(TYPE_HINTS.schedule);
    const [saving, setSaving] = useState(false);
    const [formError, setFormError] = useState<string | null>(null);

    const wfName = useCallback(
        (id: number) => workflows.find((w) => w.id === id)?.name || `Workflow #${id}`,
        [workflows],
    );

    const load = useCallback(async () => {
        setIsLoading(true);
        setError(null);
        try {
            const [tResp, wResp] = await Promise.all([
                fetch(`${apiUrl()}/road/triggers/`, { headers: authHeaders(accessToken) }),
                fetch(`${apiUrl()}/road/`, { headers: authHeaders(accessToken) }),
            ]);
            if (!tResp.ok) throw new Error("Failed to load triggers");
            setTriggers(await tResp.json());
            setWorkflows(wResp.ok ? await wResp.json() : []);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setIsLoading(false);
        }
    }, [accessToken]);

    useEffect(() => {
        if (isOpen) load();
    }, [isOpen, load]);

    // When opening the Add form, default the workflow to the one on the canvas.
    const openAddForm = () => {
        setFormWorkflowId(currentWorkflowId ? String(currentWorkflowId) : "");
        setFormType("schedule");
        setFormFreq("0 9 * * *");
        setFormCustomCron("");
        setFormConfig(TYPE_HINTS.schedule);
        setFormError(null);
        setShowAdd(true);
    };

    const onTypeChange = (type: string) => {
        setFormType(type);
        setFormConfig(TYPE_HINTS[type] ?? "{}");
    };

    const submit = async () => {
        setFormError(null);
        if (!formWorkflowId) { setFormError("Please select a workflow."); return; }
        let configuration: Record<string, unknown> = {};
        if (formType === "schedule") {
            const cron = formFreq === "custom" ? formCustomCron.trim() : formFreq;
            if (!cron) { setFormError("Pick a frequency or enter a cron expression."); return; }
            configuration = { cron, timezone: "UTC" };
        } else {
            const raw = (formConfig || "").trim() || "{}";
            try {
                const parsed = JSON.parse(raw);
                if (typeof parsed !== "object" || Array.isArray(parsed) || parsed === null) {
                    setFormError("Configuration must be a JSON object.");
                    return;
                }
                configuration = parsed;
            } catch (e) {
                setFormError(`Configuration is not valid JSON: ${e instanceof Error ? e.message : e}`);
                return;
            }
        }
        setSaving(true);
        try {
            const resp = await fetch(`${apiUrl()}/road/triggers/`, {
                method: "POST",
                headers: { ...authHeaders(accessToken), "Content-Type": "application/json" },
                body: JSON.stringify({
                    workflow_id: parseInt(formWorkflowId, 10),
                    trigger_type: formType,
                    configuration,
                    is_active: true,
                }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                const msg = typeof err.detail === "string"
                    ? err.detail
                    : (err.detail?.msg || JSON.stringify(err.detail || err));
                throw new Error(msg || "Failed to create trigger");
            }
            setShowAdd(false);
            await load();
        } catch (e) {
            setFormError(e instanceof Error ? e.message : String(e));
        } finally {
            setSaving(false);
        }
    };

    const toggle = async (t: TriggerRow) => {
        setBusyId(t.id);
        try {
            await fetch(`${apiUrl()}/road/triggers/${t.id}`, {
                method: "PATCH",
                headers: { ...authHeaders(accessToken), "Content-Type": "application/json" },
                body: JSON.stringify({ is_active: !t.is_active }),
            });
            await load();
        } finally {
            setBusyId(null);
        }
    };

    const remove = async (t: TriggerRow) => {
        if (!confirm(`Delete this ${TYPE_LABELS[t.trigger_type] || t.trigger_type} trigger?`)) return;
        setBusyId(t.id);
        try {
            await fetch(`${apiUrl()}/road/triggers/${t.id}`, {
                method: "DELETE",
                headers: authHeaders(accessToken),
            });
            await load();
        } finally {
            setBusyId(null);
        }
    };

    return (
        <Dialog open={isOpen} onOpenChange={(v) => !v && onClose()}>
            <DialogContent className="max-w-[820px] w-[820px] h-[640px] max-h-[90vh] flex flex-col">
                <DialogHeader className="flex-shrink-0">
                    <DialogTitle className="flex items-center gap-2">
                        <Zap className="h-5 w-5" aria-hidden="true" />
                        {wbNavLabel('wb-nav-triggers', 'Triggers')}
                    </DialogTitle>
                    <DialogDescription>
                        Start workflows from real-world events — schedules, completions, files,
                        webhooks, message buses, thresholds, and more.
                    </DialogDescription>
                </DialogHeader>

                {!showAdd ? (
                    <>
                        <div className="flex-shrink-0 flex justify-end">
                            <Button size="sm" onClick={openAddForm}>
                                <Plus className="h-4 w-4 mr-1" /> Add Trigger
                            </Button>
                        </div>
                        <div className="flex-1 overflow-auto mt-2 border rounded-md">
                            {isLoading ? (
                                <div className="flex items-center justify-center h-full text-gray-500">
                                    <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading…
                                </div>
                            ) : error ? (
                                <div className="p-6 text-center text-red-500 text-sm">{error}</div>
                            ) : triggers.length === 0 ? (
                                <div className="p-8 text-center text-gray-400 text-sm">
                                    No triggers configured. Click “Add Trigger” to get started.
                                </div>
                            ) : (
                                <table className="w-full text-sm">
                                    <thead className="bg-gray-50 sticky top-0">
                                        <tr className="text-left text-xs font-semibold text-gray-600 uppercase">
                                            <th className="px-3 py-2">Workflow</th>
                                            <th className="px-3 py-2">Type</th>
                                            <th className="px-3 py-2">Details</th>
                                            <th className="px-3 py-2">Status</th>
                                            <th className="px-3 py-2">Runs</th>
                                            <th className="px-3 py-2 text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {triggers.map((t) => (
                                            <tr key={t.id} className="border-t hover:bg-gray-50">
                                                <td className="px-3 py-2">{wfName(t.workflow_id)}</td>
                                                <td className="px-3 py-2">{TYPE_LABELS[t.trigger_type] || t.trigger_type}</td>
                                                <td className="px-3 py-2 font-mono text-xs text-gray-600">{detailsFor(t)}</td>
                                                <td className="px-3 py-2">{statusBadge(t.is_active)}</td>
                                                <td className="px-3 py-2">{t.trigger_count || 0}</td>
                                                <td className="px-3 py-2">
                                                    <div className="flex items-center justify-end gap-1">
                                                        <button
                                                            onClick={() => toggle(t)}
                                                            disabled={busyId === t.id}
                                                            title={t.is_active ? "Pause" : "Resume"}
                                                            className="p-1.5 rounded hover:bg-gray-200 disabled:opacity-50"
                                                        >
                                                            {t.is_active ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                                                        </button>
                                                        <button
                                                            onClick={() => remove(t)}
                                                            disabled={busyId === t.id}
                                                            title="Delete"
                                                            className="p-1.5 rounded hover:bg-red-100 text-red-600 disabled:opacity-50"
                                                        >
                                                            <Trash2 className="h-4 w-4" />
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}
                        </div>
                    </>
                ) : (
                    <div className="flex-1 overflow-auto mt-2 space-y-4">
                        <button
                            onClick={() => setShowAdd(false)}
                            className="text-sm text-gray-600 hover:text-gray-900 flex items-center"
                        >
                            <ArrowLeft className="h-4 w-4 mr-1" /> Back to triggers
                        </button>

                        <div>
                            <label htmlFor="trigger-create-workflow" className="block text-xs font-medium text-gray-600 mb-1">Workflow</label>
                            <select
                                id="trigger-create-workflow"
                                value={formWorkflowId}
                                onChange={(e) => setFormWorkflowId(e.target.value)}
                                className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm bg-white"
                            >
                                <option value="">Select a workflow…</option>
                                {currentWorkflowId && !workflows.some((w) => w.id === currentWorkflowId) && (
                                    <option value={String(currentWorkflowId)}>
                                        {currentWorkflowName || `Workflow #${currentWorkflowId}`} (current)
                                    </option>
                                )}
                                {workflows.map((w) => (
                                    <option key={w.id} value={String(w.id)}>
                                        {w.name}{w.id === currentWorkflowId ? " (current)" : ""}
                                    </option>
                                ))}
                            </select>
                        </div>

                        <div>
                            <label htmlFor="trigger-create-type" className="block text-xs font-medium text-gray-600 mb-1">Trigger type</label>
                            <select
                                id="trigger-create-type"
                                value={formType}
                                onChange={(e) => onTypeChange(e.target.value)}
                                className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm bg-white"
                            >
                                {TRIGGER_TYPES.map((t) => (
                                    <option key={t} value={t}>{TYPE_LABELS[t]}</option>
                                ))}
                            </select>
                        </div>

                        {formType === "schedule" ? (
                            <div className="space-y-2">
                                <label className="block text-xs font-medium text-gray-600">Frequency</label>
                                <select
                                    aria-label="Frequency"
                                    value={formFreq}
                                    onChange={(e) => setFormFreq(e.target.value)}
                                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm bg-white"
                                >
                                    {FREQ_PRESETS.map((f) => (
                                        <option key={f.value} value={f.value}>{f.label}</option>
                                    ))}
                                </select>
                                {formFreq === "custom" && (
                                    <input
                                        aria-label="Custom cron expression"
                                        value={formCustomCron}
                                        onChange={(e) => setFormCustomCron(e.target.value)}
                                        placeholder="0 9 * * 1"
                                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono"
                                    />
                                )}
                            </div>
                        ) : (
                            <div>
                                <label className="block text-xs font-medium text-gray-600 mb-1">
                                    Configuration (JSON)
                                </label>
                                <textarea
                                    aria-label="Configuration (JSON)"
                                    value={formConfig}
                                    onChange={(e) => setFormConfig(e.target.value)}
                                    rows={5}
                                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono"
                                />
                                <p className="text-xs text-gray-400 mt-1">
                                    The server validates the shape for <strong>{TYPE_LABELS[formType]}</strong> and
                                    returns a clear error if it&apos;s wrong.
                                </p>
                            </div>
                        )}

                        {formError && <p className="text-sm text-red-500">{formError}</p>}

                        <div className="flex justify-end gap-2">
                            <Button size="sm" variant="outline" onClick={() => setShowAdd(false)}>Cancel</Button>
                            <Button size="sm" onClick={submit} disabled={saving}>
                                {saving && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                                Save trigger
                            </Button>
                        </div>
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
};

export default TriggersDialog;
