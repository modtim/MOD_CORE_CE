"use client";

// Service / Stack Builder modal (service-stack-builder, Group F).
//
// Rebuilt from the old two-mode "template / describe → deploy" popup into a
// multi-service *builder*. Keeps the same modal shell (centered Radix Dialog)
// and open/close/accessToken props so the call site is unchanged.
//
// Shape (design.md): a left Services list + right per-service editor
// (source + name + env/ports/volumes/GPU/replicas + an "expose publicly"
// toggle — all owned by <ServiceEditor>), a persistent cost line across the
// bottom, and a Review → Deploy step.
//
//   Build  — compose N services; Template/Describe are one-click on-ramps that
//            PRE-FILL the builder (not a direct deploy).
//   Review — POST /persistent-stacks/assemble; a 422 shows the validator's
//            reason inline (stay here); success shows the assembled compose.
//   Deploy — POST /persistent-stacks (create) then POST /{id}/deploy, then the
//            stack overview: each service's ports, exposure state, and magic URL.

import { wbNavLabel } from "@/lib/product-names";
import React, { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  BackupAckDetail,
  classifyBackupAckResponse,
  formatBackupAckServices,
  withBackupAck,
} from "@/lib/stack-backup-ack";
import {
  Boxes,
  Loader2,
  Rocket,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  AlertTriangle,
  Sparkles,
  LayoutTemplate,
  Layers,
  Link2,
  Pencil,
  History,
  Save,
  Undo2,
  FileUp,
} from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import { StackComposeImport } from "./stack-compose-import";

import { ServiceList } from "./service-builder/ServiceList";
import { ServiceEditor } from "./service-builder/ServiceEditor";
// CE: the estimated-cost line is not part of CE () - no CostLine import
import {
  ServiceSpec,
  ServiceSource,
  AssembleResponse,
  blankService,
} from "./service-builder/types";

const API_URL = getApiUrl();

// ---- Local types (post-create / on-ramp only; contracts live in types.ts) --

interface TemplateParam {
  key: string;
  label: string;
  default?: string;
}

interface StackTemplate {
  id: string;
  name: string;
  description?: string;
  storage_class?: string;
  params: TemplateParam[];
}

interface ComposeShape {
  services?: Record<string, unknown>;
}

interface CreatedStack {
  id: number;
  name: string;
  status?: string;
  status_reason?: string;
  storage_class?: string;
  compose_spec?: ComposeShape;
}

interface MeshEndpoint {
  magicdns_name?: string;
  status?: string;
  node_id?: string;
}

interface StackHealth {
  grade: string;
  reason?: string | null;
}

// StackGradeBadge — the came-up-healthy verdict (healthy / pending / unhealthy)
// as a colour-coded pill, shared by the wizard's Done overview and the
// edit-existing-stack picker so both surfaces read the SAME grade the API
// reports (CORE/API/schemas.py:3555 StackHealth, GET /stacks/{id} + the
// GET /stacks list). Palette mirrors the Dashboard's _psHealthBadge
// (CORE/Dashboard/js/persistent-stacks.js:523). No grade -> renders nothing.
export function StackGradeBadge(health?: StackHealth | null) {
  const grade = health?.grade;
  if (!grade) return null;
  const title = health?.reason
    ? `Health grade: ${grade} — ${health.reason}`
    : `Health grade: ${grade}`;
  const style = grade === "unhealthy"
    ? "bg-red-100 text-red-700"
    : grade === "pending"
    ? "bg-amber-100 text-amber-700"
    : "bg-emerald-100 text-emerald-700";
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium ${style}`}
    >
      {grade}
    </span>
  );
}

interface EndpointInfo {
  stack_id: number;
  name: string;
  status?: string;
  status_reason?: string | null;
  endpoint?: string | null;
  reachable?: boolean;
  reach_mode?: string | null;
  running_unreachable?: boolean;
  mesh_endpoints?: Record<string, MeshEndpoint>;
  health?: StackHealth | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  // Accepted for parity with the other wizard panels. Auth is actually carried
  // by authenticatedFetch (reads/refreshes the stored token), so this is unused
  // here — kept so the call site matches the siblings.
  accessToken?: string | null;
}

type Step = "build" | "review" | "done";
type OnRamp = "none" | "template" | "describe" | "import";

// stack-definition-edit — everyone-facing stack composition+config editor,
// added onto the SAME "Stack Wizard" dialog rather than a new page/dialog
// (per owner UI guidance: reuse existing surfaces, one Node/Stack distinction,
// no parallel screen). Picking "Edit an existing stack" in the Build step
// switches this dialog into edit mode: services/on-ramps behave the same, but
// Review's action becomes Save (PUT /stacks/{id}/definition — a new revision,
// nothing overwritten) instead of Create+Deploy, and Done offers a History
// panel (list / diff / revert, mirroring node-code-edit's revision UX) plus a
// "Redeploy now" button (the existing POST /stacks/{id}/deploy).
interface ExistingStackListItem {
  id: number;
  name: string;
  status?: string;
  // GET /stacks carries the same StackHealth DTO as GET /stacks/{id}
  // (CORE/API/schemas.py:3555 `health`), so the picker can show the grade
  // without a per-stack round-trip. Absent on older deployments -> no badge.
  health?: StackHealth | null;
}

interface RevisionSummary {
  revision_no: number;
  origin: string;
  author_user_id?: number | null;
  message?: string | null;
  created_at?: string | null;
}

interface DefinitionResponse {
  stack_id: number;
  name: string;
  services: ServiceSpec[];
  revisions: RevisionSummary[];
  current_revision_no: number;
}

// ---- Best-effort compose → ServiceSpec mapping (Template / Describe pre-fill)

function envToRecord(environment: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (Array.isArray(environment)) {
    for (const item of environment) {
      if (typeof item !== "string") continue;
      const idx = item.indexOf("=");
      if (idx >= 0) out[item.slice(0, idx)] = item.slice(idx + 1);
      else out[item] = "";
    }
  } else if (environment && typeof environment === "object") {
    for (const [k, v] of Object.entries(environment as Record<string, unknown>)) {
      out[k] = v == null ? "" : String(v);
    }
  }
  return out;
}

function portsToContainerPorts(ports: unknown): number[] {
  const out: number[] = [];
  if (!Array.isArray(ports)) return out;
  for (const p of ports) {
    if (typeof p === "number") {
      out.push(p);
    } else if (typeof p === "string") {
      // "8080", "8080:8080", "127.0.0.1:8080:8080", "8080/tcp" → container port.
      const parts = p.split(":");
      const tail = parts[parts.length - 1].split("/")[0];
      const n = parseInt(tail, 10);
      if (!Number.isNaN(n)) out.push(n);
    } else if (p && typeof p === "object") {
      const target = (p as Record<string, unknown>).target;
      const n = typeof target === "number" ? target : parseInt(String(target), 10);
      if (!Number.isNaN(n)) out.push(n);
    }
  }
  return out;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

function composeToServices(compose: ComposeShape | undefined): ServiceSpec[] {
  const services = compose?.services;
  if (!services || typeof services !== "object") return [];
  return Object.entries(services).map(([name, raw]) => {
    const svc = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    const spec = blankService(name);
    // Every mapped source lands as an editable `image` service — the operator
    // refines it before Deploy. (git/script/site/node sources are composed by
    // hand in the builder, not reverse-mapped.)
    const source: ServiceSource = {
      kind: "image",
      image: typeof svc.image === "string" ? svc.image : "",
    };
    spec.source = source;
    spec.env = envToRecord(svc.environment);
    spec.ports = portsToContainerPorts(svc.ports);
    spec.volumes = stringList(svc.volumes);
    if (typeof svc.command === "string") spec.command = svc.command;
    else if (Array.isArray(svc.command)) spec.command = svc.command.map(String).join(" ");
    return spec;
  });
}

async function surfaceError(resp: Response, fallback: string): Promise<string> {
  try {
    const body = await resp.json();
    if (body?.detail) {
      return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    /* not JSON */
  }
  return fallback;
}

export function StackWizardPanel({ open, onClose }: Props) {
  // ---- Builder state -------------------------------------------------------
  const [step, setStep] = useState<Step>("build");
  const [name, setName] = useState("");
  const [services, setServices] = useState<ServiceSpec[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---- Review / deploy -----------------------------------------------------
  const [assembled, setAssembled] = useState<AssembleResponse | null>(null);
  const [validatorError, setValidatorError] = useState<string | null>(null);
  const [createdStack, setCreatedStack] = useState<CreatedStack | null>(null);
  const [endpointInfo, setEndpointInfo] = useState<EndpointInfo | null>(null);

  // GA.ASW.6b — "no backup plan" acknowledgement. A create (POST /stacks)
  // that 409s with requires_ack pauses Deploy here instead of surfacing a
  // plain error; confirming re-POSTs pendingCreateBody unchanged with
  // acknowledge_no_backup: true, then continues into the same deploy step.
  const [backupAck, setBackupAck] = useState<BackupAckDetail | null>(null);
  const [backupAckChecked, setBackupAckChecked] = useState(false);
  const [pendingCreateBody, setPendingCreateBody] = useState<{
    name: string;
    compose_spec: unknown;
  } | null>(null);

  // ---- On-ramps (Template / Describe pre-fill) -----------------------------
  const [onRamp, setOnRamp] = useState<OnRamp>("none");
  const [templates, setTemplates] = useState<StackTemplate[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>("");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [prompt, setPrompt] = useState("");
  const [prefillBusy, setPrefillBusy] = useState(false);
  const [prefillError, setPrefillError] = useState<string | null>(null);

  // ---- Edit-an-existing-stack mode (stack-definition-edit) ------------------
  const [editStackId, setEditStackId] = useState<number | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [existingStacks, setExistingStacks] = useState<ExistingStackListItem[]>([]);
  const [existingStacksLoading, setExistingStacksLoading] = useState(false);
  const [existingStacksError, setExistingStacksError] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<RevisionSummary[]>([]);
  const [saveMessage, setSaveMessage] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [diffFor, setDiffFor] = useState<number | null>(null);
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffBusy, setDiffBusy] = useState(false);
  const [revertBusy, setRevertBusy] = useState(false);
  const [redeployBusy, setRedeployBusy] = useState(false);
  const [redeployDone, setRedeployDone] = useState(false);
  const isEditing = editStackId != null;

  const authFetch = useCallback(
    (path: string, init?: RequestInit) =>
      authenticatedFetch(`${API_URL}${path}`, {
        ...init,
        headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      }),
    [],
  );

  const resetAll = useCallback(() => {
    setStep("build");
    setName("");
    setServices([]);
    setSelectedIndex(0);
    setBusy(false);
    setError(null);
    setAssembled(null);
    setValidatorError(null);
    setCreatedStack(null);
    setEndpointInfo(null);
    setOnRamp("none");
    setSelectedTemplateId("");
    setParamValues({});
    setPrompt("");
    setPrefillError(null);
    setEditStackId(null);
    setPickerOpen(false);
    setRevisions([]);
    setSaveMessage("");
    setHistoryOpen(false);
    setDiffFor(null);
    setDiffText(null);
    setRedeployDone(false);
  }, []);

  // Templates load when the popup opens (on-ramp is optional and must not block
  // hand-composing a stack if the fetch fails).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setTemplatesLoading(true);
    (async () => {
      try {
        const resp = await authFetch("/stacks/templates");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: StackTemplate[] = await resp.json();
        if (!cancelled) setTemplates(Array.isArray(data) ? data : []);
      } catch {
        if (!cancelled) setTemplates([]);
      } finally {
        if (!cancelled) setTemplatesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, authFetch]);

  const selectedTemplate =
    templates.find((t) => t.id === selectedTemplateId) || null;

  const onPickTemplate = (id: string) => {
    setSelectedTemplateId(id);
    const tpl = templates.find((t) => t.id === id);
    const next: Record<string, string> = {};
    (tpl?.params || []).forEach((p) => {
      next[p.key] = p.default ?? "";
    });
    setParamValues(next);
  };

  // ---- Service list wiring -------------------------------------------------

  const handleAdd = (svc: ServiceSpec) => {
    setServices((prev) => [...prev, svc]);
    setSelectedIndex(services.length); // new row is appended at the old length
  };

  const handleRemove = (index: number) => {
    setServices((prev) => prev.filter((_, i) => i !== index));
    setSelectedIndex((prev) => {
      if (index < prev) return prev - 1;
      if (index === prev) return Math.max(0, prev - 1);
      return prev;
    });
  };

  const clampedIndex = Math.min(selectedIndex, Math.max(0, services.length - 1));
  const selectedService = services[clampedIndex];

  const handleEditorChange = (updated: ServiceSpec) => {
    setServices((prev) => prev.map((s, i) => (i === clampedIndex ? updated : s)));
  };

  // ---- On-ramp pre-fill ----------------------------------------------------

  const applyPrefill = (compose: ComposeShape | undefined) => {
    const mapped = composeToServices(compose);
    if (mapped.length === 0) {
      setPrefillError(
        "That produced no services to pre-fill. Compose the stack by hand instead.",
      );
      return;
    }
    setServices(mapped);
    setSelectedIndex(0);
    setOnRamp("none");
    setPrefillError(null);
  };

  const prefillFromTemplate = async () => {
    if (!selectedTemplate || !name.trim() || prefillBusy) return;
    setPrefillBusy(true);
    setPrefillError(null);
    try {
      const resp = await authFetch("/stacks/from-template", {
        method: "POST",
        body: JSON.stringify({
          template_id: selectedTemplate.id,
          name: name.trim(),
          params: paramValues,
          preview: true,
        }),
      });
      if (!resp.ok) {
        throw new Error(
          await surfaceError(resp, `Could not load the template (HTTP ${resp.status}).`),
        );
      }
      const stack: CreatedStack = await resp.json();
      applyPrefill(stack.compose_spec);
    } catch (e) {
      setPrefillError((e as Error).message);
    } finally {
      setPrefillBusy(false);
    }
  };

  const prefillFromDescribe = async () => {
    if (!name.trim() || !prompt.trim() || prefillBusy) return;
    setPrefillBusy(true);
    setPrefillError(null);
    try {
      const resp = await authFetch("/stacks/from-wizard", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), prompt: prompt.trim() }),
      });
      if (resp.status === 502) {
        throw new Error(
          "The AI composer is unavailable right now. Try a template, or compose the stack by hand.",
        );
      }
      if (!resp.ok) {
        throw new Error(
          await surfaceError(resp, `Could not compose the stack (HTTP ${resp.status}).`),
        );
      }
      const stack: CreatedStack = await resp.json();
      applyPrefill(stack.compose_spec);
    } catch (e) {
      setPrefillError((e as Error).message);
    } finally {
      setPrefillBusy(false);
    }
  };

  // ---- Edit an existing stack -----------------------------------------------

  const openPicker = async () => {
    setPickerOpen(true);
    if (existingStacks.length || existingStacksLoading) return;
    setExistingStacksLoading(true);
    setExistingStacksError(null);
    try {
      const resp = await authFetch("/stacks");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: ExistingStackListItem[] = await resp.json();
      setExistingStacks(Array.isArray(data) ? data.filter((s) => s.status !== "torn_down") : []);
    } catch (e) {
      setExistingStacksError((e as Error).message || "Could not load your stacks.");
    } finally {
      setExistingStacksLoading(false);
    }
  };

  const startEditing = async (id: number) => {
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch(`/stacks/${id}/definition`);
      if (resp.status === 402) {
        throw new Error(
          "Editing a stack's composition isn't available on this install.",
        );
      }
      if (!resp.ok) {
        throw new Error(await surfaceError(resp, `Could not load stack ${id} (HTTP ${resp.status}).`));
      }
      const def: DefinitionResponse = await resp.json();
      setEditStackId(id);
      setName(def.name);
      setServices(Array.isArray(def.services) ? def.services : []);
      setSelectedIndex(0);
      setRevisions(Array.isArray(def.revisions) ? def.revisions : []);
      setPickerOpen(false);
      setStep("build");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const refreshRevisions = useCallback(async () => {
    if (!editStackId) return;
    try {
      const resp = await authFetch(`/stacks/${editStackId}/revisions`);
      if (!resp.ok) return;
      const data: { revisions: RevisionSummary[] } = await resp.json();
      setRevisions(Array.isArray(data.revisions) ? data.revisions : []);
    } catch {
      /* history stays as last known */
    }
  }, [authFetch, editStackId]);

  const viewDiff = async (revisionNo: number) => {
    if (!editStackId) return;
    setDiffFor(revisionNo);
    setDiffText(null);
    setDiffBusy(true);
    try {
      const resp = await authFetch(`/stacks/${editStackId}/revisions/${revisionNo}/diff`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setDiffText((data?.diff?.unified_diff as string) || "(no textual diff)");
    } catch (e) {
      setDiffText(`Could not load diff: ${(e as Error).message}`);
    } finally {
      setDiffBusy(false);
    }
  };

  const revertTo = async (revisionNo: number) => {
    if (!editStackId || revertBusy) return;
    setRevertBusy(true);
    setError(null);
    try {
      const resp = await authFetch(`/stacks/${editStackId}/revert`, {
        method: "POST",
        body: JSON.stringify({ target_revision_no: revisionNo }),
      });
      if (!resp.ok) {
        throw new Error(await surfaceError(resp, `Revert failed (HTTP ${resp.status}).`));
      }
      // Re-load the (now-current) definition so the builder reflects the
      // reverted composition — revert APPENDS a new revision, so this fetch
      // shows the SAME content under a new revision_no.
      await startEditing(editStackId);
      setDiffFor(null);
      setDiffText(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRevertBusy(false);
    }
  };

  const redeployNow = async () => {
    if (!editStackId || redeployBusy) return;
    setRedeployBusy(true);
    setError(null);
    try {
      const resp = await authFetch(`/stacks/${editStackId}/deploy`, { method: "POST" });
      if (!resp.ok) {
        throw new Error(await surfaceError(resp, `Redeploy failed (HTTP ${resp.status}).`));
      }
      setRedeployDone(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRedeployBusy(false);
    }
  };

  // ---- Review → assemble ---------------------------------------------------

  const goToReview = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setValidatorError(null);
    setAssembled(null);
    setStep("review");
    try {
      const resp = await authFetch("/stacks/assemble", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), services }),
      });
      if (resp.status === 422) {
        // Validator (or shape) reason — surface inline and STAY on Review.
        setValidatorError(await surfaceError(resp, "The stack did not pass validation."));
        return;
      }
      if (!resp.ok) {
        throw new Error(await surfaceError(resp, `Assemble failed (HTTP ${resp.status}).`));
      }
      setAssembled(await resp.json());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // ---- Deploy → create then deploy → overview ------------------------------

  const fetchEndpoint = useCallback(
    async (id: number) => {
      try {
        const resp = await authFetch(`/stacks/${id}/endpoint`);
        if (!resp.ok) return;
        const info: EndpointInfo = await resp.json();
        // GA.ASW.4 — the endpoint payload carries no health verdict; the
        // stack DTO does (GET /stacks/{id} computes it on read). Best-effort
        // merge so the overview badge can render without a second round-trip
        // ever blocking the card.
        try {
          const dto = await authFetch(`/stacks/${id}`);
          if (dto.ok) {
            const body: { health?: StackHealth | null } = await dto.json();
            info.health = body.health;
          }
        } catch {
          /* grade stays unset; the card renders without the badge */
        }
        setEndpointInfo(info);
      } catch {
        /* overview still renders from compose + status without it */
      }
    },
    [authFetch],
  );

  // Create the stack, or — on a GA.ASW.6b requires_ack 409 — stash the body
  // and surface the inline warning instead. Returns null in the ack case (the
  // caller must stop; nothing to deploy yet) so it composes into both the
  // normal Deploy click and the "Create anyway" confirm.
  const submitCreate = async (body: {
    name: string;
    compose_spec: unknown;
  }): Promise<CreatedStack | null> => {
    const createResp = await authFetch("/stacks", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!createResp.ok) {
      const { backupAck: ack, message } = await classifyBackupAckResponse(createResp);
      if (ack) {
        setPendingCreateBody(body);
        setBackupAck(ack);
        setBackupAckChecked(false);
        return null;
      }
      throw new Error(message);
    }
    setBackupAck(null);
    setPendingCreateBody(null);
    return (await createResp.json()) as CreatedStack;
  };

  const afterCreated = async (created: CreatedStack) => {
    const deployResp = await authFetch(`/stacks/${created.id}/deploy`, {
      method: "POST",
    });
    if (!deployResp.ok) {
      throw new Error(
        await surfaceError(deployResp, `Deploy failed (HTTP ${deployResp.status}).`),
      );
    }
    const deployed: CreatedStack = await deployResp.json();
    setCreatedStack(deployed);
    setStep("done");
    void fetchEndpoint(created.id);
  };

  const confirmBackupAck = async () => {
    if (!pendingCreateBody || !backupAckChecked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await submitCreate(withBackupAck(pendingCreateBody));
      if (!created) return;
      await afterCreated(created);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const cancelBackupAck = () => {
    setBackupAck(null);
    setPendingCreateBody(null);
    setBackupAckChecked(false);
  };

  const deploy = async () => {
    if (!assembled || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (isEditing && editStackId != null) {
        // Save as a new revision — does NOT redeploy (ISO append-only history;
        // the operator redeploys explicitly from Done, same as node-code-edit
        // separates saving source from rebuilding it).
        const saveResp = await authFetch(`/stacks/${editStackId}/definition`, {
          method: "PUT",
          body: JSON.stringify({
            services,
            ...(saveMessage.trim() ? { message: saveMessage.trim() } : {}),
          }),
        });
        if (saveResp.status === 402) {
          throw new Error(
            "Editing a stack's composition isn't available on this install.",
          );
        }
        if (!saveResp.ok) {
          throw new Error(await surfaceError(saveResp, `Save failed (HTTP ${saveResp.status}).`));
        }
        const saved: CreatedStack = await saveResp.json();
        setCreatedStack(saved);
        setStep("done");
        setRedeployDone(false);
        void refreshRevisions();
        return;
      }

      const created = await submitCreate({
        name: name.trim(),
        compose_spec: assembled.compose_spec,
      });
      if (!created) return;
      await afterCreated(created);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // ---- Derived -------------------------------------------------------------

  const canReview = Boolean(name.trim()) && services.length > 0;
  const canPrefillTemplate = Boolean(selectedTemplate) && Boolean(name.trim());
  const canPrefillDescribe = Boolean(name.trim()) && Boolean(prompt.trim());
  const notes = assembled?.notes || {};
  const assembledServiceNames = assembled?.compose_spec
    ? Object.keys(
        ((assembled.compose_spec as ComposeShape).services || {}) as Record<string, unknown>,
      )
    : [];

  // ---- Render --------------------------------------------------------------

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          resetAll();
          onClose();
        }
      }}
    >
      <DialogContent
        className="max-w-[900px] w-[900px] h-[680px] max-h-[92vh] flex flex-col"
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
      >
        <DialogHeader className="text-left flex-shrink-0">
          <DialogTitle className="text-left flex items-center gap-2">
            {isEditing ? (
              <Pencil className="h-5 w-5" aria-hidden="true" />
            ) : (
              <Boxes className="h-5 w-5" aria-hidden="true" />
            )}
            {isEditing ? `Edit Stack — ${name || `#${editStackId}`}` : wbNavLabel('wb-nav-wizard-stacks', 'Stack Wizard')}
          </DialogTitle>
          <DialogDescription className="text-left">
            {isEditing
              ? "Edit this stack's services and config. Saving appends a new " +
                "revision — nothing is overwritten, and you can always revert."
              : "Compose a persistent stack from one or more services, price it, " +
                "then deploy. Start from a template, describe it, or build by hand."}
          </DialogDescription>
        </DialogHeader>

        {/* Stepper */}
        <div className="flex items-center gap-2 text-[11px] font-medium flex-shrink-0 -mt-1">
          {(["build", "review", "done"] as Step[]).map((s, i) => {
            const active = step === s;
            const label =
              s === "build" ? "Build" : s === "review" ? "Review" : isEditing ? "Save" : "Deploy";
            return (
              <React.Fragment key={s}>
                <span
                  className={
                    "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 " +
                    (active ? "bg-indigo-100 text-indigo-700" : "text-gray-400")
                  }
                >
                  <span
                    className={
                      "inline-flex items-center justify-center h-4 w-4 rounded-full text-[9px] " +
                      (active ? "bg-indigo-600 text-white" : "bg-gray-200 text-gray-500")
                    }
                  >
                    {i + 1}
                  </span>
                  {label}
                </span>
                {i < 2 && <span className="text-gray-300">→</span>}
              </React.Fragment>
            );
          })}
        </div>

        {/* ==================== BUILD ==================== */}
        {step === "build" && (
          <>
            <div className="flex-1 min-h-0 flex flex-col gap-3 overflow-hidden">
              {/* Stack name + on-ramp toggles */}
              <div className="flex items-end gap-3 flex-shrink-0">
                <div className="flex-1 space-y-1.5">
                  <label className="text-xs font-medium text-gray-700">
                    Stack name <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="my-service-stack"
                    disabled={busy || isEditing}
                    title={isEditing ? "Renaming an existing stack isn't part of this editor yet." : undefined}
                    className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-50 disabled:text-gray-500"
                    autoComplete="off"
                  />
                </div>
                {!isEditing && (
                  <div className="flex items-center gap-1 pb-0.5">
                    <button
                      type="button"
                      onClick={() => setOnRamp(onRamp === "template" ? "none" : "template")}
                      className={
                        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors " +
                        (onRamp === "template"
                          ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                          : "border-gray-300 text-gray-600 hover:border-indigo-400 hover:text-indigo-700")
                      }
                    >
                      <LayoutTemplate className="h-3.5 w-3.5" />
                      Template
                    </button>
                    <button
                      type="button"
                      onClick={() => setOnRamp(onRamp === "describe" ? "none" : "describe")}
                      className={
                        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors " +
                        (onRamp === "describe"
                          ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                          : "border-gray-300 text-gray-600 hover:border-indigo-400 hover:text-indigo-700")
                      }
                    >
                      <Sparkles className="h-3.5 w-3.5" />
                      Describe
                    </button>
                    <button
                      type="button"
                      onClick={() => setOnRamp(onRamp === "import" ? "none" : "import")}
                      className={
                        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors " +
                        (onRamp === "import"
                          ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                          : "border-gray-300 text-gray-600 hover:border-indigo-400 hover:text-indigo-700")
                      }
                    >
                      <FileUp className="h-3.5 w-3.5" />
                      Import docker-compose
                    </button>
                    <button
                      type="button"
                      onClick={() => (pickerOpen ? setPickerOpen(false) : void openPicker())}
                      className={
                        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors " +
                        (pickerOpen
                          ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                          : "border-gray-300 text-gray-600 hover:border-indigo-400 hover:text-indigo-700")
                      }
                      title="Open an existing stack to edit its services/config"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                      Edit existing…
                    </button>
                  </div>
                )}
                {isEditing && (
                  <button
                    type="button"
                    onClick={() => setHistoryOpen((v) => !v)}
                    className={
                      "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors mb-0.5 " +
                      (historyOpen
                        ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                        : "border-gray-300 text-gray-600 hover:border-indigo-400 hover:text-indigo-700")
                    }
                  >
                    <History className="h-3.5 w-3.5" />
                    History ({revisions.length})
                  </button>
                )}
              </div>

              {/* Edit-existing-stack picker (stack-definition-edit) */}
              {pickerOpen && !isEditing && (
                <div className="rounded-md border border-indigo-200 bg-indigo-50/40 px-3 py-3 space-y-2 flex-shrink-0 max-h-[200px] overflow-y-auto">
                  <p className="text-[11px] text-gray-500">
                    Pick one of your stacks to edit its services and config. It
                    opens with the composition it currently has.
                  </p>
                  {existingStacksLoading ? (
                    <div className="flex items-center gap-2 text-xs text-gray-400">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading your stacks…
                    </div>
                  ) : existingStacksError ? (
                    <p className="text-xs text-red-600">{existingStacksError}</p>
                  ) : existingStacks.length === 0 ? (
                    <p className="text-xs text-gray-400">No stacks yet — build one first.</p>
                  ) : (
                    <div className="space-y-1">
                      {existingStacks.map((s) => (
                        <button
                          key={s.id}
                          type="button"
                          onClick={() => void startEditing(s.id)}
                          disabled={busy}
                          className="w-full flex items-center justify-between rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs hover:border-indigo-400 hover:bg-indigo-50 text-left"
                        >
                          <span className="font-medium text-gray-800">{s.name}</span>
                          <span className="text-gray-400 flex items-center gap-1.5">
                            {s.status || ""}
                            {StackGradeBadge(s.health)}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Revision history (edit mode) — list, diff, revert. Mirrors
                  node-code-edit's revisions/diff/revert UX for a stack. */}
              {isEditing && historyOpen && (
                <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-3 space-y-2 flex-shrink-0 max-h-[220px] overflow-y-auto">
                  {revisions.length === 0 ? (
                    <p className="text-xs text-gray-400">No history yet.</p>
                  ) : (
                    [...revisions].reverse().map((rv) => (
                      <div
                        key={rv.revision_no}
                        className="flex items-center justify-between gap-2 rounded border border-gray-200 bg-white px-2 py-1.5"
                      >
                        <div className="min-w-0">
                          <div className="text-xs font-medium text-gray-800">
                            rev {rv.revision_no} · {rv.origin}
                          </div>
                          {(rv.message || rv.created_at) && (
                            <div className="text-[10px] text-gray-400 truncate">
                              {rv.message || ""}
                              {rv.message && rv.created_at ? " · " : ""}
                              {rv.created_at ? new Date(rv.created_at).toLocaleString() : ""}
                            </div>
                          )}
                        </div>
                        <div className="flex items-center gap-1 flex-shrink-0">
                          <button
                            type="button"
                            onClick={() => void viewDiff(rv.revision_no)}
                            className="px-2 py-1 text-[10px] rounded bg-gray-100 text-gray-700 hover:bg-gray-200"
                          >
                            Diff
                          </button>
                          <button
                            type="button"
                            onClick={() => void revertTo(rv.revision_no)}
                            disabled={revertBusy}
                            className="inline-flex items-center gap-1 px-2 py-1 text-[10px] rounded bg-gray-100 text-gray-700 hover:bg-gray-200"
                            title="Append a new revision equal to this one and load it into the builder"
                          >
                            <Undo2 className="h-3 w-3" />
                            Revert to this
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                  {diffFor != null && (
                    <div className="rounded border border-gray-300 bg-gray-900 px-2 py-2">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[10px] text-gray-300">
                          Diff: rev {diffFor} → current
                        </span>
                        <button
                          type="button"
                          onClick={() => { setDiffFor(null); setDiffText(null); }}
                          className="text-[10px] text-gray-400 hover:text-white"
                        >
                          ✕
                        </button>
                      </div>
                      {diffBusy ? (
                        <div className="text-[10px] text-gray-400">Loading…</div>
                      ) : (
                        <pre className="text-[10px] text-gray-100 whitespace-pre-wrap max-h-40 overflow-auto font-mono">
                          {diffText}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* On-ramp panels — pre-fill, then edit before Deploy. */}
              {onRamp === "template" && (
                <div className="rounded-md border border-indigo-200 bg-indigo-50/40 px-3 py-3 space-y-2.5 flex-shrink-0 max-h-[240px] overflow-y-auto">
                  <p className="text-[11px] text-gray-500">
                    Pick a template to pre-fill the builder — every service lands
                    editable, nothing deploys yet.
                  </p>
                  {templatesLoading ? (
                    <div className="flex items-center gap-2 text-xs text-gray-400">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading templates…
                    </div>
                  ) : templates.length === 0 ? (
                    <p className="text-xs text-gray-400">
                      No templates available. Try Describe, or build by hand.
                    </p>
                  ) : (
                    <>
                      <select
                        value={selectedTemplateId}
                        onChange={(e) => onPickTemplate(e.target.value)}
                        disabled={prefillBusy}
                        className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white"
                      >
                        <option value="">Choose a template…</option>
                        {templates.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.name}
                          </option>
                        ))}
                      </select>
                      {selectedTemplate?.description && (
                        <p className="text-[11px] text-gray-400">
                          {selectedTemplate.description}
                        </p>
                      )}
                      {selectedTemplate?.params.map((p) => (
                        <div key={p.key} className="space-y-1">
                          <label className="text-[11px] font-medium text-gray-600">
                            {p.label}
                          </label>
                          <input
                            type="text"
                            value={paramValues[p.key] ?? ""}
                            onChange={(e) =>
                              setParamValues((prev) => ({ ...prev, [p.key]: e.target.value }))
                            }
                            disabled={prefillBusy}
                            className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                            autoComplete="off"
                          />
                        </div>
                      ))}
                      <div className="flex justify-end">
                        <Button
                          type="button"
                          size="sm"
                          disabled={prefillBusy || !canPrefillTemplate}
                          onClick={prefillFromTemplate}
                          className="bg-indigo-600 hover:bg-indigo-700 text-white"
                        >
                          {prefillBusy ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            "Pre-fill builder"
                          )}
                        </Button>
                      </div>
                    </>
                  )}
                </div>
              )}

              {onRamp === "describe" && (
                <div className="rounded-md border border-indigo-200 bg-indigo-50/40 px-3 py-3 space-y-2.5 flex-shrink-0 max-h-[240px] overflow-y-auto">
                  <p className="text-[11px] text-gray-500">
                    Describe the stack; the AI composer drafts services to pre-fill
                    the builder. Edit them before Deploy.
                  </p>
                  <textarea
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    placeholder="e.g. 'a Gitea git server with a Postgres database'"
                    disabled={prefillBusy}
                    rows={3}
                    className="w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none"
                  />
                  <div className="flex justify-end">
                    <Button
                      type="button"
                      size="sm"
                      disabled={prefillBusy || !canPrefillDescribe}
                      onClick={prefillFromDescribe}
                      className="bg-indigo-600 hover:bg-indigo-700 text-white"
                    >
                      {prefillBusy ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        "Pre-fill builder"
                      )}
                    </Button>
                  </div>
                </div>
              )}

              {onRamp === "import" && (
                <div className="rounded-md border border-indigo-200 bg-indigo-50/40 px-3 py-3 flex-shrink-0 max-h-[420px] overflow-y-auto">
                  <StackComposeImport
                    authFetch={authFetch}
                    onCreated={(s) => {
                      setCreatedStack(s);
                      setStep("done");
                    }}
                  />
                </div>
              )}

              {prefillError && (
                <div className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-800 flex items-center gap-2 flex-shrink-0">
                  <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0" />
                  {prefillError}
                </div>
              )}

              {/* Services list + per-service editor */}
              <div className="flex-1 min-h-0 flex gap-4 border-t pt-3">
                <div className="w-[210px] flex-shrink-0">
                  <ServiceList
                    services={services}
                    selectedIndex={clampedIndex}
                    onSelect={setSelectedIndex}
                    onAdd={handleAdd}
                    onRemove={handleRemove}
                  />
                </div>
                <div className="flex-1 min-w-0 overflow-y-auto pr-1">
                  {selectedService ? (
                    <ServiceEditor service={selectedService}
                                   onChange={handleEditorChange}
                                   stackId={editStackId} />
                  ) : (
                    <div className="h-full flex flex-col items-center justify-center text-center text-gray-400 gap-2">
                      <Layers className="h-8 w-8" />
                      <p className="text-xs max-w-[240px]">
                        No service selected. Add a service, or pre-fill from a
                        template / description to start composing.
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Persistent footer (CE: no estimated-cost line, D74) */}
            <div className="flex-shrink-0 border-t pt-3 space-y-3">
              {error && (
                <div className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-800">
                  {error}
                </div>
              )}
              <div className="flex items-center gap-2">
                {/* Review-disabled recovery. When Describe 422s or a
                    template can't be loaded the builder is left with zero
                    services, so canReview is false and Review greys out
                    with no explanation. Say exactly what's missing and how
                    to get unstuck rather than leaving a dead button. */}
                {!canReview && (
                  <p className="text-[11px] text-gray-500 flex items-center gap-1.5">
                    <AlertTriangle className="h-3.5 w-3.5 text-amber-500 flex-shrink-0" />
                    {!name.trim() && services.length === 0
                      ? "Name the stack and add at least one service (by hand, a template, or Describe) to Review."
                      : !name.trim()
                        ? "Give the stack a name to Review."
                        : "Add at least one service to Review — build one by hand above, pick a template, or Describe the stack."}
                  </p>
                )}
                <div className="flex-1" />
                <Button
                  type="button"
                  size="sm"
                  disabled={busy || !canReview}
                  onClick={goToReview}
                  className="bg-indigo-600 hover:bg-indigo-700 text-white"
                >
                  Review
                  <ArrowRight className="h-4 w-4 ml-1.5" />
                </Button>
              </div>
            </div>
          </>
        )}

        {/* ==================== REVIEW ==================== */}
        {step === "review" && (
          <>
            <div className="flex-1 min-h-0 overflow-y-auto py-3 space-y-4">
              <div className="text-sm font-semibold text-gray-900">
                Review “{name}”
              </div>

              {busy && !assembled && !validatorError && (
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Assembling &
                  validating the stack…
                </div>
              )}

              {validatorError && (
                <div className="rounded-md bg-red-50 border border-red-200 px-3 py-3 text-xs text-red-800 space-y-1">
                  <div className="flex items-center gap-2 font-semibold">
                    <AlertTriangle className="h-4 w-4" />
                    The stack did not validate
                  </div>
                  <p className="whitespace-pre-wrap break-words">{validatorError}</p>
                  <p className="text-[11px] text-red-600">
                    Go back, fix the flagged service, and Review again.
                  </p>
                </div>
              )}

              {assembled && (
                <div className="space-y-3">
                  <p className="text-xs text-gray-500">
                    Validated. {assembledServiceNames.length} service
                    {assembledServiceNames.length === 1 ? "" : "s"}{" "}
                    {isEditing
                      ? "will be saved as a new revision."
                      : "will be created and deployed."}
                  </p>
                  {isEditing && (
                    <div className="space-y-1">
                      <label className="text-[11px] font-medium text-gray-600">
                        Note (optional) — what changed?
                      </label>
                      <input
                        type="text"
                        value={saveMessage}
                        onChange={(e) => setSaveMessage(e.target.value)}
                        placeholder="e.g. bumped nginx, added a cache service"
                        disabled={busy}
                        className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                        autoComplete="off"
                      />
                    </div>
                  )}
                  <div className="space-y-2">
                    {services.map((svc, i) => (
                      <div
                        key={i}
                        className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 space-y-1"
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-medium text-gray-800">
                            {svc.name || "(unnamed)"}
                          </span>
                          <span className="text-[10px] text-gray-500">
                            {svc.source.kind}
                            {svc.source.kind === "image" && svc.source.image
                              ? `: ${svc.source.image}`
                              : ""}
                          </span>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-gray-500">
                          {svc.ports.length > 0 && (
                            <span>ports {svc.ports.join(", ")}</span>
                          )}
                          {svc.expose ? (
                            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 text-emerald-700 px-1.5 py-0.5 font-medium">
                              <Link2 className="h-3 w-3" /> exposed
                            </span>
                          ) : (
                            <span className="text-gray-400">internal only</span>
                          )}
                        </div>
                        {notes[svc.name] && (
                          <p className="text-[11px] text-gray-400">{notes[svc.name]}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {error && (
                <div className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-800">
                  {error}
                </div>
              )}

              {backupAck && (
                <Alert variant="destructive" data-testid="stack-wizard-backup-ack">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  <AlertDescription className="space-y-2">
                    <p className="text-xs">
                      These databases have no backup plan: {formatBackupAckServices(backupAck)}
                    </p>
                    <p className="text-[10px] text-muted-foreground">{backupAck.reason}</p>
                    <div className="flex items-center gap-2">
                      <Checkbox
                        id="stack-wizard-backup-ack-checkbox"
                        data-testid="stack-wizard-backup-ack-checkbox"
                        checked={backupAckChecked}
                        onCheckedChange={(c) => setBackupAckChecked(c === true)}
                      />
                      <label htmlFor="stack-wizard-backup-ack-checkbox" className="text-xs">
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
                        data-testid="stack-wizard-backup-ack-cancel"
                      >
                        Cancel
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        disabled={busy || !backupAckChecked}
                        onClick={confirmBackupAck}
                        data-testid="stack-wizard-backup-ack-confirm"
                        className="bg-red-600 hover:bg-red-700 text-white"
                      >
                        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Create anyway"}
                      </Button>
                    </div>
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <div className="flex-shrink-0 border-t pt-3 space-y-3">
              {/* CE: no estimated-cost line (D74) */}
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    setStep("build");
                    setError(null);
                  }}
                >
                  <ArrowLeft className="h-4 w-4 mr-1.5" />
                  Back
                </Button>
                <div className="flex-1" />
                <Button
                  type="button"
                  size="sm"
                  disabled={busy || !assembled}
                  onClick={deploy}
                  className="bg-indigo-600 hover:bg-indigo-700 text-white"
                >
                  {busy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : isEditing ? (
                    <Save className="h-4 w-4 mr-1.5" />
                  ) : (
                    <Rocket className="h-4 w-4 mr-1.5" />
                  )}
                  {isEditing ? "Save" : "Deploy"}
                </Button>
              </div>
            </div>
          </>
        )}

        {/* ==================== DONE / OVERVIEW ==================== */}
        {step === "done" && createdStack && (
          <>
            <div className="flex-1 min-h-0 overflow-y-auto py-3 space-y-4">
              <div className="rounded-md border border-gray-200 bg-gray-50 px-4 py-3 space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-gray-900 flex items-center gap-2">
                    <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                    {createdStack.name}
                  </span>
                  {(endpointInfo?.status || createdStack.status) && (
                    <span className="inline-flex items-center rounded-full bg-indigo-100 text-indigo-700 px-2 py-0.5 text-[10px] font-medium">
                      {endpointInfo?.status || createdStack.status}
                    </span>
                  )}
                  {StackGradeBadge(endpointInfo?.health)}
                </div>
                {isEditing ? (
                  <p className="text-[11px] text-gray-500">
                    Saved as a new revision. The running stack is unchanged
                    until you redeploy.
                    {redeployDone && (
                      <span className="text-emerald-600"> Redeploy dispatched.</span>
                    )}
                  </p>
                ) : endpointInfo?.running_unreachable ? (
                  <p className="text-[11px] text-amber-600">
                    Running, but not yet reachable — the mesh may still be coming up.
                  </p>
                ) : endpointInfo?.reachable ? (
                  <p className="text-[11px] text-emerald-600">
                    Reachable via {endpointInfo.reach_mode || "mesh"}.
                  </p>
                ) : (
                  <p className="text-[11px] text-gray-500">
                    Deploying — reachability appears once a worker reports it up.
                  </p>
                )}
                {(endpointInfo?.status_reason || createdStack.status_reason) && (
                  <p className="text-[11px] text-gray-400">
                    {endpointInfo?.status_reason || createdStack.status_reason}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                  Services
                </div>
                {services.map((svc, i) => {
                  const mesh = endpointInfo?.mesh_endpoints?.[svc.name];
                  return (
                    <div
                      key={i}
                      className="rounded-md border border-gray-200 px-3 py-2 space-y-1"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-gray-800">
                          {svc.name || "(unnamed)"}
                        </span>
                        {svc.expose ? (
                          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 text-emerald-700 px-1.5 py-0.5 text-[10px] font-medium">
                            <Link2 className="h-3 w-3" /> exposed
                          </span>
                        ) : (
                          <span className="text-[10px] text-gray-400">internal only</span>
                        )}
                      </div>
                      <div className="text-[11px] text-gray-500">
                        {svc.ports.length > 0
                          ? `container ports ${svc.ports.join(", ")}`
                          : "no container ports declared"}
                      </div>
                      {svc.expose && !isEditing && (
                        <div className="text-[11px]">
                          {mesh?.magicdns_name ? (
                            <span className="inline-flex items-center gap-1 text-indigo-700 break-all">
                              <Link2 className="h-3 w-3 flex-shrink-0" />
                              {mesh.magicdns_name}
                              {mesh.status ? (
                                <span className="text-gray-400"> ({mesh.status})</span>
                              ) : null}
                            </span>
                          ) : (
                            <span className="text-gray-400">
                              magic URL is minting — refresh the stack overview shortly.
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              {error && (
                <div className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-800">
                  {error}
                </div>
              )}
            </div>

            <div className="flex-shrink-0 border-t pt-3 flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={resetAll}
              >
                {isEditing ? "Edit another" : "Build another"}
              </Button>
              <div className="flex-1" />
              {isEditing && createdStack ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={redeployBusy || redeployDone}
                  onClick={() => void redeployNow()}
                >
                  {redeployBusy ? (
                    <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                  ) : (
                    <Rocket className="h-4 w-4 mr-1.5" />
                  )}
                  {redeployDone ? "Redeployed" : "Redeploy now"}
                </Button>
              ) : createdStack ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => void fetchEndpoint(createdStack.id)}
                >
                  Refresh status
                </Button>
              ) : null}
              <Button
                type="button"
                size="sm"
                disabled={busy}
                onClick={() => {
                  resetAll();
                  onClose();
                }}
                className="bg-indigo-600 hover:bg-indigo-700 text-white"
              >
                Done
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default StackWizardPanel;
