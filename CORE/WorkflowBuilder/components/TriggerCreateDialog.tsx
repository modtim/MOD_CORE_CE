"use client";

/**
 * Trigger creation dialog — surface all six workflow_trigger types in the
 * WorkflowBuilder toolbar. Mirrors the dashboard's modal flow so the two
 * places to create triggers stay consistent. The selected workflow is
 * always the currently-open one (passed in as `workflowId` / `tenantId`).
 *
 * On a successful webhook create, the dialog swaps to a one-shot result
 * view that exposes the firing URL + plaintext secret with Copy buttons.
 * The secret is unrecoverable after dismiss, so the user has to copy it
 * here or rotate the secret later from the dashboard Triggers page.
 */

import { useEffect, useMemo, useState } from "react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogFooter,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { Copy } from "lucide-react";

export type TriggerType =
    | "schedule"
    | "workflow_completion"
    | "webhook"
    | "file_event"
    | "log_pattern_alert"
    | "queue_empty"
    | "message_bus";

interface WorkflowSummary {
    id: number;
    name: string;
}

interface MeResponse {
    tenant_id?: number;
    tenantId?: number;
    user?: { tenant_id?: number };
}

interface Props {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    initialType?: TriggerType;
    workflowId: number;
    workflowName?: string;
    accessToken: string | null;
    apiUrl: string;
    /** Optional initial cron expression — used when the toolbar opens
     *  the dialog from a Schedule preset that already has a cron. */
    initialCron?: string;
    /** Called after successful trigger creation (any type). Toolbar can
     *  surface a toast or refresh state. Not invoked for the
     *  intermediate webhook-result view. */
    onCreated?: (triggerType: TriggerType) => void;
    /** PF 4.9 — preset message_bus config (same pattern as initialCron).
     *  There is deliberately no credential value prop: broker auth is
     *  carried as a stored-secret REFERENCE only. */
    initialBusProvider?: "redis" | "kafka" | "mqtt";
    initialBusTopic?: string;
    initialBusUrl?: string;
    initialBusAuth?: "none" | "secret";
    initialBusSecretRef?: string;
}

const TYPE_LABELS: Record<TriggerType, string> = {
    schedule: "Schedule (cron)",
    workflow_completion: "Workflow Completion",
    webhook: "Webhook",
    file_event: "File Event (object storage)",
    log_pattern_alert: "Log Pattern Alert",
    queue_empty: "Queue Empty",
    message_bus: "Message Bus",
};

// PF 4.9 — message_bus. The backend validator (services/triggers/validator.py
// _validate_message_bus) accepts provider redis/kafka/nats/rabbitmq/mqtt, but
// the create gate (services/workflow_triggers_service.py) refuses any provider
// outside streaming.IMPLEMENTED_MESSAGE_BUS_PROVIDERS — a trigger for an
// unimplemented provider silently never fires — so the UI only offers the
// implemented set. Mirror that constant here.
const MESSAGE_BUS_PROVIDER_OPTIONS = ["redis", "kafka", "mqtt"] as const;
const MESSAGE_BUS_AUTH_OPTIONS = ["none", "secret"] as const;

// Client-side mirrors of the backend topic validators
// (services/triggers/streaming.py validate_kafka_topic / validate_mqtt_topic).
const KAFKA_TOPIC_RE = /^[A-Za-z0-9._-]{1,249}$/;

function kafkaTopicError(topic: string): string | null {
    if (!topic) return "Topic is required";
    if (topic === "." || topic === "..")
        return `invalid kafka topic name: ${topic}`;
    if (!KAFKA_TOPIC_RE.test(topic))
        return `invalid kafka topic name: ${topic}`;
    return null;
}

function mqttTopicError(topic: string): string | null {
    if (!topic) return "Topic is required";
    if (topic.length > 256)
        return `mqtt topic too long (${topic.length} > 256 chars)`;
    if (topic.includes("\0")) return "mqtt topic contains NUL";
    if (topic.startsWith("$"))
        return `mqtt topic is broker-internal ($-prefixed): ${topic}`;
    const levels = topic.split("/");
    for (let i = 0; i < levels.length; i++) {
        const level = levels[i];
        if (level.includes("#") && !(level === "#" && i === levels.length - 1))
            return `mqtt topic has '#' in an invalid position: ${topic}`;
        if (level.includes("+") && level !== "+")
            return `mqtt topic has '+' in an invalid position: ${topic}`;
    }
    return null;
}

function messageBusTopicError(provider: string, topic: string): string | null {
    if (!topic.trim()) return "Topic is required";
    if (provider === "kafka") return kafkaTopicError(topic);
    if (provider === "mqtt") return mqttTopicError(topic);
    return null;
}

const ON_STATUS_OPTIONS = ["succeeded", "failed", "any"] as const;
const FILE_EVENT_OPTIONS = ["created", "deleted", "any"] as const;
const LOG_LEVEL_OPTIONS = ["WARNING", "ERROR", "CRITICAL", "any"] as const;
const LOG_FIELD_OPTIONS = ["message", "module", "any"] as const;
const QUEUE_SCOPE_OPTIONS = ["tenant", "workflow"] as const;

export function TriggerCreateDialog({
    open,
    onOpenChange,
    initialType = "schedule",
    workflowId,
    workflowName,
    accessToken,
    apiUrl,
    initialCron,
    onCreated,
    initialBusProvider,
    initialBusTopic,
    initialBusUrl,
    initialBusAuth,
    initialBusSecretRef,
}: Props) {
    // launch-day-followup-2 #16: only "schedule" (cron) is creatable. The
    // setter is kept so the existing useEffect that resets state on dialog
    // reopen still type-checks; it'll only ever set "schedule".
    // PF 4.9: "message_bus" is creatable alongside cron; the other legacy
    // types stay gated to "schedule" (launch-day-followup-2 #16).
    const [triggerType, setTriggerType] = useState<TriggerType>(
        initialType === "message_bus" ? "message_bus" : "schedule",
    );
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Schedule
    const [cron, setCron] = useState<string>(initialCron || "0 9 * * *");

    // Workflow completion
    const [completionSourceWorkflow, setCompletionSourceWorkflow] = useState<string>("");
    const [completionOnStatus, setCompletionOnStatus] =
        useState<(typeof ON_STATUS_OPTIONS)[number]>("succeeded");
    const [completionForwardOutputs, setCompletionForwardOutputs] = useState(false);

    // Webhook
    const [webhookForwardBody, setWebhookForwardBody] = useState(false);
    const [webhookResult, setWebhookResult] = useState<
        { url: string; secret: string } | null
    >(null);

    // File event
    const [tenantBucket, setTenantBucket] = useState<string>("");
    const [filePrefix, setFilePrefix] = useState("");
    const [fileSuffix, setFileSuffix] = useState("");
    const [fileGlob, setFileGlob] = useState("");
    const [fileEventType, setFileEventType] =
        useState<(typeof FILE_EVENT_OPTIONS)[number]>("created");
    const [fileForwardObjectPath, setFileForwardObjectPath] = useState(false);

    // Log pattern
    const [logPattern, setLogPattern] = useState("");
    const [logRegexError, setLogRegexError] = useState<string | null>(null);
    const [logLevel, setLogLevel] =
        useState<(typeof LOG_LEVEL_OPTIONS)[number]>("ERROR");
    const [logMatchField, setLogMatchField] =
        useState<(typeof LOG_FIELD_OPTIONS)[number]>("message");
    const [logCooldown, setLogCooldown] = useState<number>(60);

    // Queue empty
    const [queueScope, setQueueScope] =
        useState<(typeof QUEUE_SCOPE_OPTIONS)[number]>("tenant");
    const [queueWatchedWorkflow, setQueueWatchedWorkflow] = useState<string>("");
    const [queueCooldown, setQueueCooldown] = useState<number>(300);

    // Message bus (PF 4.9). Mirrors _validate_message_bus + the create-time
    // provider gate. NOTE: there is deliberately NO raw password field — the
    // broker credentials live in the tenant secret store and the trigger only
    // carries a `secret_ref` reference to them.
    const [busProvider, setBusProvider] =
        useState<(typeof MESSAGE_BUS_PROVIDER_OPTIONS)[number]>(
            initialBusProvider || "kafka",
        );
    const [busTopic, setBusTopic] = useState<string>(initialBusTopic || "");
    const [busUrl, setBusUrl] = useState<string>(initialBusUrl || "");
    const [busAuth, setBusAuth] =
        useState<(typeof MESSAGE_BUS_AUTH_OPTIONS)[number]>(
            initialBusAuth || "none",
        );
    const [busSecretRef, setBusSecretRef] = useState<string>(
        initialBusSecretRef || "",
    );
    const [busTls, setBusTls] = useState<boolean>(true);
    const [busConsumerTimeoutMs, setBusConsumerTimeoutMs] = useState<number>(10000);

    // Workflow picker source (used by completion and queue-empty types).
    const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);

    // ----- Side effects -------------------------------------------------

    useEffect(() => {
        if (!open) {
            // Reset transient state on close so a follow-up open starts
            // fresh — but DON'T wipe webhookResult while it's visible.
            setError(null);
            return;
        }
        setTriggerType(initialType);
        if (initialCron) setCron(initialCron);

        // Fetch workflows + current tenant bucket in parallel.
        if (!accessToken) return;
        const headers = { Authorization: `Bearer ${accessToken}` };

        fetch(`${apiUrl}/road/`, { headers })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
                const list: WorkflowSummary[] =
                    data?.workflows || data || [];
                setWorkflows(
                    Array.isArray(list)
                        ? list.map((w: any) => ({ id: w.id, name: w.name }))
                        : [],
                );
            })
            .catch(() => setWorkflows([]));

        fetch(`${apiUrl}/me`, { headers })
            .then((r) => (r.ok ? r.json() : null))
            .then((me: MeResponse | null) => {
                const tid =
                    me?.tenant_id ?? me?.tenantId ?? me?.user?.tenant_id;
                if (typeof tid === "number") {
                    setTenantBucket(`tenant-${tid}-files`);
                }
            })
            .catch(() => {
                /* leave bucket empty — user must fill it manually */
            });
    }, [open, accessToken, apiUrl, initialType, initialCron]);

    // Live regex validation — disable Save when invalid.
    useEffect(() => {
        if (!logPattern) {
            setLogRegexError(null);
            return;
        }
        try {
            // eslint-disable-next-line no-new
            new RegExp(logPattern);
            setLogRegexError(null);
        } catch (e) {
            setLogRegexError(`Invalid regex: ${(e as Error).message}`);
        }
    }, [logPattern]);

    const submitDisabled = useMemo(() => {
        if (submitting) return true;
        if (triggerType === "log_pattern_alert" && logRegexError) return true;
        return false;
    }, [submitting, triggerType, logRegexError]);

    // ----- Submit -------------------------------------------------------

    async function handleSubmit() {
        setError(null);
        if (!accessToken) {
            setError("No access token — re-login and try again.");
            return;
        }

        const config: Record<string, unknown> = {};

        if (triggerType === "schedule") {
            if (!cron.trim() || cron.trim().split(/\s+/).length !== 5) {
                setError("Cron must have 5 fields: minute hour day month weekday");
                return;
            }
            config.cron = cron.trim();
            config.timezone = "UTC";
        } else if (triggerType === "workflow_completion") {
            if (!completionSourceWorkflow) {
                setError("Select a source workflow");
                return;
            }
            config.source_workflow_id = parseInt(completionSourceWorkflow, 10);
            config.on_status = completionOnStatus;
            config.forward_outputs = completionForwardOutputs;
        } else if (triggerType === "webhook") {
            config.allowed_methods = ["POST"];
            config.forward_body = webhookForwardBody;
        } else if (triggerType === "file_event") {
            if (!tenantBucket) {
                setError("Bucket is required");
                return;
            }
            config.bucket = tenantBucket;
            if (filePrefix) config.prefix = filePrefix;
            if (fileSuffix) config.suffix = fileSuffix;
            if (fileGlob) config.glob = fileGlob;
            config.event_type = fileEventType;
            config.forward_object_path = fileForwardObjectPath;
        } else if (triggerType === "log_pattern_alert") {
            if (!logPattern) {
                setError("Pattern is required");
                return;
            }
            if (logRegexError) {
                setError(logRegexError);
                return;
            }
            config.pattern = logPattern;
            config.log_level = logLevel;
            config.match_field = logMatchField;
            config.cooldown_seconds = logCooldown;
        } else if (triggerType === "message_bus") {
            const topicErr = messageBusTopicError(busProvider, busTopic);
            if (topicErr) {
                setError(topicErr);
                return;
            }
            if (busAuth === "secret" && !busSecretRef.trim()) {
                setError("Secret reference is required — pick a stored secret "
                    + "or choose \"No broker auth\".");
                return;
            }
            config.provider = busProvider;
            config.topic = busTopic.trim();
            if (busUrl.trim()) config.url = busUrl.trim();
            if (busAuth === "secret") config.secret_ref = busSecretRef.trim();
            if (busProvider === "kafka") {
                config.tls = busTls;
                config.consumer_timeout_ms = busConsumerTimeoutMs;
            }
        } else if (triggerType === "queue_empty") {
            config.scope = queueScope;
            if (queueScope === "workflow") {
                if (!queueWatchedWorkflow) {
                    setError("Select a workflow to watch");
                    return;
                }
                config.source_workflow_id = parseInt(queueWatchedWorkflow, 10);
            } else {
                config.source_workflow_id = null;
            }
            config.cooldown_seconds = queueCooldown;
        }

        setSubmitting(true);
        try {
            const resp = await fetch(`${apiUrl}/road/triggers/`, {
                method: "POST",
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    workflow_id: workflowId,
                    trigger_type: triggerType,
                    configuration: config,
                    is_active: true,
                }),
            });
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                const detail =
                    typeof body.detail === "string"
                        ? body.detail
                        : body.detail?.msg || JSON.stringify(body.detail || body);
                setError(detail || "Failed to create trigger");
                return;
            }
            const data = await resp.json();
            if (
                triggerType === "webhook" &&
                data?.webhook_secret &&
                data?.webhook_url
            ) {
                setWebhookResult({
                    url: data.webhook_url,
                    secret: data.webhook_secret,
                });
                onCreated?.(triggerType);
            } else {
                onCreated?.(triggerType);
                onOpenChange(false);
            }
        } catch (e) {
            setError("Network error creating trigger");
        } finally {
            setSubmitting(false);
        }
    }

    function handleClose() {
        setWebhookResult(null);
        onOpenChange(false);
    }

    function copyToClipboard(value: string) {
        navigator.clipboard.writeText(value).catch(() => {
            /* clipboard unavailable — user can select manually */
        });
    }

    // ----- Render -------------------------------------------------------

    return (
        <Dialog
            open={open}
            onOpenChange={(o) => {
                if (!o) handleClose();
                else onOpenChange(true);
            }}
        >
            <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
                {webhookResult ? (
                    <>
                        <DialogHeader>
                            <DialogTitle>Webhook trigger created</DialogTitle>
                            <DialogDescription>
                                This secret will not be shown again. Copy the URL
                                and secret now, or rotate the secret later from
                                Settings → Triggers.
                            </DialogDescription>
                        </DialogHeader>
                        <div className="space-y-3">
                            <div>
                                <Label>Webhook URL</Label>
                                <div className="mt-1 flex gap-2">
                                    <Input readOnly value={webhookResult.url} />
                                    <Button
                                        type="button"
                                        variant="outline"
                                        size="icon"
                                        aria-label="Copy webhook URL"
                                        onClick={() => copyToClipboard(webhookResult.url)}
                                    >
                                        <Copy className="h-4 w-4" />
                                    </Button>
                                </div>
                            </div>
                            <div>
                                <Label>Secret</Label>
                                <div className="mt-1 flex gap-2">
                                    <Input
                                        readOnly
                                        value={webhookResult.secret}
                                        className="font-mono text-xs"
                                    />
                                    <Button
                                        type="button"
                                        variant="outline"
                                        size="icon"
                                        aria-label="Copy webhook secret"
                                        onClick={() => copyToClipboard(webhookResult.secret)}
                                    >
                                        <Copy className="h-4 w-4" />
                                    </Button>
                                </div>
                            </div>
                        </div>
                        <DialogFooter>
                            <Button onClick={handleClose}>Done</Button>
                        </DialogFooter>
                    </>
                ) : (
                    <>
                        <DialogHeader>
                            <DialogTitle>Add trigger</DialogTitle>
                            <DialogDescription>
                                {workflowName
                                    ? `Workflow: ${workflowName}`
                                    : `Workflow #${workflowId}`}
                            </DialogDescription>
                        </DialogHeader>

                        <div className="space-y-4">
                            {/* Trigger-type selector removed — cron schedule
                                is the only supported trigger type as of
                                launch-day-followup-2 (#16). The other types
                                (webhook, file_event, workflow_completion,
                                log_pattern_alert, queue_empty) still exist
                                in the codebase for the migration / audit
                                trail but cannot be created via UI or API.
                                triggerType is forced to "schedule" via the
                                useState default; setTriggerType is unused. */}
                            {triggerType === "schedule" && (
                                <div>
                                    <Label>Cron expression (UTC)</Label>
                                    <Input
                                        value={cron}
                                        onChange={(e) => setCron(e.target.value)}
                                        placeholder="0 9 * * *"
                                        className="mt-1"
                                    />
                                    <p className="mt-1 text-xs text-gray-500">
                                        Five fields: minute hour day month weekday.
                                    </p>
                                </div>
                            )}

                            {triggerType === "workflow_completion" && (
                                <div className="space-y-3">
                                    <div>
                                        <Label>Source workflow</Label>
                                        <Select
                                            value={completionSourceWorkflow}
                                            onValueChange={setCompletionSourceWorkflow}
                                        >
                                            <SelectTrigger aria-label="Source workflow" className="mt-1">
                                                <SelectValue placeholder="Select a workflow..." />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {workflows.map((w) => (
                                                    <SelectItem
                                                        key={w.id}
                                                        value={String(w.id)}
                                                    >
                                                        {w.name}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div>
                                        <Label>Fire on status</Label>
                                        <Select
                                            value={completionOnStatus}
                                            onValueChange={(v) =>
                                                setCompletionOnStatus(v as any)
                                            }
                                        >
                                            <SelectTrigger aria-label="Fire on status" className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {ON_STATUS_OPTIONS.map((s) => (
                                                    <SelectItem key={s} value={s}>
                                                        {s}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <Checkbox
                                            id="forward-outputs"
                                            checked={completionForwardOutputs}
                                            onCheckedChange={(c) =>
                                                setCompletionForwardOutputs(c === true)
                                            }
                                        />
                                        <Label htmlFor="forward-outputs">
                                            Forward source outputs to downstream inputs
                                        </Label>
                                    </div>
                                    <p className="text-xs text-gray-500">
                                        Best-effort match by port name. Mismatched keys
                                        are dropped and logged.
                                    </p>
                                </div>
                            )}

                            {triggerType === "webhook" && (
                                <div className="space-y-3">
                                    <p className="text-sm text-gray-600">
                                        On Save, a public firing URL and a one-time
                                        secret will be displayed. The secret is not
                                        recoverable later — copy it before dismissing
                                        the dialog.
                                    </p>
                                    <div className="flex items-center gap-2">
                                        <Checkbox
                                            id="webhook-fwd"
                                            checked={webhookForwardBody}
                                            onCheckedChange={(c) =>
                                                setWebhookForwardBody(c === true)
                                            }
                                        />
                                        <Label htmlFor="webhook-fwd">
                                            Forward POST body to workflow inputs
                                        </Label>
                                    </div>
                                </div>
                            )}

                            {triggerType === "file_event" && (
                                <div className="space-y-3">
                                    <div>
                                        <Label>Bucket</Label>
                                        <Input aria-label="Bucket"
                                            value={tenantBucket}
                                            readOnly
                                            className="mt-1 bg-gray-50"
                                        />
                                        <p className="mt-1 text-xs text-gray-500">
                                            Defaults to your tenant bucket.
                                        </p>
                                    </div>
                                    <div>
                                        <Label>Prefix</Label>
                                        <Input
                                            value={filePrefix}
                                            onChange={(e) => setFilePrefix(e.target.value)}
                                            placeholder="uploads/"
                                            className="mt-1"
                                        />
                                    </div>
                                    <div>
                                        <Label>Suffix</Label>
                                        <Input
                                            value={fileSuffix}
                                            onChange={(e) => setFileSuffix(e.target.value)}
                                            placeholder=".mp4"
                                            className="mt-1"
                                        />
                                    </div>
                                    <div>
                                        <Label>Glob</Label>
                                        <Input
                                            value={fileGlob}
                                            onChange={(e) => setFileGlob(e.target.value)}
                                            placeholder="*.mp4"
                                            className="mt-1"
                                        />
                                    </div>
                                    <div>
                                        <Label>Event type</Label>
                                        <Select
                                            value={fileEventType}
                                            onValueChange={(v) =>
                                                setFileEventType(v as any)
                                            }
                                        >
                                            <SelectTrigger aria-label="Event type" className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {FILE_EVENT_OPTIONS.map((s) => (
                                                    <SelectItem key={s} value={s}>
                                                        {s}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <Checkbox
                                            id="file-fwd"
                                            checked={fileForwardObjectPath}
                                            onCheckedChange={(c) =>
                                                setFileForwardObjectPath(c === true)
                                            }
                                        />
                                        <Label htmlFor="file-fwd">
                                            Forward object path to workflow input
                                        </Label>
                                    </div>
                                </div>
                            )}

                            {triggerType === "log_pattern_alert" && (
                                <div className="space-y-3">
                                    <div>
                                        <Label>Pattern (regex)</Label>
                                        <Input
                                            value={logPattern}
                                            onChange={(e) => setLogPattern(e.target.value)}
                                            placeholder="Webhook signature"
                                            className="mt-1"
                                        />
                                        {logRegexError && (
                                            <p className="mt-1 text-xs text-red-600">
                                                {logRegexError}
                                            </p>
                                        )}
                                        <p className="mt-1 text-xs text-amber-700">
                                            Warning: if the workflow you fire emits a log
                                            line matching this pattern, you create a
                                            recursion loop. The cooldown below is the
                                            safety net.
                                        </p>
                                    </div>
                                    <div>
                                        <Label>Log level</Label>
                                        <Select
                                            value={logLevel}
                                            onValueChange={(v) => setLogLevel(v as any)}
                                        >
                                            <SelectTrigger className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {LOG_LEVEL_OPTIONS.map((s) => (
                                                    <SelectItem key={s} value={s}>
                                                        {s}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div>
                                        <Label>Match field</Label>
                                        <Select
                                            value={logMatchField}
                                            onValueChange={(v) =>
                                                setLogMatchField(v as any)
                                            }
                                        >
                                            <SelectTrigger className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {LOG_FIELD_OPTIONS.map((s) => (
                                                    <SelectItem key={s} value={s}>
                                                        {s}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div>
                                        <Label>Cooldown (seconds)</Label>
                                        <Input
                                            type="number"
                                            min={1}
                                            value={logCooldown}
                                            onChange={(e) =>
                                                setLogCooldown(parseInt(e.target.value, 10) || 60)
                                            }
                                            className="mt-1"
                                        />
                                    </div>
                                </div>
                            )}

                            {triggerType === "message_bus" && (
                                <div className="space-y-3">
                                    <p className="text-sm text-gray-600">
                                        Fires the workflow for each message published
                                        to a broker topic. Only brokers with a live
                                        subscriber (redis, kafka, mqtt) can be
                                        selected; nats/rabbitmq are not implemented
                                        server-side.
                                    </p>
                                    <div>
                                        <Label>Transport</Label>
                                        <Select
                                            value={busProvider}
                                            onValueChange={(v) =>
                                                setBusProvider(v as any)
                                            }
                                        >
                                            <SelectTrigger className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {MESSAGE_BUS_PROVIDER_OPTIONS.map((p) => (
                                                    <SelectItem key={p} value={p}>
                                                        {p}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div>
                                        <Label>Topic</Label>
                                        <Input
                                            value={busTopic}
                                            onChange={(e) => setBusTopic(e.target.value)}
                                            placeholder={busProvider === "mqtt"
                                                ? "warehouse/+/arrivals"
                                                : "mod-arrivals"}
                                            className="mt-1"
                                        />
                                        {messageBusTopicError(busProvider, busTopic)
                                            && !busTopic.trim() ? (
                                                <p className="mt-1 text-xs text-red-600">
                                                    Topic is required.
                                                </p>
                                            ) : null}
                                        {busTopic.trim()
                                            && messageBusTopicError(busProvider, busTopic) ? (
                                                <p className="mt-1 text-xs text-red-600">
                                                    {messageBusTopicError(busProvider, busTopic)}
                                                </p>
                                            ) : null}
                                        <p className="mt-1 text-xs text-gray-500">
                                            {busProvider === "mqtt"
                                                ? "MQTT: 1-256 chars; '+' must fill a whole level, '#' only as the last level; $-prefixed topics refused."
                                                : busProvider === "kafka"
                                                    ? "Kafka: 1-249 chars of letters, digits, dot, underscore, hyphen."
                                                    : "Redis channel: any non-empty name."}
                                        </p>
                                    </div>
                                    <div>
                                        <Label>Broker URL (optional)</Label>
                                        <Input
                                            value={busUrl}
                                            onChange={(e) => setBusUrl(e.target.value)}
                                            placeholder={busProvider === "mqtt"
                                                ? "mqtt://broker.example:1883"
                                                : "kafka://broker.example:9092"}
                                            className="mt-1"
                                        />
                                        <p className="mt-1 text-xs text-gray-500">
                                            Leave blank to use the deployment default
                                            for this transport.
                                        </p>
                                    </div>
                                    <div>
                                        <Label>Broker auth</Label>
                                        <Select
                                            value={busAuth}
                                            onValueChange={(v) => setBusAuth(v as any)}
                                        >
                                            <SelectTrigger className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {MESSAGE_BUS_AUTH_OPTIONS.map((a) => (
                                                    <SelectItem key={a} value={a}>
                                                        {a === "none"
                                                            ? "No broker auth (plaintext)"
                                                            : "Use a stored secret"}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    {busAuth === "secret" && (
                                        <div>
                                            <Label>Secret reference</Label>
                                            <Input
                                                value={busSecretRef}
                                                onChange={(e) =>
                                                    setBusSecretRef(e.target.value)
                                                }
                                                placeholder="kafka-broker-creds"
                                                className="mt-1"
                                            />
                                            <p className="mt-1 text-xs text-gray-500">
                                                Name of a secret already stored in your
                                                tenant vault (the broker credentials
                                                object). Credentials are never typed or
                                                sent here — only this reference is.
                                            </p>
                                        </div>
                                    )}
                                    {busProvider === "kafka" && (
                                        <>
                                            <div className="flex items-center gap-2">
                                                <Checkbox
                                                    id="bus-tls"
                                                    checked={busTls}
                                                    onCheckedChange={(c) =>
                                                        setBusTls(c === true)
                                                    }
                                                />
                                                <Label htmlFor="bus-tls">
                                                    SASL over TLS (SASL_SSL)
                                                </Label>
                                            </div>
                                            <div>
                                                <Label>Consumer timeout (ms)</Label>
                                                <Input
                                                    type="number"
                                                    min={1000}
                                                    value={busConsumerTimeoutMs}
                                                    onChange={(e) =>
                                                        setBusConsumerTimeoutMs(
                                                            parseInt(e.target.value, 10)
                                                            || 10000,
                                                        )
                                                    }
                                                    className="mt-1"
                                                />
                                            </div>
                                        </>
                                    )}
                                </div>
                            )}

                            {triggerType === "queue_empty" && (
                                <div className="space-y-3">
                                    <div>
                                        <Label>Scope</Label>
                                        <Select
                                            value={queueScope}
                                            onValueChange={(v) =>
                                                setQueueScope(v as any)
                                            }
                                        >
                                            <SelectTrigger className="mt-1">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="tenant">
                                                    Tenant (all workflows)
                                                </SelectItem>
                                                <SelectItem value="workflow">
                                                    Workflow (specific)
                                                </SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    {queueScope === "workflow" && (
                                        <div>
                                            <Label>Watched workflow</Label>
                                            <Select
                                                value={queueWatchedWorkflow}
                                                onValueChange={setQueueWatchedWorkflow}
                                            >
                                                <SelectTrigger className="mt-1">
                                                    <SelectValue placeholder="Select a workflow..." />
                                                </SelectTrigger>
                                                <SelectContent>
                                                    {workflows.map((w) => (
                                                        <SelectItem
                                                            key={w.id}
                                                            value={String(w.id)}
                                                        >
                                                            {w.name}
                                                        </SelectItem>
                                                    ))}
                                                </SelectContent>
                                            </Select>
                                        </div>
                                    )}
                                    <div>
                                        <Label>Cooldown (seconds, min 30)</Label>
                                        <Input
                                            type="number"
                                            min={30}
                                            value={queueCooldown}
                                            onChange={(e) =>
                                                setQueueCooldown(
                                                    parseInt(e.target.value, 10) || 300,
                                                )
                                            }
                                            className="mt-1"
                                        />
                                        <p className="mt-1 text-xs text-gray-500">
                                            Prevents rapid re-firing on flapping queues.
                                        </p>
                                    </div>
                                </div>
                            )}

                            {error && (
                                <p className="text-sm text-red-600">{error}</p>
                            )}
                        </div>

                        <DialogFooter>
                            <Button variant="outline" onClick={handleClose}>
                                Cancel
                            </Button>
                            <Button onClick={handleSubmit} disabled={submitDisabled}>
                                {submitting ? "Saving..." : "Save"}
                            </Button>
                        </DialogFooter>
                    </>
                )}
            </DialogContent>
        </Dialog>
    );
}
