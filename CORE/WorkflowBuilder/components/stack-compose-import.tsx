"use client";

// Stack wizard — docker-compose import panel (stack-wizard-compose-import).
//
// Bring-your-own docker-compose on-ramp for the Stack Wizard: paste or upload a
// compose file, preview the MOD `x-mod` metadata the wizard needs confirmed
// (accelerator / egress / readiness / ha_class / storage_class), answer the
// required ones, then finalize into a normal PersistentStack. The pure
// preview/confirmation glue lives in lib/compose-import.ts — this component is
// just the two-stage (paste → confirm) UI wrapper around it and the two
// `/stacks/import-compose[/finalize]` calls.

import React, { useState } from "react";
import { AlertTriangle, Loader2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Confirmations,
  ComposeImportPreview,
  EgressConfirmation,
  FinalizeRequest,
  NeedItem,
  ServiceConfirmation,
  buildFinalizeRequest,
  outstandingRequired,
  seedConfirmations,
} from "@/lib/compose-import";
import {
  BackupAckDetail,
  classifyBackupAckResponse,
  formatBackupAckServices,
  withBackupAck,
} from "@/lib/stack-backup-ack";

const EGRESS_MODES = ["none", "api", "hosts"] as const;

/** Shape of the created stack this component hands back — a structural subset
 * of stack-wizard-panel's CreatedStack so `onCreated` slots in unmodified. */
interface ImportedStack {
  id: number;
  name: string;
  status?: string;
  status_reason?: string;
  storage_class?: string;
  compose_spec?: { services?: Record<string, unknown> };
}

interface Props {
  authFetch: (path: string, init?: RequestInit) => Promise<Response>;
  onCreated: (stack: ImportedStack) => void;
}

type Stage = "paste" | "confirm";

async function readErrorDetail(resp: Response): Promise<string> {
  try {
    const data = await resp.json();
    if (typeof data?.detail === "string") return data.detail;
  } catch {
    // fall through to a generic message
  }
  return `HTTP ${resp.status}`;
}

export function StackComposeImport({ authFetch, onCreated }: Props) {
  const [name, setName] = useState("");
  const [compose, setCompose] = useState("");
  const [stage, setStage] = useState<Stage>("paste");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ComposeImportPreview | null>(null);
  const [confirmations, setConfirmations] = useState<Confirmations>({ services: {} });

  // GA.ASW.6b — "no backup plan" acknowledgement. A finalize that 409s with
  // requires_ack pauses here instead of surfacing a plain error; confirming
  // re-POSTs pendingFinalizeBody unchanged with acknowledge_no_backup: true.
  const [backupAck, setBackupAck] = useState<BackupAckDetail | null>(null);
  const [backupAckChecked, setBackupAckChecked] = useState(false);
  const [pendingFinalizeBody, setPendingFinalizeBody] = useState<FinalizeRequest | null>(null);

  const canPreview = name.trim().length > 0 && compose.trim().length > 0 && !busy;

  const onFile = (file: File | undefined) => {
    if (!file) return;
    file
      .text()
      .then(setCompose)
      .catch(() => setError("Could not read that file."));
  };

  const runPreview = async () => {
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch("/stacks/import-compose", {
        method: "POST",
        body: JSON.stringify({ name, compose }),
      });
      if (!resp.ok) {
        setError(await readErrorDetail(resp));
        return;
      }
      const data = (await resp.json()) as ComposeImportPreview;
      setPreview(data);
      setConfirmations(seedConfirmations(data));
      setStage("confirm");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Preview failed.");
    } finally {
      setBusy(false);
    }
  };

  const needs = preview?.needs_confirmation ?? [];
  const outstanding = outstandingRequired(needs, confirmations);

  const setService = (service: string, patch: Partial<ServiceConfirmation>) => {
    setConfirmations((prev) => ({
      ...prev,
      services: {
        ...prev.services,
        [service]: { ...(prev.services?.[service] || {}), ...patch },
      },
    }));
  };

  const submitFinalize = async (body: FinalizeRequest) => {
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch("/stacks/import-compose/finalize", {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const { backupAck: ack, message } = await classifyBackupAckResponse(resp);
        if (ack) {
          setPendingFinalizeBody(body);
          setBackupAck(ack);
          setBackupAckChecked(false);
          return;
        }
        setError(message);
        return;
      }
      const stack = (await resp.json()) as ImportedStack;
      setBackupAck(null);
      setPendingFinalizeBody(null);
      onCreated(stack);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Finalize failed.");
    } finally {
      setBusy(false);
    }
  };

  const finalize = () => {
    if (!preview) return;
    void submitFinalize(buildFinalizeRequest(name, compose, confirmations));
  };

  const confirmBackupAck = () => {
    if (!pendingFinalizeBody || !backupAckChecked || busy) return;
    void submitFinalize(withBackupAck(pendingFinalizeBody));
  };

  const cancelBackupAck = () => {
    setBackupAck(null);
    setPendingFinalizeBody(null);
    setBackupAckChecked(false);
  };

  return (
    <div className="space-y-3" data-testid="stack-compose-import">
      {stage === "paste" && (
        <div className="space-y-2.5">
          <div className="space-y-1">
            <label className="text-xs font-medium text-gray-700">
              Stack name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              aria-label="Stack name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-imported-stack"
              disabled={busy}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
              autoComplete="off"
            />
          </div>
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <label className="text-xs font-medium text-gray-700">
                docker-compose.yml <span className="text-red-500">*</span>
              </label>
              <label className="inline-flex items-center gap-1.5 text-[11px] text-indigo-600 hover:text-indigo-700 cursor-pointer">
                <Upload className="h-3.5 w-3.5" />
                Upload file
                <input
                  type="file"
                  accept=".yml,.yaml"
                  className="hidden"
                  disabled={busy}
                  onChange={(e) => onFile(e.target.files?.[0])}
                />
              </label>
            </div>
            <textarea
              aria-label="docker-compose.yml"
              value={compose}
              onChange={(e) => setCompose(e.target.value)}
              placeholder={"services:\n  app:\n    image: my/image:latest"}
              disabled={busy}
              rows={10}
              className="w-full px-3 py-2 text-xs font-mono border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
            />
          </div>
          <div className="flex justify-end">
            <Button
              type="button"
              size="sm"
              disabled={!canPreview}
              onClick={runPreview}
              data-testid="compose-import-preview"
              className="bg-indigo-600 hover:bg-indigo-700 text-white"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Preview"}
            </Button>
          </div>
        </div>
      )}

      {stage === "confirm" && preview && (
        <div className="space-y-3">
          <p className="text-[11px] text-gray-500">
            Confirm the MOD metadata this compose can&apos;t express. Required
            answers are marked — everything else keeps its suggested value.
          </p>
          <div className="space-y-2.5 max-h-[320px] overflow-y-auto pr-1">
            {needs.map((need, i) => (
              <NeedRow
                key={`${need.scope}-${need.service ?? "stack"}-${need.field}-${i}`}
                need={need}
                confirmations={confirmations}
                onServiceChange={setService}
                onStackChange={(patch) =>
                  setConfirmations((prev) => ({ ...prev, ...patch }))
                }
              />
            ))}
          </div>
          <div className="flex items-center justify-between">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => setStage("paste")}
            >
              Back
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={busy || outstanding.length > 0}
              onClick={finalize}
              data-testid="compose-import-save"
              className="bg-indigo-600 hover:bg-indigo-700 text-white"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Save"}
            </Button>
          </div>
        </div>
      )}

      {error && (
        <div
          className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-800 flex items-start gap-2"
          data-testid="compose-import-error"
        >
          <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />
          <span className="whitespace-pre-wrap break-words">{error}</span>
        </div>
      )}

      {backupAck && (
        <Alert variant="destructive" data-testid="compose-import-backup-ack">
          <AlertTriangle className="h-3.5 w-3.5" />
          <AlertDescription className="space-y-2">
            <p className="text-xs">
              These databases have no backup plan: {formatBackupAckServices(backupAck)}
            </p>
            <p className="text-[10px] text-muted-foreground">{backupAck.reason}</p>
            <div className="flex items-center gap-2">
              <Checkbox
                id="compose-import-backup-ack-checkbox"
                data-testid="compose-import-backup-ack-checkbox"
                checked={backupAckChecked}
                onCheckedChange={(c) => setBackupAckChecked(c === true)}
              />
              <label htmlFor="compose-import-backup-ack-checkbox" className="text-xs">
                I understand this data is not backed up
              </label>
            </div>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={cancelBackupAck}
                data-testid="compose-import-backup-ack-cancel"
              >
                Cancel
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={busy || !backupAckChecked}
                onClick={confirmBackupAck}
                data-testid="compose-import-backup-ack-confirm"
                className="bg-red-600 hover:bg-red-700 text-white"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Create anyway"}
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}

interface NeedRowProps {
  need: NeedItem;
  confirmations: Confirmations;
  onServiceChange: (service: string, patch: Partial<ServiceConfirmation>) => void;
  onStackChange: (patch: Partial<Confirmations>) => void;
}

function NeedRow({ need, confirmations, onServiceChange, onStackChange }: NeedRowProps) {
  const testId = `need-${need.scope}-${need.service ?? "stack"}-${need.field}`;
  const label = need.service ? `${need.service} — ${need.field}` : need.field;

  let control: React.ReactNode = null;

  if (need.scope === "stack" && need.field === "storage_class") {
    control = (
      <select
        aria-label={label}
        data-testid={testId}
        value={confirmations.storage_class || ""}
        onChange={(e) => onStackChange({ storage_class: e.target.value })}
        className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md bg-white"
      >
        <option value="">Choose…</option>
        {(need.options as string[] | undefined)?.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  } else if (need.scope === "stack" && need.field === "ha_class") {
    control = (
      <select
        aria-label={label}
        data-testid={testId}
        value={confirmations.ha_class || ""}
        onChange={(e) => onStackChange({ ha_class: e.target.value })}
        className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md bg-white"
      >
        <option value="">Choose…</option>
        {(need.options as string[] | undefined)?.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  } else if (need.scope === "service" && need.service) {
    const service = need.service;
    const svc = confirmations.services?.[service] || {};

    if (need.field === "accelerator") {
      const kind = svc.accelerator?.kind || "none";
      control = (
        <select
          aria-label={label}
          data-testid={testId}
          value={kind}
          onChange={(e) => {
            const v = e.target.value;
            onServiceChange(
              service,
              v === "none"
                ? { accelerator: null }
                : { accelerator: { ...(svc.accelerator || {}), required: true, kind: v } },
            );
          }}
          className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md bg-white"
        >
          {(need.options as string[] | undefined)?.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
    } else if (need.field === "accelerator.min_vram_gb") {
      control = (
        <input
          type="number"
          aria-label={label}
          data-testid={testId}
          min={1}
          value={svc.accelerator?.min_vram_gb ?? ""}
          onChange={(e) => {
            const v = e.target.value === "" ? null : Number(e.target.value);
            onServiceChange(service, {
              accelerator: { ...(svc.accelerator || { required: true, kind: "cuda" }), min_vram_gb: v },
            });
          }}
          className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md"
        />
      );
    } else if (need.field === "egress") {
      const mode = svc.egress?.mode || "none";
      control = (
        <div className="space-y-1.5">
          <select
            aria-label={label}
            data-testid={testId}
            value={mode}
            onChange={(e) =>
              onServiceChange(service, {
                egress: {
                  ...(svc.egress || {}),
                  mode: e.target.value as EgressConfirmation["mode"],
                },
              })
            }
            className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md bg-white"
          >
            {EGRESS_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          {mode === "hosts" && (
            <input
              type="text"
              aria-label={`${label} hosts`}
              data-testid={`${testId}-hosts`}
              placeholder="host1.example.com, host2.example.com"
              value={(svc.egress?.hosts || []).join(", ")}
              onChange={(e) =>
                onServiceChange(service, {
                  egress: {
                    mode: "hosts",
                    hosts: e.target.value
                      .split(",")
                      .map((h) => h.trim())
                      .filter(Boolean),
                  },
                })
              }
              className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md"
            />
          )}
        </div>
      );
    } else if (need.field === "readiness.path") {
      control = (
        <input
          type="text"
          aria-label={label}
          data-testid={testId}
          value={svc.readiness?.path ?? ""}
          onChange={(e) =>
            onServiceChange(service, { readiness: { ...(svc.readiness || {}), path: e.target.value } })
          }
          className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-md"
        />
      );
    } else if (need.field === "readiness") {
      control = (
        <div className="flex gap-1.5">
          <input
            type="number"
            aria-label={`${label} port`}
            data-testid={`${testId}-port`}
            placeholder="port"
            value={svc.readiness?.port ?? ""}
            onChange={(e) =>
              onServiceChange(service, {
                readiness: {
                  ...(svc.readiness || {}),
                  port: e.target.value === "" ? undefined : Number(e.target.value),
                },
              })
            }
            className="w-20 px-2 py-1.5 text-xs border border-gray-300 rounded-md"
          />
          <input
            type="text"
            aria-label={`${label} path`}
            data-testid={`${testId}-path`}
            placeholder="path"
            value={svc.readiness?.path ?? ""}
            onChange={(e) =>
              onServiceChange(service, { readiness: { ...(svc.readiness || {}), path: e.target.value } })
            }
            className="flex-1 px-2 py-1.5 text-xs border border-gray-300 rounded-md"
          />
        </div>
      );
    }
  }

  return (
    <div className="rounded-md border border-gray-200 px-2.5 py-2 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-gray-700">{label}</span>
        {need.required && <span className="text-[10px] font-medium text-red-500">required</span>}
      </div>
      <p className="text-[10px] text-gray-400">{need.reason}</p>
      {control}
    </div>
  );
}
