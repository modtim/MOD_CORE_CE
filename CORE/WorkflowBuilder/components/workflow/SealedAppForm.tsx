"use client";

/**
 * GG.8 — the Workflow Builder "app form" for SEALED workflows.
 *
 * A sealed installed copy (`WorkflowListing.licence == 'sealed'`) hides its
 * graph from the buyer (services/sealed_workflow.py, 403 "sealed" on every
 * graph-read endpoint) — but the buyer can still RUN the pipeline. Instead
 * of an empty/broken canvas, workflow-builder.tsx renders this component:
 * a simple inputs -> run -> outputs app form.
 *
 * Data comes from `GET /road/{id}/sealed-form` (name + the buyer's own
 * editable `sealed_inputs`, never the graph) and edits are saved back via
 * `PUT /road/{id}/sealed-inputs`. Running reuses the SAME run endpoint
 * every other workflow uses (`POST /road/workflow-versions/{id}/run`)
 * — the server expands the sealed listing's graph in-memory
 * (services.sealed_workflow.expand_for_run) and overlays these saved
 * values, so a purchase-required 402 or any other run guard behaves
 * identically to a normal run.
 *
 * Reuses WorkflowProgress (run status/progress polling, already redacts
 * node identity for sealed buyers server-side) and AssetPreview (output
 * previews) rather than re-implementing either.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Loader2, PlayCircle, Save } from "lucide-react";
import { WorkflowProgress } from "@/components/workflow-progress";
import { AssetPreview, type PreviewAsset } from "@/components/nodes/asset-preview";
import BlockedRunPanel from "@/components/workflow/BlockedRunPanel";
import { mapPreflightItems, type BlockedRunReason } from "@/lib/blocked-run";

export interface SealedInputEntry {
  node: string;
  configuration: Record<string, unknown>;
}

interface SealedFormData {
  workflow_id: number;
  name: string;
  sealed: boolean;
  version_id: number | null;
  sealed_inputs?: SealedInputEntry[];
}

interface SealedAppFormProps {
  workflowId: number;
  apiUrl: string;
  accessToken: string | null;
  /** True for platform/tenant admins (see the canMarketplace gate in
   *  workflow-builder.tsx) — hides the "Ask your admin" row. */
  isAdmin?: boolean;
}

type FieldKind = "boolean" | "number" | "text";

function fieldKind(value: unknown): FieldKind {
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  return "text";
}

// Heuristic ONLY — the API never tells us a field's node_kind (that would
// leak pipeline identity), so we can't know for certain a key is a file
// reference. Key names the buyer already sees in their own sealed_inputs
// are safe to look at; this just picks a friendlier label/placeholder.
function looksLikeFileField(key: string): boolean {
  return /file|image|asset|cog|path|video|audio/i.test(key);
}

export default function SealedAppForm({ workflowId, apiUrl, accessToken, isAdmin = false }: SealedAppFormProps) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<SealedFormData | null>(null);
  // node id -> key -> value, seeded from form.sealed_inputs, edited in place.
  const [values, setValues] = useState<Record<string, Record<string, unknown>>>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [blockedReasons, setBlockedReasons] = useState<BlockedRunReason[]>([]);
  const [runId, setRunId] = useState<number | null>(null);
  const [outputs, setOutputs] = useState<PreviewAsset[]>([]);

  const authHeaders = useMemo(
    () => ({ Authorization: `Bearer ${accessToken}`, Accept: "application/json" }),
    [accessToken],
  );

  useEffect(() => {
    let cancelled = false;
    if (!accessToken || !workflowId) return;
    setLoading(true);
    setLoadError(null);
    setForm(null);
    setRunId(null);
    setOutputs([]);
    setRunError(null);
    setBlockedReasons([]);
    (async () => {
      try {
        const res = await fetch(`${apiUrl}/road/${workflowId}/sealed-form`, { headers: authHeaders });
        if (!res.ok) {
          if (!cancelled) setLoadError(`Could not load this app (${res.status}).`);
          return;
        }
        const data: SealedFormData = await res.json();
        if (cancelled) return;
        setForm(data);
        const seeded: Record<string, Record<string, unknown>> = {};
        for (const entry of data.sealed_inputs || []) {
          seeded[entry.node] = { ...(entry.configuration || {}) };
        }
        setValues(seeded);
      } catch {
        if (!cancelled) setLoadError("Could not load this app — the request failed.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiUrl, workflowId, accessToken, authHeaders]);

  const setFieldValue = useCallback((node: string, key: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [node]: { ...(prev[node] || {}), [key]: value } }));
    setDirty(true);
  }, []);

  const save = useCallback(async (): Promise<boolean> => {
    if (!form) return false;
    setSaving(true);
    try {
      const payload = {
        sealed_inputs: Object.entries(values).map(([node, configuration]) => ({ node, configuration })),
      };
      const res = await fetch(`${apiUrl}/road/${workflowId}/sealed-inputs`, {
        method: "PUT",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) return false;
      const data = await res.json().catch(() => null);
      if (data?.sealed_inputs) {
        const seeded: Record<string, Record<string, unknown>> = {};
        for (const entry of data.sealed_inputs as SealedInputEntry[]) {
          seeded[entry.node] = { ...(entry.configuration || {}) };
        }
        setValues(seeded);
      }
      setDirty(false);
      return true;
    } catch {
      return false;
    } finally {
      setSaving(false);
    }
  }, [apiUrl, workflowId, authHeaders, values, form]);

  const fetchOutputs = useCallback(
    async (completedRunId: number) => {
      try {
        const res = await fetch(`${apiUrl}/runs/${completedRunId}/previews`, { headers: authHeaders });
        if (!res.ok) return;
        const previews: Record<string, PreviewAsset[]> = await res.json();
        const flat = Object.values(previews || {}).flat();
        setOutputs(flat);
      } catch {
        // best-effort — a missing preview list just means no output shows
      }
    },
    [apiUrl, authHeaders],
  );

  const run = useCallback(async () => {
    if (!form || !form.version_id) return;
    setRunError(null);
    setBlockedReasons([]);
    setOutputs([]);
    setRunning(true);
    try {
      if (dirty) {
        const ok = await save();
        if (!ok) {
          setRunError("Could not save your inputs — run cancelled.");
          return;
        }
      }
      const res = await fetch(`${apiUrl}/road/workflow-versions/${form.version_id}/run`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ cog_ids: [] }),
      });
      if (res.status === 402) {
        const body = await res.json().catch(() => ({} as any));
        const detail = body?.detail;
        if (detail && detail.code === "purchase_required") {
          setBlockedReasons(mapPreflightItems(detail.items));
          return;
        }
        setRunError(typeof detail === "string" ? detail : "This run needs items you don't own yet.");
        return;
      }
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setRunError(text || `Run failed to start (${res.status}).`);
        return;
      }
      const result = await res.json();
      const newRunId = result.workflow_run_id ?? result.run_id ?? result.id ?? null;
      if (newRunId) setRunId(newRunId);
    } catch {
      setRunError("Run failed to start — the request failed.");
    } finally {
      setRunning(false);
    }
  }, [form, dirty, save, apiUrl, authHeaders]);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading app…
      </div>
    );
  }

  if (loadError || !form) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground p-6 text-center">
        {loadError || "This app is unavailable."}
      </div>
    );
  }

  const entries = form.sealed_inputs || [];

  return (
    <div className="flex-1 overflow-y-auto p-6" data-testid="sealed-app-form">
      <div className="max-w-xl mx-auto space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>{form.name}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {entries.length === 0 && (
              <p className="text-sm text-muted-foreground">This app has no editable inputs — just click Run.</p>
            )}
            {entries.map((entry) => (
              <div key={entry.node} className="space-y-3" data-testid={`sealed-input-node-${entry.node}`}>
                {Object.entries(values[entry.node] || {}).map(([key, value]) => {
                  const kind = fieldKind(value);
                  const fieldId = `sealed-${entry.node}-${key}`;
                  return (
                    <div key={fieldId} className="space-y-1">
                      <Label htmlFor={fieldId}>{key}{looksLikeFileField(key) ? " (file reference)" : ""}</Label>
                      {kind === "boolean" ? (
                        <Checkbox
                          id={fieldId}
                          checked={Boolean(value)}
                          onCheckedChange={(checked) => setFieldValue(entry.node, key, Boolean(checked))}
                        />
                      ) : kind === "number" ? (
                        <Input
                          id={fieldId}
                          type="number"
                          value={value as number}
                          onChange={(e) => setFieldValue(entry.node, key, e.target.valueAsNumber)}
                        />
                      ) : (
                        <Input
                          id={fieldId}
                          type="text"
                          value={(value as string) ?? ""}
                          onChange={(e) => setFieldValue(entry.node, key, e.target.value)}
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            ))}

            <div className="flex gap-2 pt-2">
              {entries.length > 0 && (
                <Button variant="outline" onClick={() => void save()} disabled={saving || !dirty}>
                  <Save className="h-4 w-4 mr-2" />
                  {saving ? "Saving…" : "Save"}
                </Button>
              )}
              <Button onClick={() => void run()} disabled={running}>
                <PlayCircle className="h-4 w-4 mr-2" />
                {running ? "Starting…" : "Run"}
              </Button>
            </div>

            {runError && <p className="text-sm text-destructive">{runError}</p>}
            {blockedReasons.length > 0 && <BlockedRunPanel reasons={blockedReasons} isAdmin={isAdmin} />}
          </CardContent>
        </Card>

        {runId && (
          <WorkflowProgress
            workflowRunId={runId}
            accessToken={accessToken}
            onComplete={() => void fetchOutputs(runId)}
            isAdmin={isAdmin}
          />
        )}

        {outputs.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Outputs</CardTitle>
            </CardHeader>
            <CardContent>
              <AssetPreview assets={outputs} />
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
