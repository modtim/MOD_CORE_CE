"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

import { useState, useCallback, useEffect, useMemo, useRef, useReducer } from "react";
import {
  reduce as reduceChat,
  initialChatState,
  buildSynthesizeDescription,
  composerEnabledIn,
  inferTypesFromDescription,
  buildSynthesizeContract,
  classifySynthFailure,
  buildPreflight,
  type ChatMessage,
  type RepoCandidate,
} from "@/lib/describe-chat";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { CopyLogButton } from "@/components/ui/copy-log-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { NumberedEditor } from "@/components/ui/numbered-editor";
// wizard-rebuild §8 — Describe cascade panel. Imported here so the
// component lands in the workflow_builder bundle. Rendered behind
// a runtime feature flag below; off by default while §8 stabilizes.
import { DescribeCascadePanel } from "./describe-cascade-panel";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { cn } from "@/lib/utils";
import { toast } from "@/components/ui/use-toast";
import { authenticatedFetch } from "@/lib/api-client";
import { useEntitlements } from "@/hooks/use-entitlements";
import { ioBannerBody, needsIOConfirmation } from "@/lib/io-gate";
import { render as renderMetadataTemplate, coerceAndValidate as coerceMetadata } from "@/lib/metadataTemplate";
import { detectLanguage } from "@/lib/detect-language";
import {
  computeReviewInitialSkipped,
  computeBuildAutoSkipFromArtifacts,
} from "@/lib/variant-skip";
import {
  deferredRegistrationFields,
  validateSampleSize,
  stripDataUrlPrefix,
  SAMPLE_INPUT_MAX_BYTES,
} from "@/lib/sample-input";
// Pending-generations store is retired: server-side writes a
// Notification row on task completion, the bell picks it up, and the
// Resume flow routes the user back into the wizard Preview. No local
// localStorage state needed for the close-and-notify UX.
import WizardErrorBoundary from "./wizard-error-boundary";
import SourceFilesPane, { type WizardSourceFile, makeEmptyFile } from "./source-files-pane";
import CodeEditor from "./code-editor";
// sz-tcp.5 — the tier card shows the server's credit-cost basis chip
// ('estimated' / 'calibrated' / 'manual' / 'free') next to its heading.
// Unknown/absent bases render nothing (old nodes have no cost_basis).
import CostBasisBadge from "./CostBasisBadge";
// BuildVariantPreview was the earlier single-panel tile view; the
// Preview step now renders each variant in its own tab with an
// inline per-file diff, so BuildVariantPreview is unused here (its
// module stays in the repo for contract tests / future reuse).
import {
  Upload, Container, FileCode, ChevronRight, ChevronLeft, ChevronDown,
  Plus, Trash2, Loader2, Check, Cpu, Monitor, GitBranch, KeyRound, Files,
  ChevronsUpDown,
  Sparkles, AlertTriangle, CheckCircle2,
} from "lucide-react";

// Credits disabled by default. When re-enabling, fetch from /platform/features.
const CREDITS_UI_ENABLED = false;

interface NewNodeWizardProps {
  open: boolean;
  onClose: () => void;
  accessToken: string | null;
  /**
   * Called when the wizard successfully dispatches a build + registration
   * task. The parent uses `taskId` + `nodeName` to drive a persistent
   * progress card (see BuildProgressCard) — the build itself takes
   * minutes (LLM gen + docker build + docker push + API register +
   * optional Trivy scan) and the user needs visibility into the
   * current stage after the wizard closes.
   */
  /** Fires after a successful Confirm & Build submit. `taskId` is the
   *  Phase-2 (image-builder) task; `generationTaskId` is the Phase-1
   *  (Containerizer) task that produced the artifacts the build is
   *  shipping. Both are passed so the parent can render BOTH rows in
   *  the build queue — Phase 1 as already-done, Phase 2 as live —
   *  instead of the queue looking like Phase 1 was skipped.
   *  `singleStep` is set on the repo (git) flow which has no Phase 1
   *  at all — the parent renders the row with a "Build & register"
   *  header (no "Step 2 of 2" prefix) so the queue doesn't lie about
   *  there being a missing Step 1 of 2. */
  onNodeCreated?: (info?: {
    taskId: number;
    nodeName: string;
    generationTaskId?: number | null;
    singleStep?: boolean;
  }) => void;
  /**
   * Set when the user clicked "Resume" on a pending generation badge.
   * The wizard jumps straight to the Preview step and polls
   * /generation-artifacts/{taskId} to hydrate the previously-generated
   * artifacts, so the user lands exactly where they were when they
   * hit "Close & notify me".
   */
  resumeTaskId?: number | null;
  /**
   * The user-chosen nodeName at the time the deferred Phase 1 task
   * was dispatched. The defer flow wipes localStorage, so on resume
   * the wizard's `nodeName` would default to "" — meaning Phase 2's
   * onNodeCreated callback fires with nodeName="" and the queue row
   * falls back to "Task #<id>". Parent reads it from the build queue
   * entry and passes it back so the wizard rehydrates the name.
   */
  resumeNodeName?: string;
  /**
   * workflow-wizard-missing-node-handoff — when the Workflow Wizard
   * proposes a workflow that needs a node the user hasn't built yet,
   * clicking "Build it now" on the eager handoff panel hands off this
   * seed payload (LLM-suggested source_type + source + purpose). The
   * NewNodeWizard hydrates these onto its form on mount so the user
   * lands on a pre-populated form instead of an empty one. Absent for
   * the legacy 422 missing_nodes recovery path or when the wizard is
   * opened from scratch.
   */
  resumeWizardSeed?: {
    sourceType?: "git" | "image" | "dockerfile" | "script" | "describe";
    source?: string;
    purpose?: string;
  } | null;
  /**
   * Called when the user hits "Close & notify me" on the Preview step
   * mid-generation. The parent adds a `phase: "generation"` row to
   * the Node Queue so the user can track the Containerizer task
   * there, and the server-side DB Notification fires on completion
   * (worker_tasks._notify_wizard_generation_complete).
   */
  onDeferGeneration?: (info: { taskId: number; nodeName: string }) => void;
  /**
   * node-wizard→workflow handoff. Called when the user accepts the nudge to
   * build a multi-step request in the Workflow Wizard instead. The parent
   * closes this wizard, seeds the Workflow Wizard with `prompt`, and opens it.
   */
  onSwitchToWorkflow?: (prompt: string) => void;
  /**
   * node-code-edit-rebuild — set by NodeLibrary's "Edit code" action
   * (via workflow-builder.tsx) to open the wizard directly into
   * "edit existing node code" mode instead of the normal create-a-node
   * flow. When set, the wizard skips Source/Configure/.../Review
   * entirely and renders a dedicated code-edit surface: fetches
   * GET /nodes/{editNodeId}/source, lets the user edit the
   * hero + per-variant source, save (POST .../source, auto-forking via
   * POST .../copy on 409 must_fork if the node is a system node),
   * rebuild (POST .../rebuild), and browse/diff/revert revisions
   * (GET .../source/revisions, GET .../source/diff, POST .../source/revert).
   * Everyone-facing — permission is enforced server-side (RBAC capability
   * on the node + the custom_nodes entitlement for rebuild), not by an
   * admin feature flag.
   */
  editNodeId?: number | null;
  /** Display-name fallback for the edit-mode header while /source is
   *  still loading (or if it 403s because the caller lacks permission). */
  editNodeName?: string;
  /**
   * sz-tcp.5 — raw `cost_basis` of the node being edited, when the caller
   * knows it (NodeLibrary edit-mode handoff). The tier-card heading renders
   * a CostBasisBadge chip for it; absent/unknown → no chip.
   */
  costBasis?: string | null;
}

type SourceType = "image" | "dockerfile" | "repo" | "script" | "describe";

interface EnvVarDef {
  name: string;
  description: string;
  default_value: string;
  required: boolean;
}

const STEPS = ["Source", "Configure", "Variables", "Network", "Review", "Preview"] as const;
type Step = typeof STEPS[number];

// Review is the final user-facing step for almost every submit path.
// The wizard fires one POST to /build-and-register-node from Review;
// the server side runs the workflow_run (generate if needed → ingest
// → variants → register). Preview is retained only for the deferred-
// sample-review flow (user attached a sample input and wants to see
// the smoke output before registering). The 5-step path is therefore
// the default; Preview is added on a per-submit basis when relevant.
function stepsForSource(_sourceType: SourceType): readonly Step[] {
  return ["Source", "Configure", "Variables", "Network", "Review"] as const;
}

// Preset destinations the wizard offers as quick-toggles. Default is
// deny-all — users opt into each preset or enter custom hosts. Enforced
// by the worker's runtime sandbox (see SelectNetworkMode + the
// mod-user-internal / mod-user-egress Docker bridges).
const EGRESS_PRESETS: { host: string; label: string; description: string }[] = [
  { host: "api.openai.com", label: "OpenAI", description: "OpenAI ChatCompletion / Embeddings" },
  { host: "api.anthropic.com", label: "Anthropic", description: "Claude API" },
  { host: "huggingface.co", label: "Hugging Face", description: "Model + dataset downloads" },
  { host: "github.com", label: "GitHub", description: "Git clone / raw file fetch" },
  { host: "pypi.org", label: "PyPI", description: "Python package installs" },
  { host: "registry.npmjs.org", label: "npm", description: "Node package installs" },
  { host: "*.amazonaws.com", label: "AWS", description: "S3 / other AWS services" },
  { host: "*.googleapis.com", label: "Google APIs", description: "GCP services" },
];

// Curated dropdown options. `input_type` / `output_type` on the
// NodeDefinition are OPEN strings end-to-end — the LLM may suggest any
// domain-specific label (`ifc`, `parquet`, `dicom`, ...) and the
// backend stores it verbatim. This list only controls what users see
// in the dropdown. If an LLM-suggested value isn't in this list, the
// wizard injects a synthetic "Custom" option so the value renders.
const INPUT_TYPES = [
  { value: "image", label: "Image", desc: "PNG, JPG, TIFF, EXR" },
  { value: "video", label: "Video", desc: "MP4, MOV, AVI" },
  { value: "audio", label: "Audio", desc: "MP3, WAV, FLAC" },
  { value: "text", label: "Text/Data", desc: "JSON, CSV, TXT" },
  { value: "json", label: "JSON", desc: "Structured JSON documents" },
  { value: "csv", label: "CSV/TSV", desc: "Tabular flat files" },
  { value: "mesh", label: "3D Mesh", desc: "OBJ, FBX, GLB, PLY" },
  { value: "texture", label: "Texture", desc: "PBR texture maps" },
  { value: "pointcloud", label: "Point Cloud", desc: "PLY, PCD, LAS" },
  { value: "pdf", label: "PDF", desc: "Single- or multi-page PDF documents" },
  { value: "document", label: "Document", desc: "DOCX, DOC, ODT, RTF" },
  // AEC / BIM
  { value: "ifc", label: "IFC (BIM)", desc: "Industry Foundation Classes" },
  { value: "dwg", label: "DWG (CAD)", desc: "AutoCAD drawing" },
  { value: "dxf", label: "DXF (CAD)", desc: "Drawing Exchange Format" },
  { value: "step", label: "STEP (CAD)", desc: "ISO 10303 / .step / .stp" },
  // GIS
  { value: "geojson", label: "GeoJSON", desc: "GIS vector data" },
  { value: "shp", label: "Shapefile", desc: "ESRI Shapefile (.shp)" },
  // Scientific / ML
  { value: "parquet", label: "Parquet", desc: "Columnar tabular data" },
  { value: "hdf5", label: "HDF5", desc: "Hierarchical scientific data" },
  { value: "dicom", label: "DICOM", desc: "Medical imaging" },
  { value: "fasta", label: "FASTA/FASTQ", desc: "Bio sequences" },
  { value: "onnx", label: "ONNX model", desc: "Portable ML model" },
  { value: "safetensors", label: "Safetensors", desc: "ML weight checkpoint" },
  // Generic
  { value: "binary", label: "Binary", desc: "Opaque bytes" },
  { value: "other", label: "Other", desc: "Generic / unclassified" },
];

const OUTPUT_TYPES = INPUT_TYPES;

// Persist the in-progress wizard state to localStorage so a surprise
// remount / page-redirect / accidental Escape tap doesn't nuke 10 min
// of typing. Stored under one key as a single JSON blob; hydrated on
// mount, overwritten on every state change, cleared by resetForm.
const WIZARD_STATE_KEY = "mod.newNodeWizard.state.v1";

/** sz-tcp.7 — pure tier-edit helpers (unit-tested in tierOverride.test.ts). */
type TierKey = "small" | "medium" | "large";
type TierSet = { small: number; medium: number; large: number };

/**
 * Apply one tier edit: the edited tier takes `value`, every other tier keeps
 * whatever the wizard currently shows. Pure — the wizard's `applyTier` wraps
 * it around the two state cells it has to keep in lock-step.
 */
export function applyTierEdit(prev: TierSet, tier: TierKey, value: number): TierSet {
  return { ...prev, [tier]: value };
}

/**
 * Reset link: restore the wizard's suggested tier set verbatim and drop the
 * 'manual' basis back to whatever it was before the first edit (null = the
 * server default, 'estimated'). Returns the tiers unchanged when there is
 * nothing to restore (no suggestion recorded).
 */
export function resetTiers(
  current: TierSet,
  suggested: TierSet | null,
  previousBasis: string | null,
): { tiers: TierSet; basis: string | null } {
  if (!suggested) return { tiers: current, basis: previousBasis };
  return { tiers: suggested, basis: previousBasis };
}

type PersistedWizardState = {
  currentStep: string;
  sourceType: string;
  dockerImage: string;
  dockerfileContent: string;
  scriptContent: string;
  scriptInterpreter: string;
  scriptFilename: string;
  // γ — sample-input filename for resume; bytes are NOT persisted
  // (could blow up localStorage). User re-attaches on resume.
  sampleInputFilename?: string;
  repoUrl: string;
  nodeName: string;
  description: string;
  inputType: string;
  outputType: string;
  processType: string;
  // Legacy single-cost field: still written (=== medium tier) so a draft
  // saved by an older build hydrates cleanly, and sz-tcp.5's tier card
  // (all three inputs required).
  costInCredits: number;
  costTiers?: { small: number; medium: number; large: number };
  // sz-tcp.7 — the wizard's own cost basis once the author edits a tier
  // ('manual'), the tier set that was on screen before the first edit
  // (the machine's guess, for the "Reset" link) and the basis in effect
  // before that edit (what Reset restores). All optional: a draft saved
  // by an older build hydrates with none of them.
  costBasis?: string | null;
  suggestedTiers?: TierSet | null;
  basisBeforeEdit?: string | null;
  containerTimeout: number;
  selectedBaseImage: string;
  useMultifileInput?: boolean;
  sourceFiles?: Array<{
    id: string;
    filename: string;
    body: string;
    language?: string;
    is_entrypoint: boolean;
  }>;
};

// An in-progress draft survives navigating away and back (the whole point of
// persisting it). To avoid an abandoned draft resurfacing forever on a much-later
// "+ New Node", drafts older than this TTL are treated as stale and dropped.
const WIZARD_DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

function loadPersisted(): Partial<PersistedWizardState> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(WIZARD_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedWizardState & { savedAt?: number };
    if (parsed.savedAt && Date.now() - parsed.savedAt > WIZARD_DRAFT_TTL_MS) {
      window.localStorage.removeItem(WIZARD_STATE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function persistWizardState(s: PersistedWizardState) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(WIZARD_STATE_KEY, JSON.stringify({ ...s, savedAt: Date.now() }));
  } catch {
    /* quota / SecurityError — in-memory still works */
  }
}

export function clearPersistedWizardState() {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(WIZARD_STATE_KEY);
  } catch { /* ignore */ }
}

// Map a filename's extension/basename to one of the script-tab's
// scriptInterpreter enum values. Returns null when the filename
// doesn't disambiguate (no extension, exotic ext) — caller keeps
// the existing interpreter selection. Returns the sentinel string
// "DOCKERFILE_REDIRECT" if the user uploaded an actual Dockerfile;
// caller is expected to toast and nudge them to the Dockerfile tab.
//
// Distinguishes plain-text files (`requirements.txt`, `Pipfile`,
// `pyproject.toml`, any `*.txt`) so they pick up the new "text"
// interpreter option instead of being misclassified as code.
export function inferScriptInterpreterFromFilename(filename: string): string | null {
  if (!filename) return null;
  const lower = filename.toLowerCase();
  // Strip path prefix that webkitRelativePath leaves behind.
  const base = lower.includes("/") ? lower.slice(lower.lastIndexOf("/") + 1) : lower;
  if (base === "dockerfile" || base.endsWith(".dockerfile")) return "DOCKERFILE_REDIRECT";
  if (base === "requirements.txt" || base === "pipfile" || base === "pyproject.toml") {
    return "text";
  }
  const dot = base.lastIndexOf(".");
  if (dot === -1) return null;
  const ext = base.slice(dot);
  const map: Record<string, string> = {
    ".py": "python3",
    ".sh": "bash", ".bash": "bash",
    ".js": "node", ".mjs": "node",
    ".ts": "typescript",
    ".rb": "ruby",
    ".php": "php",
    ".pl": "perl",
    ".lua": "lua",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".java": "java",
    ".r": "r",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".txt": "text",
  };
  return map[ext] || null;
}

// δ §21 — one chat bubble in the Describe-and-build transcript.
// Renders system / user messages, optional repo-candidate cards, and
// the two inline buttons the reducer surfaces (Build fresh on the
// candidate list, Build it on ready_to_build). Pure render — all
// state lives in the chat reducer.
// Human-readable live status for the in-flight "Putting it together…"
// bubble. Piece 3: the synth endpoint now STREAMS its real internal
// stages (generating → reviewing → revising → escalating), so we show
// what's actually happening — not just an elapsed clock. When no stage
// event has arrived yet, fall back to the time-based hint so it still
// reads as "working", never "frozen".
function synthStatusLine(
  sec: number, attempt: number, stage?: string, stageName?: string,
): string {
  const mm = Math.floor(sec / 60);
  const ss = sec % 60;
  const elapsed = mm > 0 ? `${mm}m ${ss}s` : `${ss}s`;
  let hint: string;
  switch (stage) {
    case "seeded":
      hint = stageName
        ? `Found a working node for a similar task (“${stageName}”) — adapting it…`
        : "Found a similar working node — adapting it…";
      break;
    case "generating":
      hint = "Generating the wrapper script…";
      break;
    case "reviewing":
      hint = "Reviewing the result for correctness…";
      break;
    case "revising":
      hint = "Revising based on what the review found…";
      break;
    case "escalating":
      hint = "Tricky one — bringing in a stronger model…";
      break;
    default:
      // No stage yet (or "starting") — time-based fallback.
      if (sec < 15) hint = "Generating the wrapper script…";
      else if (sec < 40) hint = "Writing the container recipe + dependencies…";
      else if (sec < 75) hint = "Complex node — the model needs a bit longer on this one…";
      else hint = "Still working — heavier nodes can take a couple of minutes…";
  }
  const att = attempt > 1 ? ` · attempt ${attempt}` : "";
  return `${hint}  (${elapsed}${att})`;
}

// SSE reader for the streaming synth endpoint. Mirrors the workflow-wizard
// panel's consumer: POST + fetch getReader() + TextDecoder, parsing
// `data: {json}\n\n` records. Calls `onStage` for each progress event and
// resolves with the final `result` payload (same shape the non-streaming
// endpoint returned). Falls back to a single JSON body when the runtime
// has no streaming body (e.g. the jsdom test env).
async function streamDescribeSynthesize(
  url: string,
  body: unknown,
  authFetch: (url: string, init: RequestInit) => Promise<Response>,
  onStage: (event: string, data: any) => void,
): Promise<{ result?: any; httpStatus?: number }> {
  const resp = await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) return { httpStatus: resp.status };
  const hasStream =
    !!resp.body && typeof (resp.body as any).getReader === "function";
  if (!hasStream) {
    const data = await resp.json().catch(() => null);
    return { result: data };
  }
  const reader = (resp.body as ReadableStream<Uint8Array>).getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: any = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      let evt: any;
      try {
        evt = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (evt.t === "progress") onStage(evt.event, evt.data || {});
      else if (evt.t === "result") finalResult = evt;
    }
  }
  return { result: finalResult };
}

function DescribeChatBubble({
  msg,
  chosenRepo,
  onPickRepo,
  onBuildFresh,
  onBuildIt,
  onSwitchToWorkflow,
  liveStatus,
  disabled,
}: {
  msg: ChatMessage;
  chosenRepo: string | null;
  onPickRepo: (fullName: string) => void;
  onBuildFresh: () => void;
  onBuildIt: () => void;
  onSwitchToWorkflow: () => void;
  liveStatus?: string;
  disabled: boolean;
}) {
  const isUser = msg.role === "user";
  const hasCandidates = !!(msg.candidates && msg.candidates.length > 0);
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        data-candidates-bubble={hasCandidates ? "true" : undefined}
        className={`max-w-[85%] rounded-lg px-3 py-2 text-[11px] leading-snug ${
          isUser
            ? "bg-indigo-600 text-white"
            : "bg-background border border-border text-foreground"
        }`}
      >
        <div className="whitespace-pre-wrap flex items-center gap-2">
          {msg.pending && <Loader2 className="w-3 h-3 animate-spin shrink-0" />}
          <span>{msg.text}</span>
        </div>
        {msg.pending && liveStatus && (
          <div className="mt-1 pl-5 text-[10px] text-muted-foreground tabular-nums">
            {liveStatus}
          </div>
        )}

        {msg.candidates && msg.candidates.length > 0 && (() => {
          // Backend pre-sorts by composite_score and flags at most one
          // candidate as is_recommended. Render that one as a big
          // "BEST MATCH" card; the rest are regular cards below.
          const recommended = msg.candidates.find((c) => c.is_recommended);
          const others = msg.candidates.filter((c) => !c.is_recommended);
          return (
            <div className="mt-2 space-y-2">
              {recommended && (
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => onPickRepo(recommended.full_name)}
                  className={`w-full text-left rounded-lg border-2 px-3 py-2.5 transition-colors shadow-sm ${
                    chosenRepo === recommended.full_name
                      ? "border-primary bg-primary/15 text-foreground"
                      : "border-primary/40 bg-primary/5 hover:bg-primary/10 text-foreground"
                  } ${disabled ? "opacity-50 cursor-not-allowed" : ""}`}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-primary shrink-0" />
                    <span className="text-[10px] font-bold uppercase tracking-wider text-primary">
                      Best match
                    </span>
                    {typeof recommended.composite_score === "number" && (
                      <span className="text-[10px] text-primary/70">
                        ({recommended.composite_score}% confidence)
                      </span>
                    )}
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-xs font-mono font-semibold truncate">
                      {recommended.full_name}
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <Badge variant="outline" className="text-[10px] h-4 px-1">
                        {recommended.license_spdx || "no license"}
                      </Badge>
                      <Badge variant="outline" className="text-[10px] h-4 px-1">
                        ★ {recommended.stars || 0}
                      </Badge>
                    </div>
                  </div>
                  {recommended.description && (
                    <div className="text-[11px] text-muted-foreground mt-1 line-clamp-2">
                      {recommended.description}
                    </div>
                  )}
                  {recommended.fit_reason && (
                    <div className="text-[11px] text-foreground/80 mt-1 italic">
                      {recommended.fit_reason}
                    </div>
                  )}
                  {Array.isArray(recommended.risk_flags) && recommended.risk_flags.length > 0 && (
                    <div className="text-[10px] text-amber-700 dark:text-amber-400 mt-1">
                      ⚠ {recommended.risk_flags.join(", ")}
                    </div>
                  )}
                </button>
              )}
              {others.length > 0 && (
                <>
                  {recommended && (
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground mt-1 mb-0.5">
                      Other options
                    </div>
                  )}
                  {others.map((c) => (
                    <button
                      key={c.full_name}
                      type="button"
                      disabled={disabled}
                      onClick={() => onPickRepo(c.full_name)}
                      className={`w-full text-left rounded-md border px-2 py-1.5 transition-colors ${
                        chosenRepo === c.full_name
                          ? "border-primary bg-primary/10 text-foreground"
                          : "border-border bg-background hover:bg-muted text-foreground"
                      } ${disabled ? "opacity-50 cursor-not-allowed" : ""}`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="text-[11px] font-mono truncate">{c.full_name}</div>
                        <div className="flex items-center gap-1 shrink-0">
                          <Badge variant="outline" className="text-[9px] h-4 px-1">
                            {c.license_spdx || "no license"}
                          </Badge>
                          <Badge variant="outline" className="text-[9px] h-4 px-1">
                            ★ {c.stars || 0}
                          </Badge>
                        </div>
                      </div>
                      {c.description && (
                        <div className="text-[10px] text-muted-foreground mt-0.5 line-clamp-2">
                          {c.description}
                        </div>
                      )}
                      {c.fit_reason && (
                        <div className="text-[10px] text-foreground/70 mt-0.5">{c.fit_reason}</div>
                      )}
                      {Array.isArray(c.risk_flags) && c.risk_flags.length > 0 && (
                        <div className="text-[10px] text-amber-700 dark:text-amber-400 mt-0.5">
                          ⚠ {c.risk_flags.join(", ")}
                        </div>
                      )}
                    </button>
                  ))}
                </>
              )}
            </div>
          );
        })()}

        {(msg.showBuildFreshButton || msg.showBuildItButton) && (
          <div className="mt-2 flex gap-2">
            {msg.showBuildFreshButton && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7 text-[11px]"
                disabled={disabled}
                onClick={onBuildFresh}
              >
                Build fresh instead
              </Button>
            )}
            {msg.showBuildItButton && (
              <Button
                type="button"
                size="sm"
                className="h-7 text-[11px]"
                disabled={disabled}
                onClick={onBuildIt}
              >
                <Sparkles className="w-3 h-3 mr-1" />
                Build it
              </Button>
            )}
          </div>
        )}

        {/* Multi-step request — the only forward action is the Workflow Wizard
            (no single-node fallback). */}
        {msg.showSwitchToWorkflowButton && (
          <div className="mt-2">
            <Button
              type="button"
              size="sm"
              className="h-7 text-[11px] bg-indigo-600 hover:bg-indigo-700 text-white"
              onClick={onSwitchToWorkflow}
            >
              <Sparkles className="w-3 h-3 mr-1" />
              Switch to {wbNavLabel('wb-nav-wizard-flow', 'Workflow Wizard')}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

// node-code-edit-rebuild — renders server-computed unified-diff text (as
// returned by GET /nodes/{id}/source/diff) with the same
// green/rose "+ / -" line styling build-variant-preview.tsx's DiffView
// uses for its before/after LCS diff. Kept separate from DiffView because
// this text arrives already diffed — there's nothing here to run an LCS
// over, just lines to color by their unified-diff prefix.
function UnifiedDiffText({ text }: { text: string }) {
  const lines = useMemo(() => text.split("\n"), [text]);
  if (lines.every((l) => !l.trim())) {
    return (
      <div className="px-3 py-4 text-center text-xs text-gray-500 italic">
        No changes in this file.
      </div>
    );
  }
  return (
    <div className="font-mono text-[11px] leading-snug max-h-[240px] overflow-auto border rounded">
      {lines.map((line, idx) => {
        const isHeader = line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@") || line.startsWith("diff ") || line.startsWith("index ");
        const isAdd = !isHeader && line.startsWith("+");
        const isDel = !isHeader && line.startsWith("-");
        const cls = isHeader
          ? "text-gray-400 bg-gray-50"
          : isAdd
            ? "bg-emerald-50 text-emerald-900"
            : isDel
              ? "bg-rose-50 text-rose-900"
              : "text-gray-600";
        return (
          <div key={idx} className={`whitespace-pre-wrap break-all px-2 py-[1px] ${cls}`}>
            {line || " "}
          </div>
        );
      })}
    </div>
  );
}

// sz-tcp.9 — tier-threshold hint copy for the Small / Medium / Large card.
// Pure formatter: turns the public GET /platform/credit-rate
// `credit_tier_thresholds` payload ({small_max_bytes, medium_max_bytes}) into
// per-tier copy ("Small: inputs ≤ 2 MB", "Medium: inputs ≤ 30 MB",
// "Large: inputs > 30 MB"). Any missing / non-positive / non-finite value
// falls back to the 2 / 30 MB defaults, so a failed fetch degrades silently.
const TIER_HINT_DEFAULT_SMALL_MAX_BYTES = 2 * 1024 * 1024;
const TIER_HINT_DEFAULT_MEDIUM_MAX_BYTES = 30 * 1024 * 1024;

function formatTierByteSize(bytes: number): string {
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb * 10) / 10} KB`;
  return `${Math.round((kb / 1024) * 10) / 10} MB`;
}

export function formatTierHint(
  thresholds: { small_max_bytes?: unknown; medium_max_bytes?: unknown } | null | undefined,
): { small: string; medium: string; large: string } {
  const parsedSmall = Number(thresholds?.small_max_bytes);
  const parsedMedium = Number(thresholds?.medium_max_bytes);
  const smallMax =
    Number.isFinite(parsedSmall) && parsedSmall > 0
      ? parsedSmall
      : TIER_HINT_DEFAULT_SMALL_MAX_BYTES;
  const parsedMediumOrSmallMax = Number.isFinite(parsedMedium) && parsedMedium > 0 ? parsedMedium : TIER_HINT_DEFAULT_MEDIUM_MAX_BYTES;
  const mediumMax = Math.max(parsedMediumOrSmallMax, smallMax);
  return {
    small: `inputs ≤ ${formatTierByteSize(smallMax)}`,
    medium: `inputs ≤ ${formatTierByteSize(mediumMax)}`,
    large: `inputs > ${formatTierByteSize(mediumMax)}`,
  };
}

export default function NewNodeWizard({ open, onClose, accessToken, onNodeCreated, resumeTaskId, resumeNodeName, resumeWizardSeed, onDeferGeneration, onSwitchToWorkflow, editNodeId, editNodeName, costBasis: costBasisIn }: NewNodeWizardProps) {
  // Hydrate on first mount. Every state below seeds from localStorage
  // when present so a remount of this component (for whatever reason
  // React decides) lands the user back where they were.
  const _persisted = loadPersisted() || {};

  // Step tracking
  // workflow-wizard-missing-node-handoff — when the parent opens the
  // wizard via the Workflow Wizard's "Build it now" button, the seed
  // payload carries an LLM-suggested source_type + source. Apply it on
  // mount; otherwise fall through to the localStorage-persisted value
  // (so a half-finished wizard run still rehydrates). The wizard's
  // `key` is bumped on every open, so useState initializers run fresh
  // each time — no need for a separate hydrate effect.
  const _seedSourceType = resumeWizardSeed?.sourceType;
  const _seedSource = resumeWizardSeed?.source || "";
  const _seedPurpose = resumeWizardSeed?.purpose || "";

  const [currentStep, setCurrentStep] = useState<Step>((_persisted.currentStep as Step) || "Source");

  // Source step
  const [sourceType, setSourceType] = useState<SourceType>(
    (_seedSourceType as SourceType | undefined) || (_persisted.sourceType as SourceType) || "image",
  );
  const [dockerImage, setDockerImage] = useState(
    _seedSourceType === "image" && _seedSource
      ? _seedSource
      : (_persisted.dockerImage || ""),
  );
  const [dockerfileContent, setDockerfileContent] = useState(
    _seedSourceType === "dockerfile" && _seedSource
      ? _seedSource
      : (_persisted.dockerfileContent || ""),
  );
  const [scriptContent, setScriptContent] = useState(
    _seedSourceType === "script" && _seedSource
      ? _seedSource
      : (_persisted.scriptContent || ""),
  );
  const [scriptInterpreter, setScriptInterpreter] = useState<string>(_persisted.scriptInterpreter || "python3");
  const [scriptFilename, setScriptFilename] = useState(_persisted.scriptFilename || "");
  // #14 round-3 — drag-drop zone for files or folders, with a click
  // fallback for the click-to-browse path. The native file picker
  // can't switch between "select files" and "select folder" in one
  // popup; drag-drop bridges that by accepting both via
  // DataTransferItem.webkitGetAsEntry().
  const [scriptDropActive, setScriptDropActive] = useState(false);
  const scriptFilePickerRef = useRef<HTMLInputElement | null>(null);
  // δ Phase 11 — repo-source safety gate. Polled when the user
  // edits the repo URL. ok=true → green check; ok=false → rose
  // banner with the reasons. Acts as a hard gate on canAdvance.
  const [repoSafety, setRepoSafety] = useState<{
    ok: boolean;
    reasons: string[];
    warnings: string[];
    license_spdx?: string | null;
    full_name?: string | null;
    not_github?: boolean;
    not_configured?: boolean;
  } | null>(null);
  const [repoSafetyChecking, setRepoSafetyChecking] = useState(false);

  // Image-mode preflight — verifies the docker ref actually resolves
  // against a public registry BEFORE the user submits. Catches the
  // "HuggingFace model path mistyped as docker image" trap
  // (runwayml/stable-diffusion-v1-5 etc.). When exists=false, the
  // Source step shows a red banner with the reason + LLM-suggested
  // real images, and canAdvance returns false until the user fixes
  // the ref or picks a suggestion.
  const [imageRefCheck, setImageRefCheck] = useState<{
    exists: boolean | null;
    reason: string;
    registry_source: string;
    suggestions: Array<{ ref: string; why: string }>;
  } | null>(null);
  const [imageRefChecking, setImageRefChecking] = useState(false);

  // δ §21 — Describe-and-build chat. The state machine in
  // `lib/describe-chat.ts` owns the transcript and step transitions.
  // `describeAvailable` and `isPlatformAdmin` stay separate because
  // they come from the feature-config probe, not the chat flow.
  const [describeAvailable, setDescribeAvailable] = useState(false);
  const [isPlatformAdmin, setIsPlatformAdmin] = useState(false);
  // g4: describe-search answered not_configured. The chat reducer drops its
  // NOT_CONFIGURED_PROMPT bubble once Q&A is already done (confident
  // I/O inference sets hasAskedQA), so surface the cause inline for everyone.
  const [describeSearchNotConfigured, setDescribeSearchNotConfigured] = useState(false);
  const [chatState, dispatchChat] = useReducer(reduceChat, initialChatState);
  // Live synth progress. The "Putting it together…" bubble is a single
  // in-flight POST that can run 30-90s for a complex node; without a
  // ticking status line it looks frozen. `synthTick` counts seconds
  // while step === "synthesizing"; `synthAttempt` is the current retry.
  const [synthTick, setSynthTick] = useState(0);
  const [synthAttempt, setSynthAttempt] = useState(1);
  // Piece 3: real backend stage streamed from /describe-synthesize-stream
  // (generating/reviewing/revising/escalating/seeded) + the exemplar name
  // during "seeded". Drives the live status line so the wizard shows what
  // it's actually doing instead of a bare spinner.
  const [synthStage, setSynthStage] = useState<string>("");
  const [synthStageName, setSynthStageName] = useState<string>("");
  // Provenance for the Review step: the prior working node (or built-in
  // reference recipe) this build was seeded from, and which kind it was.
  const [exemplarUsed, setExemplarUsed] = useState<string>("");
  const [exemplarSource, setExemplarSource] = useState<string>("");
  const [composerText, setComposerText] = useState(
    _seedSourceType === "describe" && _seedSource ? _seedSource : "",
  );

  // Drive the live synth timer. Runs a 1s tick only while a
  // describe-synthesize POST is in flight; resets to 0 on entry/exit so
  // each build starts the clock fresh.
  useEffect(() => {
    if (chatState.step !== "synthesizing") {
      setSynthTick(0);
      return;
    }
    setSynthTick(0);
    setSynthStage("");
    setSynthStageName("");
    const id = setInterval(() => setSynthTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [chatState.step]);

  // γ — real-data sample loop. The user can attach an input file on
  // any source type. When present, the build defers registration and
  // the wizard polls the build task to render captured sample output
  // for thumbs-up review before the NodeDefinition row is created.
  const [sampleInputFile, setSampleInputFile] = useState<File | null>(null);
  const [sampleInputFilename, setSampleInputFilename] = useState<string>(
    _persisted.sampleInputFilename || "",
  );
  const [sampleInputBase64, setSampleInputBase64] = useState<string>("");
  const sampleFilePickerRef = useRef<HTMLInputElement | null>(null);
  // γ — deferred-registration UI state. Set when the build poll
  // returns deferred_registration; drives the Preview thumbs UI.
  const [deferredRegistration, setDeferredRegistration] = useState<any | null>(null);
  const [sampleTestFailure, setSampleTestFailure] = useState<any | null>(null);
  const [finalizing, setFinalizing] = useState(false);
  const [declineReason, setDeclineReason] = useState("");
  const [showDeclineForm, setShowDeclineForm] = useState(false);
  // Build-task id from a defer submit so the Preview poll loop knows
  // which task to watch (separate from the artifact-generation taskId
  // already tracked above).
  const [deferredBuildTaskId, setDeferredBuildTaskId] = useState<number | null>(null);
  // #17 — when Auto mode runs to Preview, also fire the Confirm &
  // Build action automatically. This is the difference between
  // "auto-advance the form" (today's behavior) and "build the entire
  // node end-to-end on its own" (Tim's #17 ask). Set when auto-advance
  // completes; a useEffect below watches for Preview-ready state and
  // dispatches handleSubmit once.
  const [autoBuildPending, setAutoBuildPending] = useState(false);
  // #17/#20 (round 6) — Auto-build progress state. Replaces the
  // round-5 `backgroundMode` (which hid the dialog and made failures
  // invisible). Now: when the user clicks Build on Review with auto
  // mode on, we keep the dialog open and swap its body for a clear
  // progress panel showing phase 1 → phase 2 → done. The dialog
  // closes itself when phase 2 succeeds; on failure the error
  // banner is rendered inline so the user can see what went wrong.
  const [autoBuildPhase, setAutoBuildPhase] = useState<null | "starting" | "phase1" | "phase2" | "done" | "failed">(null);
  const [autoBuildPhaseDetail, setAutoBuildPhaseDetail] = useState<string>("");
  // Legacy alias retained so older code paths still compile while we
  // migrate. Always false in round-6 — keep the dialog visible.
  const backgroundMode = false;
  const setBackgroundMode = (_v: boolean) => { /* no-op in round 6 */ };

  // Curated base images — fetched from /nodes/base-images on
  // wizard open. The Script + Dockerfile tabs let the user pick one from
  // this list so tenant nodes always `FROM` a MOD-scanned base. See
  // CORE/API/utils/curated_base_images.py for the source-of-truth list.
  interface CuratedBaseImage {
    variant_dir: string;
    image_tag: string;
    image_ref: string;
    display_name: string;
    category: string;
    default_interpreter?: string | null;
    description: string;
    process_type_hint: string;
  }
  const [baseImages, setBaseImages] = useState<CuratedBaseImage[]>([]);
  const [selectedBaseImage, setSelectedBaseImage] = useState<string>(_persisted.selectedBaseImage || "python-3.11");

  // Phase B (#1) — synth-declared system binaries. The describe-
  // synthesize endpoint returns these alongside script_content; we
  // hold them in wizard state and pass them in the build submission
  // so the Containerizer's post-process can bake them into the
  // generated Dockerfile. Empty for non-describe paths (Script /
  // Image / Repo source-types).
  const [synthSystemPackages, setSynthSystemPackages] = useState<string[]>([]);
  // Critic D advisory notes returned by describe-synthesize (non-blocking
  // heads-ups like "may not fully match your request: ..."). Rendered in Review.
  const [synthWarnings, setSynthWarnings] = useState<string[]>([]);
  const [synthDockerfileExtras, setSynthDockerfileExtras] = useState<string>("");

  // Git repo
  const [repoUrl, setRepoUrl] = useState(
    _seedSourceType === "git" && _seedSource
      ? _seedSource
      : (_persisted.repoUrl || ""),
  );
  const [repoCredential, setRepoCredential] = useState(""); // secret name for git auth

  // Registry credentials (for pulling from Docker Hub, private Harbor, etc.)
  const [registryCredential, setRegistryCredential] = useState(""); // secret name for registry auth

  // Available secrets for credentials dropdown
  const [availableSecrets, setAvailableSecrets] = useState<{id: number, name: string}[]>([]);

  // Configure step. Hydration order: persisted > resume-prop > "".
  // Persisted wins so a wizard mid-edit (mounted but invisible)
  // doesn't lose what the user typed; resumeNodeName covers the
  // post-defer Resume case where persistence was deliberately wiped
  // by handleClose+notify, so the user's chosen name still rides
  // through into Phase 2's queue row instead of falling back to
  // "Task #<id>".
  const [nodeName, setNodeName] = useState(_persisted.nodeName || resumeNodeName || "");
  const [description, setDescription] = useState(_persisted.description || _seedPurpose || "");
  const [inputType, setInputType] = useState(_persisted.inputType || "image");
  const [outputType, setOutputType] = useState(_persisted.outputType || "image");
  const [processType, setProcessType] = useState(_persisted.processType || "CPU");
  // sz-tcp.5 — the wizard no longer asks for one cost: it asks for a
  // Small / Medium / Large tier card (three required number inputs).
  // `costInCredits` stays as the single source of truth for everything
  // downstream (DAG preview, persistence, /build-and-register-node) and
  // always mirrors the MEDIUM tier, so old payloads keep working; the
  // full tier set rides along as `cost_tiers: {small, medium, large}`.
  // Hydration prefers a persisted tier set, falling back to the legacy
  // scalar for drafts saved by an older build.
  const [costTiers, setCostTiers] = useState<{ small: number; medium: number; large: number }>(
    _persisted.costTiers
      || { small: (_persisted.costInCredits ?? 1), medium: (_persisted.costInCredits ?? 1), large: (_persisted.costInCredits ?? 1) }
  );
  // Backward-compat scalar: the MEDIUM tier, kept in its own state cell so
  // every existing consumer (DAG preview, persistence, /build-and-register-
  // node's `cost_in_credits`) keeps working untouched. The tier inputs below
  // write through applyTier, which keeps the two in lock-step.
  const [costInCredits, setCostInCredits] = useState(
    (_persisted.costTiers && _persisted.costTiers.medium)
      ?? _persisted.costInCredits
      ?? 1
  );
  // sz-tcp.7 — cost basis. The wizard starts at whatever the node being
  // edited already carries (edit-mode handoff) or null (= the server's
  // 'estimated' default for a fresh node). The moment the user touches any
  // tier input the basis flips to 'manual' and the untouched tier set they
  // were looking at is remembered as `suggestedTiers`, so the tier card can
  // offer a "Reset" back to the machine's guess.
  const [effCostBasis, setEffCostBasis] = useState<string | null>(
    _persisted.costBasis ?? costBasisIn ?? null
  );
  const [suggestedTiers, setSuggestedTiers] = useState<TierSet | null>(
    _persisted.suggestedTiers ?? null
  );
  const [basisBeforeEdit, setBasisBeforeEdit] = useState<string | null>(
    _persisted.basisBeforeEdit ?? costBasisIn ?? null
  );
  const applyTier = (tier: TierKey, value: number) => {
    if (suggestedTiers === null) setSuggestedTiers(costTiers);
    if (effCostBasis !== "manual") {
      setEffCostBasis("manual");
      setBasisBeforeEdit(effCostBasis);
    }
    setCostTiers(applyTierEdit(costTiers, tier, value));
    if (tier === "medium") setCostInCredits(value);
  };
  const resetTierEdit = () => {
    const restored = resetTiers(costTiers, suggestedTiers, basisBeforeEdit);
    setCostTiers(restored.tiers);
    setCostInCredits(restored.tiers.medium);
    setEffCostBasis(restored.basis);
    setSuggestedTiers(null);
    setBasisBeforeEdit(null);
  };
  // sz-tcp.9 — tier-threshold hint copy. Fetched ONCE per mount from the
  // public GET /platform/credit-rate; on any failure the defaults (2 / 30 MB)
  // stay in place silently (no error UI, no retry loop).
  //
  // 2400UI129 — CE ships no /platform/credit-rate (CORE/API/routers/platform_credit_rate.py
  // is DENIED in open-core/manifest.yaml), so the hint fetch must not fire there.
  // Same edition mechanism as 2400UI16 (assistant-panel.tsx) and 0958a48c3
  // (workflow-builder.tsx `isProjectWizardCE`): useEntitlements(), hidden ONLY
  // when the block positively reports edition "ce" — FAIL-OPEN otherwise (unresolved
  // / errored → edition null → paid behaviour untouched). While the block is still
  // loading we do not fetch at all: the one-shot gate below releases when `loading`
  // flips false, so CE never races a 404 on the first /platform/features round-trip.
  const { edition: creditRateEdition, loading: creditRateLoading } = useEntitlements();
  const creditRateCE = creditRateEdition === "ce";
  const creditRateCeRef = useRef(false);
  const creditRateGateRef = useRef<{ wait: Promise<void>; release: () => void } | null>(null);
  const creditRateReleasedRef = useRef(false);
  if (!creditRateGateRef.current) {
    let release: (() => void) | null = null;
    const wait = new Promise<void>((resolve) => { release = resolve; });
    creditRateGateRef.current = { wait, release: release ?? (() => {}) };
  }
  useEffect(() => {
    if (creditRateCE) creditRateCeRef.current = true;
    if (!creditRateLoading && creditRateGateRef.current && !creditRateReleasedRef.current) {
      creditRateReleasedRef.current = true;
      creditRateGateRef.current.release();
    }
    // Bounded fallback: window.fetch has no timeout, so a hung /platform/features
    // would leave `loading` true forever and the gate would never release (paid
    // hint copy would never refresh). release() is idempotent.
    const fallback = setTimeout(() => {
      if (creditRateGateRef.current) creditRateGateRef.current.release();
    }, 5000);
    return () => clearTimeout(fallback);
  }, [creditRateEdition, creditRateLoading]);
  const [tierHints, setTierHints] = useState<{ small: string; medium: string; large: string }>(
    formatTierHint(null)
  );
  useEffect(() => {
    if (!open || !accessToken) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      await creditRateGateRef.current!.wait;
      if (cancelled || creditRateCeRef.current) return;
      try {
        const resp = await authenticatedFetch(`${API_URL}/platform/credit-rate`, {});
        if (!resp.ok) return;
        const data = await resp.json();
        if (!cancelled) setTierHints(formatTierHint(data?.credit_tier_thresholds));
      } catch {
        /* silent: keep the 2/30 MB default hint copy */
      }
    })();
    return () => { cancelled = true; };
  }, [open, accessToken]);
  const [containerTimeout, setContainerTimeout] = useState(_persisted.containerTimeout ?? 7200);

  // Smart-category state (manual-test-driven 2026-05-22). Defaults to
  // empty so the Review-step fetch can populate it from the LLM
  // suggester. User edits in-place via a combobox of existing
  // categories. Empty → server falls back to "Custom".
  const [nodeType, setNodeType] = useState<string>("");
  // Combobox state for the Category field — popover open + cmdk search.
  // Kept here next to nodeType so the picker is a thin UI wrapper around
  // the same source of truth (nodeType is what gets submitted).
  const [nodeTypeOpen, setNodeTypeOpen] = useState(false);
  const [nodeTypeSearch, setNodeTypeSearch] = useState<string>("");
  const [nodeTypeSuggestion, setNodeTypeSuggestion] = useState<{
    suggestion: string;
    existing: string[];
    rationale: string;
    source: string;
  } | null>(null);
  const [nodeTypeLoading, setNodeTypeLoading] = useState(false);

  // Variables step
  const [envVars, setEnvVars] = useState<EnvVarDef[]>([]);

  // Network step — per-node egress allowlist. Default deny-all; user opts
  // into preset hosts or adds custom entries. Posted to the API as
  // allowed_egress on the node definition; enforced by the worker runtime
  // (mod-user-internal vs mod-user-egress Docker networks).
  const [egressHosts, setEgressHosts] = useState<Set<string>>(new Set());
  const [customEgressInput, setCustomEgressInput] = useState("");
  const [useSandboxRuntime, setUseSandboxRuntime] = useState(false);

  // Submission
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [taskId, setTaskId] = useState<number | null>(null);

  // Client-side syntax-check state. Populated by a debounced call to
  // /check-syntax whenever the user edits the Source step. Non-blocking
  // UX: we render the error inline + disable the Next button until the
  // code parses. Null = not yet checked / passed; object = failure.
  const [syntaxError, setSyntaxError] = useState<{ line: number | null; error: string } | null>(null);
  const [syntaxChecking, setSyntaxChecking] = useState(false);

  // LLM status — polled on wizard open so the user knows whether the
  // first analysis will be fast (model resident) or slow (cold-load).
  const [llmStatus, setLlmStatus] = useState<{
    loaded: boolean;
    model: string;
    detail: string;
    size_bytes?: number;
  } | null>(null);

  // Worker-side reachability ping state. Clicking "check worker" on
  // the Source step dispatches a diagnostic Containerizer task that
  // just curls the LLM. Result gets toasted — green if reachable, red
  // with remediation if DNS / routing is broken. Isolates "wizard
  // submit failed" from "can a worker-spawned container hit Ollama".
  const [isPingingWorker, setIsPingingWorker] = useState(false);

  // LLM-driven auto-fill — fires on Source → Next. Set of field keys
  // that were AI-filled so we can badge them + show an Undo affordance.
  // Cleared once the user edits the field manually.
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisReasoning, setAnalysisReasoning] = useState<string>("");
  const [analysisConfidence, setAnalysisConfidence] = useState<"high" | "medium" | "low" | null>(null);
  // Build-time prediction surfaced from /analyze-script. The API
  // already computes these from the recipe cache + library profiles
  // (high-confidence prior builds → 2 attempts/~8min; medium → 3/~15;
  // first-time → 8/~25; etc). Rendering them next to the AI banner
  // sets user expectations BEFORE they click Create Node, so a
  // 4-attempt repair loop doesn't surprise them.
  const [predictedAttempts, setPredictedAttempts] = useState<number | null>(null);
  const [predictedMinutes, setPredictedMinutes] = useState<number | null>(null);
  const [predictionBasis, setPredictionBasis] = useState<string>("");
  const [aiFilledFields, setAiFilledFields] = useState<Set<string>>(new Set());
  const lastAnalyzedScriptRef = useState(() => ({ content: "" }))[0];
  const [skipToReview, setSkipToReview] = useState(false);

  // Preview step (image flow) — LLM-generated Albedo-pattern artifacts
  const [isGenerating, setIsGenerating] = useState(false);
  // True only during the *resume* hydration window (clicking Resume on
  // a Phase 1 done row, or a notification's resume= deep link). Drives
  // a prominent loading skeleton on the Preview step so users don't
  // see an empty Preview shell that looks like the wizard is broken
  // (issue #7). isGenerating alone doesn't disambiguate live-generation
  // from resume-hydration, hence the extra flag.
  const [isResumeHydrating, setIsResumeHydrating] = useState(false);

  // Auto-build mode (issue #3). When on, after the user submits the
  // Source step's content the wizard auto-advances through the
  // pre-filled Configure / Variables / Network / Review steps until
  // it lands on Preview, where the user reviews the LLM-generated
  // artifacts before clicking Confirm and Build. Each intermediate
  // step renders briefly (~120ms) so the user can intercept and
  // edit any field they want; clicking the auto-build pill cancels
  // the chain at the next step boundary.
  const [autoMode, setAutoMode] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    try {
      const raw = window.localStorage.getItem("mod.wizard.autoMode");
      // Default-on for first-time users; explicit "false" turns it
      // off until the user toggles it back on.
      return raw !== "false";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem("mod.wizard.autoMode", String(autoMode));
    } catch {
      /* quota — ignore */
    }
  }, [autoMode]);
  // True while the auto-advance chain is running. Drives the
  // "Auto-build running — click to pause" pill on the wizard
  // header. A click flips this to false; the chain reads it on
  // the next loop iteration and bails.
  const [autoAdvancing, setAutoAdvancing] = useState(false);
  const autoAdvanceCancelledRef = useRef(false);

  // Auto-scroll sentinel for the Describe chat — see usage near the
  // chat transcript. Scrolls into view on every step transition / new
  // message so the user always sees the latest bubble + CTA without
  // needing to know to scroll manually.
  const chatBottomRef = useRef<HTMLDivElement | null>(null);

  // Tier 2 #1 — credit pre-flight. Fetched once when the wizard
  // opens so the user sees the gate BEFORE spending 30 minutes on
  // a build that registration will reject for insufficient credits
  // (the failure mode the TRELLIS.2 driver run hit at the very
  // last step). null = not fetched yet / fetch failed; banner is
  // hidden in that case so a flaky preflight never blocks the user.
  type WizardPreflight = {
    node_creation_cost: number;
    credits_remaining: number | null;
    credits_total?: number;
    can_create_node: boolean;
    can_advance?: boolean;
    reason: string | null;
    // Disk-space warning from the docker host. Populated when the API
    // detects the docker daemon's root partition is below the safety
    // floor. Builds will still be allowed (auto-repair gates fail loud
    // when they actually run), but the wizard surfaces this upfront so
    // users don't sink 5+ min into a build that's going to fail on
    // disk pressure with misleading "docker socket" errors.
    disk_warning?: {
      free_gb: number;
      min_gb: number;
      message: string;
    } | null;
  };
  const [creditPreflight, setCreditPreflight] = useState<WizardPreflight | null>(null);
  useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await authenticatedFetch(`${API_URL}/nodes/wizard-preflight`, {});
        if (!resp.ok) return;
        const data = await resp.json();
        if (!cancelled) setCreditPreflight(data as WizardPreflight);
      } catch {
        /* fail-open — no banner if preflight is unreachable */
      }
    })();
    return () => { cancelled = true; };
  }, [accessToken]);

  // Cluster-capability defaulting. On mount, ask the API which
  // accelerators this deployment's worker fleet actually has. Default
  // processType to the recommended one — so a user on an AMD-only
  // deployment sees "ROCm" pre-selected rather than the legacy CPU
  // default, and a user on a CPU-only deployment doesn't see GPU
  // options they can't run.
  //
  // Only nudges processType if (a) the user hasn't already picked
  // something AND (b) nothing was persisted from a prior session.
  // Failure is silent — falling back to the existing "CPU" default
  // is safe; we never want a flaky capabilities endpoint to block
  // the wizard from opening.
  type ClusterCapabilities = {
    available: string[];     // ["cuda", "rocm", "intel", "npu", "cpu"]
    recommended: string;     // "cuda"
    description: string;     // human-readable hint
  };
  const [clusterCapabilities, setClusterCapabilities] = useState<ClusterCapabilities | null>(null);
  useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await authenticatedFetch(`${API_URL}/woodsman/wizard/cluster-capabilities`, {});
        if (!resp.ok) return;
        const data = await resp.json() as ClusterCapabilities;
        if (cancelled) return;
        setClusterCapabilities(data);
        // Only override processType if the user is still on the legacy
        // default ("CPU") AND no value was persisted from before. The
        // recommended capability is lower-case from the API; wizard
        // state uses upper-case to match the curated_base_images
        // process_type_hint convention.
        const recommendedUpper = (data.recommended || "cpu").toUpperCase();
        if (!_persisted.processType && recommendedUpper !== "CPU") {
          setProcessType((curr) => (curr === "CPU" ? recommendedUpper : curr));
        }
      } catch {
        /* fail-open — wizard still works with the CPU default */
      }
    })();
    return () => { cancelled = true; };
  }, [accessToken]);

  // δ Phase 11 — debounced repo URL safety check. Hits
  // /check-repo-safety when the user pauses typing for ~600 ms.
  // Clears the cached result if the URL is empty.
  useEffect(() => {
    if (!accessToken) return;
    if (sourceType !== "repo") return;
    const url = repoUrl.trim();
    if (!url) {
      setRepoSafety(null);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      setRepoSafetyChecking(true);
      const API_URL = apiUrl();
      try {
        const resp = await authenticatedFetch(`${API_URL}/woodsman/check-repo-safety`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo_url: url }),
        });
        if (!resp.ok) {
          if (!cancelled) setRepoSafety(null);
          return;
        }
        const data = await resp.json();
        if (!cancelled) setRepoSafety(data);
      } catch {
        if (!cancelled) setRepoSafety(null);
      } finally {
        if (!cancelled) setRepoSafetyChecking(false);
      }
    }, 600);
    return () => { cancelled = true; window.clearTimeout(handle); };
  }, [accessToken, sourceType, repoUrl]);

  // Smart category suggestion (manual-test-driven 2026-05-22).
  // Fires when the user lands on Review with a node_name set. The
  // LLM picks from the tenant's existing categories first, falling
  // back to a fresh proposal. User can override via the dropdown.
  // Cancels in-flight on unmount / step change so we don't race
  // ourselves when the user navigates back and forward.
  useEffect(() => {
    if (!accessToken) return;
    if (currentStep !== "Review") return;
    if (!nodeName.trim()) return;
    // Don't re-fetch once the user has explicitly chosen a value.
    if (nodeType.trim()) return;
    let cancelled = false;
    setNodeTypeLoading(true);
    (async () => {
      const API_URL = apiUrl();
      try {
        const resp = await authenticatedFetch(
          `${API_URL}/nodes/suggest-category`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              node_name: nodeName,
              description,
              base_image: sourceType === "image" ? dockerImage : undefined,
              source_body: sourceType === "script" ? scriptContent : undefined,
              repo_url: sourceType === "repo" ? repoUrl : undefined,
            }),
          },
        );
        if (!resp.ok || cancelled) return;
        const data = await resp.json();
        if (cancelled) return;
        setNodeTypeSuggestion({
          suggestion: data.suggestion,
          existing: data.existing_categories || [],
          rationale: data.rationale || "",
          source: data.source || "llm",
        });
        // Pre-fill the input with the suggestion; user can edit.
        setNodeType(data.suggestion || "");
      } catch {
        // Silently swallow — server falls back to "Custom" if we
        // don't send a node_type.
      } finally {
        if (!cancelled) setNodeTypeLoading(false);
      }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken, currentStep, nodeName]);

  // Image-mode preflight — debounced check that the docker ref
  // actually resolves on a public registry. Mirrors the repoSafety
  // pattern. Fires ~600ms after the user pauses typing. Clears
  // when the field is empty.
  useEffect(() => {
    if (!accessToken) return;
    if (sourceType !== "image") {
      setImageRefCheck(null);
      return;
    }
    const ref = dockerImage.trim();
    if (!ref) {
      setImageRefCheck(null);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      setImageRefChecking(true);
      const API_URL = apiUrl();
      try {
        const resp = await authenticatedFetch(`${API_URL}/woodsman/check-image-ref`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            image_ref: ref,
            node_name: nodeName,
            input_type: inputType,
            output_type: outputType,
            description,
          }),
        });
        if (!resp.ok) {
          if (!cancelled) setImageRefCheck(null);
          return;
        }
        const data = await resp.json();
        if (!cancelled) setImageRefCheck(data);
      } catch {
        if (!cancelled) setImageRefCheck(null);
      } finally {
        if (!cancelled) setImageRefChecking(false);
      }
    }, 600);
    return () => { cancelled = true; window.clearTimeout(handle); };
  }, [accessToken, sourceType, dockerImage, nodeName, inputType, outputType, description]);

  // δ Phase 9 — feature-config probe gates the Describe tab on
  // whether the platform admin has set the github_token secret.
  // Refetched on every wizard open; cheap (one DB round-trip).
  useEffect(() => {
    if (!open || !accessToken) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await authenticatedFetch(`${API_URL}/platform/feature-config`, {});
        if (!resp.ok) return;
        const data = await resp.json();
        if (!cancelled) {
          setDescribeAvailable(!!data.describe_and_build_available);
          setIsPlatformAdmin(!!data.is_platform_admin);
        }
      } catch {
        /* fail-closed: tab stays disabled if we can't reach the API */
      }
    })();
    return () => { cancelled = true; };
  }, [open, accessToken]);

  // δ §21 — describe-search call. Driven by the chat reducer:
  // dispatch search_resolved / search_failed when /describe-search
  // returns. The reducer routes the result onto the right next step
  // (choose_repo, ask_input on no-results / not-configured / rate-
  // limited, or back to ask_input on failure).
  // human-assisted-discovery (Tier 2): only ask the "name a tool" question ONCE
  // per description. A fresh search (no answer) resets it; the answer re-fire
  // keeps it set so a still-weak result routes normally instead of re-asking.
  const askedForToolRef = useRef(false);
  const runDescribeSearch = useCallback(async (description: string, opts?: { userToolAnswer?: string }) => {
    if (!accessToken) return;
    if (!opts?.userToolAnswer) askedForToolRef.current = false;
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(`${API_URL}/woodsman/describe-search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          ...(opts?.userToolAnswer ? { user_tool_answer: opts.userToolAnswer } : {}),
        }),
      });
      if (!resp.ok) {
        // 2400LR-wb12f — surface the server's named cause (FastAPI
        // {"detail": {"error"|"code": ..., "message": ...}} or a string
        // detail) instead of a bare "HTTP <status>".
        let code: string | undefined;
        let serverMessage: string | undefined;
        try {
          const body = await resp.json();
          const detail = body?.detail ?? body;
          if (typeof detail === "string") {
            serverMessage = detail;
          } else if (detail && typeof detail === "object") {
            const c = detail.error ?? detail.code;
            if (typeof c === "string") code = c;
            if (typeof detail.message === "string") serverMessage = detail.message;
            if (!code && detail.not_configured === true) code = "not_configured";
          }
        } catch { /* body not JSON — keep the generic path */ }
        dispatchChat({ type: "search_failed", reason: `HTTP ${resp.status}`, code, serverMessage });
        return;
      }
      const data = await resp.json();
      // Multi-step request → TERMINAL route to the Workflow Wizard. There is NO
      // single-node fallback: the chat shows the reason + a "Switch to Workflow
      // Wizard" button (the composer is disabled in this step), and we stop the
      // node flow here.
      //
      // EXCEPT when this wizard was opened BY the Workflow Wizard to build a
      // specific missing node (resumeWizardSeed present). That node is already a
      // decomposed leaf of a workflow — re-routing it would be an infinite loop
      // (Workflow Wizard → "build this node" → Node Wizard → "go to Workflow
      // Wizard" → …). Build it as a node.
      if (data.surface === "workflow" && !resumeWizardSeed) {
        dispatchChat({ type: "route_to_workflow", reason: (data.surface_reason || "").toString() });
        return;
      }
      // human-assisted-discovery (Tier 2): a weak pool asks the user for a
      // tool/keyword ONCE, instead of silently going to build-fresh. The answer
      // re-fires this search with user_tool_answer (askedForToolRef set), so a
      // still-weak result then routes normally rather than re-asking.
      if (data.needs_clarification && !askedForToolRef.current) {
        dispatchChat({
          type: "search_needs_clarification",
          question: (data.clarifying_question || "").toString(),
        });
        return;
      }
      setDescribeSearchNotConfigured(!!data.not_configured);
      if (data.not_configured) {
        dispatchChat({ type: "search_resolved", candidates: [], notConfigured: true });
      } else if (data.rate_limited) {
        dispatchChat({ type: "search_resolved", candidates: [], rateLimited: true });
      } else {
        dispatchChat({
          type: "search_resolved",
          candidates: data.candidates || [],
          recommendBuildFresh: !!data.recommend_build_fresh,
        });
      }
    } catch (err: any) {
      dispatchChat({ type: "search_failed", reason: err?.message || "network error" });
    }
  }, [accessToken, resumeWizardSeed]);

  // δ §21 — describe-synthesize call with auto-retry. Synth validators
  // (Phase A binary-presence, #2 I/O contract, #3 licence, URL HEAD)
  // produce technical rejection text that's meaningful to the LLM but
  // not to the end user. Loop up to MAX_SYNTH_RETRIES, feeding the
  // last attempt's script + error back as `previous_attempt` so the
  // LLM can correct itself. Only surface the failure to the user
  // (via `synthesize_failed`) after the budget is exhausted OR a
  // non-retryable failure mode hits (network error, rate-limit,
  // non-200 HTTP). On success, prefills the script-source state and
  // dispatches `synthesize_succeeded`.
  const runDescribeSynthesize = useCallback(async (opts?: { extraRefinement?: string; alreadyClarified?: boolean }) => {
    if (!accessToken) return;
    let description = buildSynthesizeDescription(chatState);
    if (opts?.extraRefinement && opts.extraRefinement.trim()) {
      // Critic D clarify re-fire: chatState.refinements doesn't yet include the
      // just-typed answer (the dispatch hasn't committed), so fold it in here.
      description += `\n\nRefinements from the user:\n- ${opts.extraRefinement.trim()}`;
    }
    let repoUrl: string | undefined;
    let installStrategy: string | undefined;
    if (chatState.chosenRepo) {
      const repoMsg = [...chatState.messages].reverse().find(m => m.candidates && m.candidates.length > 0);
      const repoMeta = repoMsg?.candidates?.find(c => c.full_name === chatState.chosenRepo);
      repoUrl = repoMeta?.html_url;
      installStrategy = repoMeta?.install_strategy;
    }
    const contract = buildSynthesizeContract(chatState);
    const baseBody: Record<string, unknown> = { description };
    if (chatState.chosenRepo && repoUrl) {
      baseBody.repo_url = repoUrl;
      baseBody.repo_full_name = chatState.chosenRepo;
      if (installStrategy) baseBody.install_strategy = installStrategy;
    }
    if (contract.user_input_type) baseBody.user_input_type = contract.user_input_type;
    if (contract.user_output_type) baseBody.user_output_type = contract.user_output_type;
    // Critic D v2: on the re-synth after the user answered a clarifying
    // question, tell the critic not to ask again.
    if (opts?.alreadyClarified) baseBody.already_clarified = true;

    // The clarify re-fire already transitioned the chat to "synthesizing" via
    // user_send; only the normal "Build it" path needs to dispatch build_it
    // (which adds the pending "building…" bubble).
    if (!opts?.alreadyClarified) dispatchChat({ type: "build_it" });
    const API_URL = apiUrl();

    // Generous retry budget so the LLM has room to iterate through
    // a multi-step failure ladder (typical openMVG-class repo: first
    // attempt misses dockerfile_extras, second attempt picks wrong
    // tag, third attempt hits a URL HEAD validator, fourth lands
    // clean). Each retry threads the previous attempt's script +
    // joined rejection reasons as `previous_attempt` so the LLM
    // sees exactly what failed and why.
    const MAX_SYNTH_RETRIES = 10;
    let prevScript: string | undefined;
    let prevError: string | undefined;
    let lastReason: string | undefined;
    let lastCategory: string | undefined;

    for (let attempt = 1; attempt <= MAX_SYNTH_RETRIES; attempt++) {
      setSynthAttempt(attempt);
      const body: Record<string, unknown> = { ...baseBody };
      if (prevScript || prevError) {
        body.previous_attempt = {
          script: prevScript || "",
          error: prevError || "",
        };
      }
      try {
        // Piece 3: stream the synth so the bubble shows real backend stages
        // (generating → reviewing → revising → escalating) instead of a
        // frozen spinner. `data` is the final result payload — same shape
        // the non-streaming endpoint returned, so the logic below is
        // unchanged.
        const { result: data, httpStatus } = await streamDescribeSynthesize(
          `${API_URL}/woodsman/describe-synthesize-stream`,
          body,
          authenticatedFetch,
          (event, edata) => {
            setSynthStage(event);
            if (event === "seeded" && edata?.name) setSynthStageName(String(edata.name));
          },
        );
        if (httpStatus) {
          // Non-200 from the API. Don't retry — likely auth /
          // rate-limit / system-level (the LLM doesn't even get a
          // chance to redo it). Surface immediately.
          // 502/503/504 = infrastructure (backend down or warming up),
          // NOT a build failure — the reducer renders the infra state
          // (no retry button) instead of the "wrapper script didn't
          // pass" wording.
          dispatchChat({
            type: "synthesize_failed",
            reason: `HTTP ${httpStatus}`,
            category: classifySynthFailure({ httpStatus }),
          });
          return;
        }
        if (!data) {
          dispatchChat({ type: "synthesize_failed", reason: "stream ended without a result" });
          return;
        }
        // Critic D v2 — genuinely ambiguous intent: ask the user ONE question
        // instead of proceeding. Surface it in the describe chat; the user's
        // answer re-fires synthesize with already_clarified=true.
        if (data.needs_clarification) {
          dispatchChat({ type: "synthesize_needs_clarification", question: data.clarifying_question || "" });
          return;
        }
        if (data.ok) {
          // SUCCESS — auto-fill the Script-source state.
          const reqs = (data.requirements || []) as string[];
          const scriptBody = data.script_content || "";
          // Only prepend a pip-requirements header when the script doesn't
          // already carry one. Curated recipes (and some generated scripts)
          // embed their own `# pip-requirements:` line; prepending unconditionally
          // produced a duplicated header at the top of the editor.
          const hasReqHeader = /^\s*#\s*pip-requirements\s*:/im.test(scriptBody);
          const reqHeader = (reqs.length > 0 && !hasReqHeader)
            ? `# pip-requirements:\n# ${reqs.join("\n# ")}\n\n`
            : "";
          setScriptContent(reqHeader + scriptBody);
          setScriptFilename(`${(nodeName || "describe").replace(/[^a-zA-Z0-9_-]+/g, "_")}.py`);
          setScriptInterpreter("python3");
          if (data.base_image_hint) setSelectedBaseImage(data.base_image_hint);
          if (data.suggested_input_type) setInputType(data.suggested_input_type);
          if (data.suggested_output_type) setOutputType(data.suggested_output_type);
          setSynthSystemPackages(Array.isArray(data.system_packages) ? data.system_packages : []);
          setSynthDockerfileExtras(typeof data.dockerfile_extras === "string" ? data.dockerfile_extras : "");
          setSynthWarnings(Array.isArray(data.warnings) ? data.warnings.filter((w: unknown) => typeof w === "string") : []);
          setExemplarUsed(typeof data.exemplar_used === "string" ? data.exemplar_used : "");
          setExemplarSource(typeof data.exemplar_source === "string" ? data.exemplar_source : "");
          setSourceType("script");
          dispatchChat({ type: "synthesize_succeeded" });
          toast({
            title: attempt > 1 ? `Wrapper synthesised (after ${attempt} attempts)` : "Wrapper synthesised",
            description: "Edit the generated script if needed. Attaching a sample input is recommended (catches semantic bugs that synthetic smoke misses) but no longer required.",
          });
          return;
        }
        // data.ok === false — capture for the next attempt's
        // previous_attempt, then decide whether to retry.
        const reasons = (data.rejection_reasons || []) as string[];
        lastReason = reasons.length > 0
          ? reasons.join("; ")
          : data.rate_limited ? "rate-limited"
          : "rejected";
        lastCategory = typeof data.rejection_category === "string"
          ? data.rejection_category
          : undefined;
        // Rate-limited won't fix on retry — bail and let the user
        // see the rate-limit message rather than burning the rest
        // of the budget on guaranteed failures.
        if (data.rate_limited) {
          dispatchChat({ type: "synthesize_failed", reason: lastReason, category: lastCategory });
          return;
        }
        // Stash for the next attempt. The LLM's prior script (even
        // when rejected) plus the validator's reasons let it correct
        // its own work rather than starting from scratch.
        if (typeof data.script_content === "string" && data.script_content.trim()) {
          prevScript = data.script_content;
        }
        prevError = lastReason;
        // Loop continues to next attempt.
      } catch (err: unknown) {
        // Network / parse error — don't retry. Surface immediately
        // so the user can decide whether to wait and try again.
        // fetch rejects (TypeError: Failed to fetch, load failed, …)
        // mean the backend is unreachable — an infrastructure state,
        // not a build failure.
        const msg = err instanceof Error ? err.message : "network error";
        dispatchChat({
          type: "synthesize_failed",
          reason: msg,
          category: classifySynthFailure({ networkError: true }),
        });
        return;
      }
    }

    // Budget exhausted. Surface the last rejection so the user can
    // either pick a different repo (licence flow re-renders the
    // candidate list) or retry manually (Build-it button).
    dispatchChat({
      type: "synthesize_failed",
      reason: lastReason || `synthesis failed after ${MAX_SYNTH_RETRIES} attempts`,
      category: lastCategory,
    });
  }, [accessToken, chatState, nodeName]);

  // Composer "Send" handler. Routes the user's input by current step:
  //  - initial: this IS the description → kick off /describe-search
  //  - ask_input / ask_output: scripted Q&A answer → reducer records it
  //  - choose_repo: push-back ("these aren't right") → reducer augments
  //    the description, transitions to searching, we re-run search
  //  - ready_to_build: refinement to factor into synthesize → reducer
  //    stacks it and stays put; no API call
  const handleComposerSend = useCallback(() => {
    const text = composerText.trim();
    if (!text) return;
    setComposerText("");
    const fromStep = chatState.step;
    const priorDescription = chatState.description;
    const priorInputAnswer = chatState.inputAnswer;
    const priorChosenRepo = chatState.chosenRepo;
    dispatchChat({ type: "user_send", text });
    if (fromStep === "initial") {
      // New inference path (lib/describe-chat.ts inferTypesFromDescription):
      // when the user's first message contains a confident "X to Y"
      // pattern, the reducer skips ask_input + ask_output and goes
      // straight to `searching`. We have to mirror that decision here
      // so the actual /describe-search call fires — the reducer
      // controls UI state, this callback controls the side-effects.
      // Without this branch, the wizard would sit in "searching"
      // forever because the search request never went out.
      const inferred = inferTypesFromDescription(text);
      if (inferred.confident && inferred.input && inferred.output) {
        runDescribeSearch(text);
      }
      // Non-inferred → reducer went to ask_input, no API call yet
      // (search fires later when the user answers ask_output).
    } else if (fromStep === "choose_repo") {
      // Reducer just augmented the description with this text; replay
      // the same combination here for the search call (we can't read
      // the post-dispatch state synchronously).
      const augmented = `${priorDescription}\n\n${text}`.trim();
      runDescribeSearch(augmented);
    } else if (fromStep === "ask_output" && !priorChosenRepo) {
      // Second search pass — the initial search came up empty on a
      // sparse description; fold the Q&A answers in and try again.
      // (No re-search when a repo was already picked: we have what
      // we need.)
      const enriched = [
        priorDescription,
        priorInputAnswer ? `Input: ${priorInputAnswer}` : "",
        `Output: ${text}`,
      ].filter(Boolean).join("\n\n");
      runDescribeSearch(enriched);
    } else if (fromStep === "clarify") {
      // Critic D asked a clarifying question; the user just answered. Re-fire
      // synthesize with the answer folded in and already_clarified=true so the
      // critic won't ask again. (Reducer already moved us to "synthesizing".)
      runDescribeSynthesize({ extraRefinement: text, alreadyClarified: true });
    } else if (fromStep === "search_clarify") {
      // Discovery was weak and asked the user to name a tool/keyword; they just
      // answered. Re-fire the search on the ORIGINAL description with the answer.
      // askedForToolRef stays set so a still-weak result routes normally.
      askedForToolRef.current = true;
      runDescribeSearch(priorDescription, { userToolAnswer: text });
    }
    // ask_input / ready_to_build are reducer-only.
  }, [composerText, chatState.step, chatState.description, chatState.inputAnswer, chatState.chosenRepo, runDescribeSearch, runDescribeSynthesize]);

  // The describe-chat assistant used to fire an async LLM "rewrite"
  // on every newly-appended scripted prompt — POSTing to
  // /describe-rewrite-prompt and then dispatching `replace_message_text`
  // to swap the visible text in place. The race that produced was: the
  // user saw the scripted question (e.g. "what kind of thing goes
  // in?"), started reading, and a few hundred ms later the message
  // text visibly changed under them. From their seat it read as the
  // bot "changing its own question". The rewrite was best-effort
  // polish at best and the cost (looking unprofessional, mid-read
  // mutation) is not worth it. Scripted prompts stay as they were
  // written — predictable, calm, no surprise. If we want richer
  // copy later, do it BEFORE rendering, not after.


  // Auto-scroll the Describe chat to the latest bubble + CTA whenever
  // the transcript or step changes. Without this, clicking a repo
  // card spawns "ready_to_build" below the candidate list with no
  // visual cue — users missed the "Build it" button. Uses behavior:
  // "smooth" so the motion telegraphs that something new appeared.
  useEffect(() => {
    if (sourceType !== "describe") return;
    const sentinel = chatBottomRef.current;
    if (!sentinel) return;
    // Defer to next frame so the DOM has the new bubble committed.
    const id = window.requestAnimationFrame(() => {
      // When the latest bubble holds a candidate list, scroll the
      // TOP of that bubble into view — never the bottom of the chat.
      // Auto-scrolling to the bottom would drop the highest-ranked
      // repos out of sight and put the long-tail (lower-quality)
      // candidates front-and-center, which biases the user toward
      // picking low-quality repos by visual proximity. Once the user
      // clicks a repo (chatState.chosenRepo set) or the chat
      // transitions to ready_to_build, fall back to scrolling the
      // bottom sentinel so the "Build it" CTA is visible.
      const candidatesBubbles = document.querySelectorAll(
        '[data-candidates-bubble="true"]'
      );
      const latestCandidates = candidatesBubbles[candidatesBubbles.length - 1] as HTMLElement | undefined;
      const shouldShowCandidatesTop =
        chatState.step === "choose_repo"
        && !chatState.chosenRepo
        && latestCandidates;
      try {
        if (shouldShowCandidatesTop) {
          latestCandidates!.scrollIntoView({ behavior: "smooth", block: "start" });
        } else {
          sentinel.scrollIntoView({ behavior: "smooth", block: "end" });
        }
      } catch {
        (shouldShowCandidatesTop ? latestCandidates! : sentinel).scrollIntoView();
      }
    });
    return () => window.cancelAnimationFrame(id);
  }, [
    sourceType,
    chatState.messages.length,
    chatState.step,
    chatState.chosenRepo,
  ]);

  // Per-step LLM Q&A scaffolding (issue #18). The Source-step
  // analyzer can emit clarifying_questions when it can't
  // disambiguate something from the source alone (e.g. an
  // env-var-driven input that COULD be a node input file or an
  // internal config). The wizard renders the questions inline on
  // Source; the user picks an option (or free-texts an answer)
  // and the wizard re-fires analyze-script with `user_answers`
  // to propagate resolution into Configure-step pre-fill.
  type ClarifyingQuestion = {
    id: string;
    question: string;
    options: string[];
    source_location: string;
  };
  type GlueArtifact = {
    artifact_type: string;
    content: string;
    description: string;
  };
  const [clarifyingQuestions, setClarifyingQuestions] = useState<ClarifyingQuestion[]>([]);
  // α — hard schema gate. Stores the analyzer's per-call schema
  // confidence (specific to input_type + output_type). When "low"
  // AND the user hasn't manually overridden either field, the
  // Configure step blocks Next so a vaguely-typed node can never
  // ship. Default "high" is the no-information state — same as the
  // wizard's pre-α behaviour.
  const [schemaConfidence, setSchemaConfidence] = useState<"high" | "medium" | "low">("high");
  // Map from question id → user's chosen answer string (preset or
  // free-text). Submitted to analyze-script's `user_answers` field
  // when the user clicks "Continue" on the Q&A panel.
  const [questionAnswers, setQuestionAnswers] = useState<Record<string, string>>({});
  // Glue artifact proposed by the LLM (entrypoint.sh / wrapper
  // layer / shim / Containerfile wrapper, depending on source
  // type). Renders as a tab in the Preview-step artifact dropdown
  // row list. User can edit or remove before dispatch.
  const [glueArtifact, setGlueArtifact] = useState<GlueArtifact | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [generatedDockerfile, setGeneratedDockerfile] = useState("");
  const [generatedEntrypoint, setGeneratedEntrypoint] = useState("");
  const [generatedBuildPush, setGeneratedBuildPush] = useState("");
  const [generatedImageName, setGeneratedImageName] = useState("");

  // 4th Albedo artifact — the DAG METADATA BLOCK `.py` file. Server
  // renders this from the wizard's metadata at poll time (see #57) and
  // again at commit (see #59) using the same metadata_template module. The
  // text shown here is read-only by default: users edit the underlying
  // METADATA FIELDS (name, description, process_type, cost, timeout)
  // in the form above and the preview updates live. Direct-editing the
  // .py bytes is an escape hatch for power users; the server re-renders
  // from node_metadata regardless so arbitrary edits are ignored.
  const [generatedMetadataFile, setGeneratedMetadataFile] = useState("");
  const [generatedMetadataFilename, setGeneratedMetadataFilename] = useState("");

  // Per-GPU Dockerfile variants + the gpu_variants dict for the metadata.
  // Populated by the poll loop when the Containerizer emitted them.
  // Shape: {cuda: "FROM .../base-cuda\n...", rocm: "...", ...}
  const [generatedDockerfileVariants, setGeneratedDockerfileVariants] =
    useState<Record<string, string>>({});
  const [generatedGpuVariants, setGeneratedGpuVariants] =
    useState<Record<string, string>>({});
  // The hero's own variant label (cuda/rocm/intel/npu/cpu) — the
  // Containerizer infers this from the FROM line. Used to name the
  // first Preview tab properly ('CUDA' rather than a generic 'Hero').
  const [generatedHeroVariant, setGeneratedHeroVariant] = useState<string>("");

  // Phase 1-3 variant-aware wizard state. source_variants is the
  // per-variant per-file adapted source bodies the Containerizer
  // produced; variant_adaptations is the diagnostic metadata the
  // BuildVariantPreview renders (compat + changes + warnings +
  // incompatible reason). skippedVariants lets the operator exclude
  // specific variants before Build — the skipped set is sent with
  // the build request so those variants don't get pushed to Harbor.
  const [generatedSourceVariants, setGeneratedSourceVariants] =
    useState<Record<string, Record<string, string>>>({});
  const [generatedVariantAdaptations, setGeneratedVariantAdaptations] =
    useState<Record<string, any>>({});
  const [generatedSourceFiles, setGeneratedSourceFiles] =
    useState<Array<{ filename: string; body: string; language?: string; is_entrypoint?: boolean }>>([]);
  const [skippedVariants, setSkippedVariants] = useState<Set<string>>(new Set());

  // Operator variant-source overrides ("Rebuild {variant} with my changes").
  // serverSourceVariantsRef snapshots the server-generated per-variant source
  // as the dirty baseline: a variant is dirty when generatedSourceVariants[v]
  // differs from this snapshot, which enables its Rebuild button. rebuilding
  // tracks the in-flight POST per variant. previewHydratedRef guards the
  // Preview poll so operator edits aren't clobbered by a later poll tick.
  const serverSourceVariantsRef = useRef<Record<string, Record<string, string>>>({});
  const previewHydratedRef = useRef(false);
  const [rebuildingVariant, setRebuildingVariant] = useState<string | null>(null);

  // Preview-step accordion state — which artifact row is currently
  // expanded. Single-accordion: only one row open at a time. Default
  // is null (all collapsed) so the user sees the full metadata strip
  // before opening anything.
  const [previewExpandedKey, setPreviewExpandedKey] = useState<string | null>(null);

  // Multi-file input state (SourceFilesPane). Starts empty; the
  // existing Script tab stays the default single-paste UX. When the
  // user clicks "Multi-file" we populate this from scriptContent +
  // scriptFilename so switching doesn't lose what they already
  // pasted. On submit, if sourceFiles is non-empty we send it as
  // source_files[] in the /generate-artifacts request.
  const [useMultifileInput, setUseMultifileInput] = useState(
    (_persisted.useMultifileInput as boolean) || false,
  );
  const [sourceFiles, setSourceFiles] = useState<
    Array<{ id: string; filename: string; body: string; language?: string; is_entrypoint: boolean }>
  >(_persisted.sourceFiles || []);

  // Review-step variant picker state. predictedVariants is every
  // backend the platform supports right now — intersection of the
  // admin's Settings → GPU Compatibility whitelist AND currently-
  // active workers' hardware_tags. `predictedSuggested` is the
  // subset pre-checked based on which GPU framework the source
  // actually imports. `predictedUnmatched` is admin-enabled tags
  // that no active worker is covering yet (surfaced as a warning
  // so the admin knows to register a worker).
  const [predictedVariants, setPredictedVariants] = useState<string[]>([]);
  const [predictedSuggested, setPredictedSuggested] = useState<string[]>([]);
  const [predictedUnmatched, setPredictedUnmatched] = useState<string[]>([]);
  const [predictedHero, setPredictedHero] = useState<string>("");
  const [predictedIncompatibleReasons, setPredictedIncompatibleReasons] =
    useState<Record<string, string>>({});
  const [predictedCircuitBroken, setPredictedCircuitBroken] = useState<string[]>([]);
  const [predictedVariantsLoading, setPredictedVariantsLoading] = useState(false);
  const [reviewSkippedVariants, setReviewSkippedVariants] = useState<Set<string>>(new Set());

  // The server picks which generation path to use based on the input
  // shape (tiny single-file script → fast single-call albedo; larger
  // script or repo URL → multi-stage codebase pipeline). The wizard
  // sends the same request either way; no user-facing toggle.
  // See _enqueue_container_task in routers/node_definitions.py.
  const [generatedStages, setGeneratedStages] = useState<Array<{
    stage: string; status: string; detail?: string; ts?: number;
  }>>([]);

  const activeSteps = stepsForSource(sourceType);
  const stepIndex = activeSteps.indexOf(currentStep);

  // Live metadata preview — recomputed on every metadata edit using the same
  // shared template the server renders from on commit. Refreshes
  // synchronously as the user edits Configure fields so the Preview
  // step's 4th editor stays in sync without waiting for a poll round-
  // trip. Server re-renders from the submitted node_metadata at
  // /build-and-register-local, so this preview IS what ships.
  const livePreviewDag = useMemo(() => {
    try {
      // DAG metadata no longer carries process_type or gpu_variants —
      // those live on NodeDefinition. The scheduler derives the queue
      // at enqueue time (select_queue_for_node) from the variants dict
      // written by /build-and-register-local. The preview below is the
      // exact rendered .py the server will commit (byte-for-byte).
      const md = coerceMetadata({
        node_name: nodeName || "Untitled Node",
        executable_path: `${generatedImageName || (nodeName
          ? nodeName.replace(/[^a-zA-Z0-9]/g, "").toLowerCase()
          : "node")}:latest`,
        description,
        input_type: inputType,
        output_type: outputType,
        cost_in_credits: costInCredits,
        container_timeout: containerTimeout,
      });
      return renderMetadataTemplate(md);
    } catch {
      return null;
    }
  }, [nodeName, description, inputType, outputType,
      costInCredits, containerTimeout, generatedImageName]);

  // Fetch available secrets for credentials dropdown when wizard opens
  useEffect(() => {
    if (!open || !accessToken || availableSecrets.length > 0) return;
    const fetchSecrets = async () => {
      try {
        const API_URL = apiUrl();
        const resp = await authenticatedFetch(`${API_URL}/secrets/?limit=100`, {
          headers: { Accept: "application/json" },
        });
        if (resp.ok) {
          const secrets = await resp.json();
          setAvailableSecrets(secrets.map((s: any) => ({ id: s.id, name: s.name })));
        }
      } catch (e) {
        // Silently fail — credentials dropdown will just be empty
      }
    };
    fetchSecrets();
  }, [open, accessToken]);

  // Fetch curated base images (for the Script + Dockerfile tab dropdown).
  useEffect(() => {
    if (!open || !accessToken || baseImages.length > 0) return;
    const fetchBaseImages = async () => {
      try {
        const API_URL = apiUrl();
        const resp = await authenticatedFetch(`${API_URL}/nodes/base-images`, {
          headers: { Accept: "application/json" },
        });
        if (resp.ok) {
          const list = (await resp.json()) as CuratedBaseImage[];
          setBaseImages(list);
          if (!list.find((b) => b.variant_dir === selectedBaseImage)) {
            setSelectedBaseImage(list[0]?.variant_dir || "");
          }
        }
      } catch {
        // Silently fail — Script/Dockerfile tabs will keep their legacy
        // hardcoded defaults.
      }
    };
    fetchBaseImages();
  }, [open, accessToken]);

  // When the user picks a base image, auto-sync the dependent wizard
  // state (interpreter for Script, process_type hint for Configure).
  // Matches the plan's "show the system's suggestion, user can override":
  // we nudge the defaults, but don't lock anything.
  const onSelectBaseImage = useCallback((variantDir: string) => {
    setSelectedBaseImage(variantDir);
    const b = baseImages.find((x) => x.variant_dir === variantDir);
    if (!b) return;
    if (b.default_interpreter) {
      setScriptInterpreter(b.default_interpreter);
    }
    // Only nudge process_type if the user hasn't already picked a
    // non-default; respect user overrides.
    setProcessType((curr) => (curr === "CPU" ? b.process_type_hint : curr));
  }, [baseImages]);

  // Poll the wizard LLM status so the user knows whether the first
  // analysis will be fast or slow. If the model is cold, the status
  // banner explains the ~2-minute cold-load wait; if it's warm, the
  // chip stays green and the user can click Next confidently.
  //
  // Poll every 10s while the wizard is open AND not yet loaded. Once
  // loaded we stop polling (model is resident and keep_alive=-1
  // keeps it there; API-side keep-warm thread refreshes every 5min).
  useEffect(() => {
    if (!open || !accessToken) return;
    let cancelled = false;
    const API_URL = apiUrl();
    const poll = async () => {
      try {
        const resp = await authenticatedFetch(`${API_URL}/nodes/wizard-llm-status`, {
          headers: { Accept: "application/json" },
        });
        if (!resp.ok) return;
        const data = await resp.json();
        if (cancelled) return;
        setLlmStatus({
          loaded: !!data.loaded,
          model: data.model || "",
          detail: data.detail || "",
          size_bytes: data.size_bytes,
        });
      } catch {}
    };
    poll();
    const id = window.setInterval(() => {
      if (llmStatus?.loaded) return;  // stop polling once warm
      poll();
    }, 10000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [open, accessToken, llmStatus?.loaded]);

  const resetForm = useCallback(() => {
    setCurrentStep("Source");
    setSourceType("image");
    setDockerImage("");
    setDockerfileContent("");
    setScriptContent("");
    setScriptFilename("");
    setSampleInputFile(null);
    setSampleInputFilename("");
    setSampleInputBase64("");
    setDeferredRegistration(null);
    setSampleTestFailure(null);
    setDeferredBuildTaskId(null);
    setDeclineReason("");
    setShowDeclineForm(false);
    setRepoUrl("");
    setRepoCredential("");
    setRegistryCredential("");
    setNodeName("");
    setDescription("");
    setInputType("image");
    setOutputType("image");
    setProcessType("CPU");
    setCostTiers({ small: 1, medium: 1, large: 1 });
    setCostInCredits(1);
    setEffCostBasis(null);
    setSuggestedTiers(null);
    setBasisBeforeEdit(null);
    setContainerTimeout(7200);
    setEnvVars([]);
    setEgressHosts(new Set());
    setCustomEgressInput("");
    setUseSandboxRuntime(false);
    setIsSubmitting(false);
    setTaskId(null);
    setIsGenerating(false);
    setGenerationError(null);
    setGeneratedDockerfile("");
    setGeneratedEntrypoint("");
    setGeneratedBuildPush("");
    setGeneratedImageName("");
    setGeneratedMetadataFile("");
    setGeneratedMetadataFilename("");
    setPredictedAttempts(null);
    setPredictedMinutes(null);
    setPredictionBasis("");
    clearPersistedWizardState();
  }, []);

  // Persist working state on every change so a surprise remount doesn't
  // wipe the user's in-progress wizard. Only fields the user actually
  // edits are persisted — generated artifacts / transient flags are not
  // round-tripped so they regenerate fresh on next open.
  useEffect(() => {
    persistWizardState({
      currentStep,
      sourceType,
      dockerImage,
      dockerfileContent,
      scriptContent,
      scriptInterpreter,
      scriptFilename,
      sampleInputFilename,
      repoUrl,
      nodeName,
      description,
      inputType,
      outputType,
      processType,
      costInCredits,
      costTiers,
      // sz-tcp.7 — the manual-basis override + the suggested tier set the
      // "Reset" link restores, so a remount mid-wizard keeps both.
      costBasis: effCostBasis,
      suggestedTiers,
      basisBeforeEdit,
      containerTimeout,
      selectedBaseImage,
      // Phase 3 wiring — persist the multi-file input so a refresh
      // mid-wizard doesn't drop the extra files. Skip set isn't
      // persisted: fresh artifacts ALWAYS reset it anyway, and a
      // reload mid-Preview is a rare race we accept.
      useMultifileInput,
      sourceFiles,
    });
  }, [currentStep, sourceType, dockerImage, dockerfileContent, scriptContent,
      scriptInterpreter, scriptFilename, sampleInputFilename, repoUrl, nodeName, description,
      inputType, outputType, processType, costInCredits, costTiers, effCostBasis, suggestedTiers,
      basisBeforeEdit, containerTimeout,
      selectedBaseImage, useMultifileInput, sourceFiles]);

  // Plain close (escape / click-outside): just hide the dialog. Do NOT
  // wipe the form. The user can reopen and resume mid-wizard — a stray
  // click off the dialog should never cost them 5 minutes of typing.
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  // Hard close — wipe persistence now, close the dialog, and mark
  // that the NEXT open should start fresh. We deliberately do NOT
  // call resetForm() here because that fires 26 setState calls in
  // the same tick as onClose(), and React renders the wizard with
  // every field blanked for one frame BEFORE the close lands. The
  // user sees their Preview evaporate (the "blip"). By deferring
  // the field reset to the next-open effect below, the close is
  // visually clean — Preview stays filled until the dialog fades.
  const shouldResetOnNextOpenRef = useRef(false);
  const handleCloseAndReset = useCallback(() => {
    clearPersistedWizardState();
    shouldResetOnNextOpenRef.current = true;
    onClose();
  }, [onClose]);

  // On next open after a submit, reset the form fields. Gated by the
  // ref so plain opens (escape + reopen mid-edit) preserve state.
  useEffect(() => {
    if (open && shouldResetOnNextOpenRef.current) {
      shouldResetOnNextOpenRef.current = false;
      resetForm();
    }
  }, [open, resetForm]);

  // The legacy in-wizard resume path is gone. Build is now one
  // workflow_run with progress rendered in the build queue row, so
  // there's nothing for the wizard to hydrate when the user clicks
  // Resume from the notifications bell. The bell's resume button
  // routes the user to the queue row instead — see notifications-bell.

  // "Close & notify me when done" — user is on the Preview step mid-
  // generation. Stash the task_id in pending-generations so the
  // workflow-builder badge polls it to completion and fires a browser
  // Notification. Ask for Notification permission if we haven't yet.
  const handleCloseAndNotify = useCallback(async () => {
    if (!taskId) return;
    // Two completion paths surface to the user:
    //   (1) a Node Queue row (onDeferGeneration → addBuild with
    //       phase="generation") gives an inline progress row in the
    //       Builds dialog with a Resume CTA on completion;
    //   (2) a DB Notification row (written server-side by
    //       worker_tasks._notify_wizard_generation_complete on task
    //       complete/failed) surfaces in the Notifications bell for
    //       users who close the tab before the task finishes.
    // Both converge on the same Resume flow (setResumeTaskId +
    // setWizardOpen) so regardless of which one the user clicks,
    // they land back on Preview with artifacts hydrated.
    onDeferGeneration?.({ taskId, nodeName: nodeName || "Untitled Node" });
    // Treat this like a hard close: the in-flight task is now owned by
    // the Builds dialog / notification surface, so the wizard should
    // reset on the next "New node" click. Without this, persisted
    // state hydrates the dialog with the previous node's fields and
    // the user is stuck editing a stale draft.
    clearPersistedWizardState();
    shouldResetOnNextOpenRef.current = true;
    onClose();
  }, [taskId, nodeName, onClose, onDeferGeneration]);

  const canAdvance = (): boolean => {
    switch (currentStep) {
      case "Source":
        // Syntax errors block advancement — catch them here before
        // firing the (expensive) LLM analysis call on broken code.
        if (syntaxError) return false;
        if (sourceType === "image") {
          if (dockerImage.trim().length === 0) return false;
          // Block on a definitively-bad ref. exists=null is "we
          // couldn't verify" — allowed to proceed (private images,
          // unsupported registries, etc.). exists=false is the
          // known-bad case the preflight catches.
          if (imageRefCheck && imageRefCheck.exists === false) return false;
          return true;
        }
        if (sourceType === "dockerfile") return dockerfileContent.trim().length > 0;
        if (sourceType === "repo") {
          if (repoUrl.trim().length === 0) return false;
          // δ Phase 11 — block Next on a known-bad repo. ok=false
          // means the safety gate refused (license / archived /
          // keyword). Pending check (null) is allowed so users
          // aren't stuck on slow networks; the backend re-runs the
          // gate on build-and-register-local.
          if (repoSafety && repoSafety.ok === false) return false;
          return true;
        }
        if (sourceType === "script") {
          return scriptContent.trim().length > 0;
        }
        if (sourceType === "describe") {
          // Stay on the tab until the user has synthesised AND
          // attached a sample. Once synthesis fires we flip
          // sourceType="script", so this branch normally only
          // disables Next while the user is still composing. If the
          // synthesiser produced a Dockerfile but the flip did not land,
          // do not strand the user on a disabled Next.
          return generatedDockerfile.trim().length > 0;
        }
        return false;
      case "Configure": {
        if (nodeName.trim().length === 0) return false;
        // α — hard schema gate (lib/io-gate.ts). Image-mode submissions
        // also gate here because they skip the analyzer entirely;
        // their dropdowns default silently to image/image unless the
        // user explicitly confirms (issue raised in manual testing
        // 2026-05-21).
        if (needsIOConfirmation({
          sourceType,
          schemaConfidence,
          aiFilledFields,
        })) return false;
        return true;
      }
      case "Variables":
        return true; // optional
      case "Network":
        return true; // optional; default deny-all is always valid
      case "Review":
        // node_name is required by the API; the Configure step
        // normally enforces this but the Image-source "jump to
        // Review" path can land here with an empty name if the
        // auto-derive failed. Re-check here so the Create Node
        // button stays disabled until the user fills it in.
        if (nodeName.trim().length === 0) return false;
        // I/O gate also re-checked here — the "Analyze and jump to
        // Review" shortcut skips Configure, so the gate there
        // wouldn't fire. Build stays disabled until the user
        // confirms I/O types.
        if (needsIOConfirmation({
          sourceType,
          schemaConfidence,
          aiFilledFields,
        })) return false;
        return true;
      case "Preview":
        return (
          generatedDockerfile.trim().length > 0 &&
          generatedEntrypoint.trim().length > 0 &&
          generatedBuildPush.trim().length > 0
        );
    }
  };

  // Content of the user's submission on the Source step. Used as the
  // input to /analyze-script so it can pre-fill downstream steps.
  const getSourceContent = (): string => {
    if (sourceType === "script") return scriptContent;
    if (sourceType === "dockerfile") return dockerfileContent;
    if (sourceType === "repo") return repoUrl;
    if (sourceType === "image") return dockerImage;
    return "";
  };

  // Debounced syntax check. Fires ~500ms after the user pauses typing
  // so we don't hammer the API on every keystroke. Only the Script
  // and Dockerfile tabs do anything useful here; Repo + Image modes
  // short-circuit server-side.
  useEffect(() => {
    if (!accessToken) return;
    if (!open) return;
    if (sourceType !== "script" && sourceType !== "dockerfile") {
      setSyntaxError(null);
      return;
    }
    const content = sourceType === "script" ? scriptContent : dockerfileContent;
    if (!content.trim()) {
      setSyntaxError(null);
      return;
    }
    const handle = window.setTimeout(async () => {
      setSyntaxChecking(true);
      try {
        const API_URL = apiUrl();
        const resp = await authenticatedFetch(`${API_URL}/woodsman/check-syntax`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            script: content,
            interpreter: sourceType === "script" ? scriptInterpreter : "python3",
            source_type: sourceType,
          }),
        });
        if (!resp.ok) {
          setSyntaxError(null);
          return;
        }
        const data = await resp.json();
        setSyntaxError(data.ok ? null : { line: data.line, error: data.error });
      } catch {
        setSyntaxError(null);
      } finally {
        setSyntaxChecking(false);
      }
    }, 500);
    return () => window.clearTimeout(handle);
  }, [open, accessToken, sourceType, scriptContent, dockerfileContent]);

  // Review-step variant prediction. When the user lands on Review we
  // hit /predict-variants (no LLM, no credits) to surface which GPU
  // backends this source would produce images for. Runs once per
  // (Review entry + source change). Doesn't block navigation — if
  // the endpoint 500s we silently fall back to "all enabled" on the
  // server side.
  useEffect(() => {
    if (!open || !accessToken || currentStep !== "Review") return;
    if (sourceType !== "image" && sourceType !== "script" &&
        sourceType !== "dockerfile" && sourceType !== "repo" &&
        sourceType !== "describe") return;
    // Local API_URL shadow — matches the pattern used by every other
    // effect in this file (the component doesn't have a file-level
    // API_URL const; each network effect declares its own).
    const API_URL = apiUrl();
    setPredictedVariantsLoading(true);
    const body: any = {
      source_image: sourceType === "image"
        ? dockerImage
        : (baseImages.find((b) => b.variant_dir === selectedBaseImage)?.image_ref || ""),
      // describe mode's synthesized script lives in scriptContent (set by
      // the describe-synthesize handler), so feed it the same as script mode
      // — otherwise the variant sniffer sees an empty source and the Review
      // step shows a stale/wrong variant list (e.g. phantom CUDA on a
      // pure-CPU Pillow node).
      additional_instructions: (sourceType === "script" || sourceType === "describe")
        ? scriptContent
        : sourceType === "dockerfile" ? dockerfileContent : "",
    };
    if (sourceType === "repo") {
      // Server-side repo_fetcher will pull the same sentinel-file slice
      // /analyze-repo uses so the GPU-variant sniffer sees the actual
      // source code instead of an empty string.
      body.repo_url = repoUrl;
    }
    if (useMultifileInput && sourceFiles.length > 0) {
      body.source_files = sourceFiles.map((f) => ({
        filename: f.filename,
        body: f.body,
      }));
    }
    authenticatedFetch(`${API_URL}/woodsman/predict-variants`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then((r) => r.ok ? r.json() : { variants: [], suggested: [], skipped_by_admin: [], hero: "" })
      .then((data) => {
        const list: string[] = Array.isArray(data.variants) ? data.variants : [];
        const suggested: string[] = Array.isArray(data.suggested) ? data.suggested : list;
        const notMatched: string[] = Array.isArray(data.skipped_by_admin) ? data.skipped_by_admin : [];
        const hero: string = typeof data.hero === "string" ? data.hero : "";
        setPredictedVariants(list);
        setPredictedSuggested(suggested);
        setPredictedUnmatched(notMatched);
        setPredictedHero(hero);
        // P9f.1 — per-variant compatibility signal. Surfaces deep-CUDA
        // incompatibility (P8d), tripped circuit breaker (P9y), and any
        // future source-level reasons the endpoint adds.
        setPredictedIncompatibleReasons(
          (data && typeof data.incompatible_reasons === "object"
            && data.incompatible_reasons !== null)
            ? data.incompatible_reasons as Record<string, string>
            : {}
        );
        setPredictedCircuitBroken(
          Array.isArray(data?.circuit_broken_variants)
            ? data.circuit_broken_variants
            : []
        );
        // Reset skip toggles: start with everything NOT in `suggested`
        // pre-skipped, plus circuit-broken and source-incompatible
        // variants (force-skipped, checkbox disabled).
        const circuitBrokenArr: string[] = Array.isArray(data?.circuit_broken_variants)
          ? data.circuit_broken_variants
          : [];
        const incompatibleReasonsObj: Record<string, string> =
          (data && typeof data.incompatible_reasons === "object" && data.incompatible_reasons !== null)
            ? data.incompatible_reasons as Record<string, string>
            : {};
        setReviewSkippedVariants(
          computeReviewInitialSkipped(
            list, suggested, circuitBrokenArr, incompatibleReasonsObj,
            processType === "CPU",
          ),
        );
      })
      .catch(() => {
        setPredictedVariants([]);
        setPredictedSuggested([]);
        setPredictedUnmatched([]);
        setPredictedHero("");
        setPredictedIncompatibleReasons({});
        setPredictedCircuitBroken([]);
      })
      .finally(() => setPredictedVariantsLoading(false));
  }, [open, accessToken, currentStep, sourceType, dockerImage,
      scriptContent, dockerfileContent, selectedBaseImage, baseImages,
      useMultifileInput, sourceFiles, processType]);

  const runWorkerLlmPing = async () => {
    if (!accessToken || isPingingWorker) return;
    setIsPingingWorker(true);
    const API_URL = apiUrl();
    try {
      const dispatch = await authenticatedFetch(`${API_URL}/nodes/worker-llm-ping`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!dispatch.ok) {
        throw new Error(`Ping dispatch failed: HTTP ${dispatch.status}`);
      }
      const { task_id } = await dispatch.json();

      // Poll the existing generation-artifacts endpoint — it
      // recognises the llm_ping JSON shape and returns the diagnostic.
      // Bounded to ~30s; pings usually finish in 2-5s.
      for (let attempt = 0; attempt < 15; attempt++) {
        await new Promise((r) => setTimeout(r, 2000));
        const resp = await authenticatedFetch(
          `${API_URL}/woodsman/generation-artifacts/${task_id}`,
          {},
        );
        if (!resp.ok) continue;
        const data = await resp.json();
        if (data.status === "completed" && typeof data.llm_ping === "boolean") {
          if (data.llm_ping) {
            toast({
              title: "Worker reachability: OK",
              description: `Reached ${data.host || "LLM"} in ${data.latency_ms}ms (HTTP ${data.status_code}).`,
            });
          } else {
            toast({
              title: "Worker reachability: FAILED",
              description: `Can't reach ${data.endpoint || "LLM"} from a worker-spawned container. Error: ${data.error || "(unknown)"}. The PLATFORM_TASK network policy isn't routing the task correctly — check the worker binary version vs. the API.`,
              variant: "destructive",
            });
          }
          return;
        }
        if (data.status === "failed") {
          toast({
            title: "Worker reachability: worker task failed before pinging",
            description: data.error || "Unknown",
            variant: "destructive",
          });
          return;
        }
      }
      toast({
        title: "Worker reachability: timeout",
        description: "Ping task didn't finish in 30s. The worker may be stuck pulling the containerizer image.",
        variant: "destructive",
      });
    } catch (err: any) {
      toast({
        title: "Worker reachability: error",
        description: err.message || String(err),
        variant: "destructive",
      });
    } finally {
      setIsPingingWorker(false);
    }
  };

  const runAnalysis = async (options: { jumpToReview?: boolean } = {}) => {
    if (!accessToken) return;
    const content = getSourceContent();
    if (!content.trim()) return;

    // Repo mode: hit the dedicated /analyze-repo endpoint. The server
    // fetches a representative slice of the repo over raw HTTPS
    // (README + manifests + likely entrypoints), then runs the same
    // LLM analyzer that the script flow uses, so the response shape
    // matches and the rest of this function works unchanged.
    if (sourceType === "repo") {
      // Skip the dedupe shortcut when we're re-firing with user
      // answers — the content didn't change but the LLM has new
      // information to incorporate (issue #18).
      if (
        lastAnalyzedScriptRef.content === content &&
        Object.keys(questionAnswers).length === 0
      ) return;
      setIsAnalyzing(true);
      try {
        const API_URL = apiUrl();
        const resp = await authenticatedFetch(`${API_URL}/woodsman/analyze-repo`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo_url: repoUrl }),
        });
        if (!resp.ok) {
          console.warn("analyze-repo failed with", resp.status);
          return;
        }
        const analysis = await resp.json();
        lastAnalyzedScriptRef.content = content;
        setAnalysisConfidence(analysis.confidence || "low");
        // α — schema-specific confidence drives the Configure-step
        // hard gate. Falls back to overall confidence on older
        // analyzers (server-side does this too; defending here).
        setSchemaConfidence(
          (analysis.schema_confidence || analysis.confidence || "high") as
            "high" | "medium" | "low",
        );
        setAnalysisReasoning(analysis.reasoning || "");
        setPredictedAttempts(typeof analysis.predicted_attempts === "number" ? analysis.predicted_attempts : null);
        setPredictedMinutes(typeof analysis.predicted_minutes === "number" ? analysis.predicted_minutes : null);
        setPredictionBasis(typeof analysis.prediction_basis === "string" ? analysis.prediction_basis : "");
        const filled = new Set<string>();
        // See the script-mode comment block below — same overwrite rule
        // for fields the previous analysis filled.
        const wasAiFilledRepo = (key: string) => aiFilledFields.has(key);
        if (analysis.suggested_name && (wasAiFilledRepo("nodeName") || !nodeName.trim())) {
          setNodeName(analysis.suggested_name); filled.add("nodeName");
        }
        if (analysis.suggested_description && (wasAiFilledRepo("description") || !description.trim())) {
          setDescription(analysis.suggested_description); filled.add("description");
        }
        if (analysis.input_type) { setInputType(analysis.input_type); filled.add("inputType"); }
        if (analysis.output_type) { setOutputType(analysis.output_type); filled.add("outputType"); }
        if (analysis.process_type) { setProcessType(analysis.process_type); filled.add("processType"); }
        if (analysis.suggested_base_image) {
          setSelectedBaseImage(analysis.suggested_base_image);
          filled.add("baseImage");
          const b = baseImages.find((x) => x.variant_dir === analysis.suggested_base_image);
          if (b && b.default_interpreter) setScriptInterpreter(b.default_interpreter);
        }
        if (Array.isArray(analysis.env_vars) && analysis.env_vars.length > 0) {
          setEnvVars(analysis.env_vars.map((v: any) => ({
            name: String(v.name || ""),
            description: String(v.description || ""),
            default_value: "",
            required: Boolean(v.required),
          })));
          filled.add("envVars");
        }
        if (Array.isArray(analysis.allowed_egress) && analysis.allowed_egress.length > 0) {
          const hosts = new Set<string>(analysis.allowed_egress.map((e: any) => String(e.host)));
          setEgressHosts(hosts);
          filled.add("egress");
        }
        setAiFilledFields(filled);
        // Skip-to-review path. Same rationale as the script branch: a
        // parseable response of any confidence routes to Review;
        // network errors fall through to the catch and stay on Source.
        if (options.jumpToReview) {
          setSkipToReview(true);
          setCurrentStep("Review");
        }
      } catch (err) {
        console.error("analyze-repo error:", err);
      } finally {
        setIsAnalyzing(false);
      }
      return;
    }

    // Image source: nothing to feed an LLM analyzer. The user is
    // wrapping a pre-built image, not authoring source code. Calling
    // /analyze-script with the image tag as `script` makes the
    // server parse "python:3.11-slim" as Python and yell about
    // "illegal target for annotation" (issue #12). Short-circuit
    // here: mark analysis as "skipped" so the Configure banner
    // doesn't lie about prefill, and honour jumpToReview so the
    // user still gets the one-click Review experience.
    if (sourceType === "image") {
      if (!analysisConfidence) {
        setAnalysisConfidence("low");
        setAnalysisReasoning("Image source — no analysis needed");
      }
      // Image mode has no analyzer — the Input/Output dropdowns
      // default to image/image and stick silently unless we force
      // the user to confirm. Pre-mark both as aiFilledFields so the
      // existing Configure-step I/O gate (needsIOConfirmation) fires
      // and the Next button stays disabled until the user picks
      // (or rubber-stamps) both.
      setAiFilledFields((prev) => {
        const next = new Set(prev);
        next.add("inputType");
        next.add("outputType");
        return next;
      });
      // Auto-derive a Node Name from the image ref so the
      // jump-to-Review path doesn't strand the user with an empty
      // node_name (which the API 400s on). Title-cases the basename
      // sans tag: `runwayml/stable-diffusion-v1-5` → "Stable
      // Diffusion V1 5". User can still edit on the Review step.
      if (!nodeName.trim() && dockerImage.trim()) {
        const basename = dockerImage.split(":")[0].split("/").pop() || "";
        const titled = basename
          .replace(/[-_.]+/g, " ")
          .trim()
          .replace(/\b\w/g, (c) => c.toUpperCase());
        if (titled) setNodeName(titled);
      }
      if (options.jumpToReview) {
        setSkipToReview(true);
        setCurrentStep("Review");
      }
      return;
    }

    // Don't re-run analysis for the same content twice in one open-dialog.
    // BUT: if the caller wants to jumpToReview, jump regardless of
    // confidence — the old behaviour required `confidence === "high"`
    // and stranded the user on Source whenever the LLM was anything
    // less than fully sure (issue #17). Soft errors and warnings still
    // surface on Configure (and Review, after task 1.3); the wizard
    // doesn't get to decide for the user that they shouldn't proceed.
    //
    // Skip the dedupe when we're re-firing with user answers — the
    // content didn't change but the LLM has new information to
    // incorporate (issue #18). On a follow-up turn the LLM typically
    // converges to a final analysis with no remaining
    // clarifying_questions + a glue_artifact attached.
    if (
      lastAnalyzedScriptRef.content === content &&
      Object.keys(questionAnswers).length === 0
    ) {
      if (options.jumpToReview) {
        setSkipToReview(true);
        setCurrentStep("Review");
      }
      return;
    }

    setIsAnalyzing(true);
    try {
      const API_URL = apiUrl();
      const resp = await authenticatedFetch(`${API_URL}/woodsman/analyze-script`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          script: content,
          interpreter: sourceType === "script" ? "python3" : "python3",
          // sourceType is narrowed to "image" | "dockerfile" | "script" here
          // (the "repo" path returns at line 901 above). Dropping the dead
          // "repo" branch silences a TS comparison-overlap warning.
          source_type: sourceType === "dockerfile" ? "dockerfile" : "script",
          // Per-step Q&A propagation (issue #18). On follow-up
          // turns after the user answered the LLM's clarifying
          // questions, ship the answers so the LLM can incorporate
          // them. Empty/missing on the first turn — server treats
          // both shapes the same.
          ...(Object.keys(questionAnswers).length > 0
            ? {
                user_answers: Object.entries(questionAnswers).map(
                  ([id, answer]) => ({ id, answer }),
                ),
              }
            : {}),
        }),
      });
      if (!resp.ok) {
        // Fallback: don't block the wizard — let the user continue manually.
        console.warn("analyze-script failed with", resp.status);
        return;
      }
      const analysis = await resp.json();
      lastAnalyzedScriptRef.content = content;
      setAnalysisConfidence(analysis.confidence || "low");
      setSchemaConfidence(
        (analysis.schema_confidence || analysis.confidence || "high") as
          "high" | "medium" | "low",
      );
      setAnalysisReasoning(analysis.reasoning || "");
      setPredictedAttempts(typeof analysis.predicted_attempts === "number" ? analysis.predicted_attempts : null);
      setPredictedMinutes(typeof analysis.predicted_minutes === "number" ? analysis.predicted_minutes : null);
      setPredictionBasis(typeof analysis.prediction_basis === "string" ? analysis.prediction_basis : "");

      // Per-step Q&A scaffolding (issue #18). If the LLM emitted
      // clarifying questions, capture them so the Source-step UI
      // can render the panel; if it emitted a glue artifact,
      // capture it so the Preview step's artifact rows can show it
      // as another tab. Both default to empty / null when the LLM
      // has nothing to add — the existing single-pass flow is
      // unchanged in that case.
      if (Array.isArray(analysis.clarifying_questions) && analysis.clarifying_questions.length > 0) {
        setClarifyingQuestions(analysis.clarifying_questions);
      } else {
        // Convergence: the LLM had no more questions. Clear the
        // panel AND the per-turn answer state so a future fresh
        // analysis starts clean. Without this clear, the
        // questionAnswers map would carry over across analyses
        // and incorrectly defeat the dedupe shortcut on the next
        // unrelated source.
        setClarifyingQuestions([]);
        setQuestionAnswers({});
      }
      if (analysis.glue_artifact && typeof analysis.glue_artifact === "object") {
        setGlueArtifact(analysis.glue_artifact);
      } else {
        setGlueArtifact(null);
      }

      // Track which fields we auto-filled so the UI can badge them.
      const filled = new Set<string>();

      // Stale-state fix: a field that was AI-filled in a PREVIOUS
      // analysis pass should be overwriteable by the current pass even
      // if the value isn't empty. Otherwise, switching from one source
      // (TRELLIS) to another (BLIP) leaves the previous LLM-suggested
      // node name + description glued in place because the trim-empty
      // check sees them as "user input" — they aren't, they're prior
      // LLM output. We treat aiFilledFields as overwrite-allowed.
      const wasAiFilled = (key: string) => aiFilledFields.has(key);

      if (analysis.suggested_name && (wasAiFilled("nodeName") || !nodeName.trim())) {
        setNodeName(analysis.suggested_name);
        filled.add("nodeName");
      }
      if (analysis.suggested_description && (wasAiFilled("description") || !description.trim())) {
        setDescription(analysis.suggested_description);
        filled.add("description");
      }
      if (analysis.input_type) {
        setInputType(analysis.input_type);
        filled.add("inputType");
      }
      if (analysis.output_type) {
        setOutputType(analysis.output_type);
        filled.add("outputType");
      }
      if (analysis.process_type) {
        setProcessType(analysis.process_type);
        filled.add("processType");
      }
      // Base image — the LLM picked from the curated catalog. Pre-select
      // it on the Configure step so the user sees a sensible default
      // without having to choose up front. They can still override via
      // the dropdown.
      if (analysis.suggested_base_image) {
        setSelectedBaseImage(analysis.suggested_base_image);
        filled.add("baseImage");
        // Sync dependent state (interpreter, process_type hint) without
        // overriding user-set values — reuses the dropdown handler.
        const b = baseImages.find((x) => x.variant_dir === analysis.suggested_base_image);
        if (b && b.default_interpreter) {
          setScriptInterpreter(b.default_interpreter);
        }
      }
      if (Array.isArray(analysis.env_vars) && analysis.env_vars.length > 0) {
        setEnvVars(analysis.env_vars.map((v: any) => ({
          name: String(v.name || ""),
          description: String(v.description || ""),
          default_value: "",
          required: Boolean(v.required),
        })));
        filled.add("envVars");
      }
      if (Array.isArray(analysis.allowed_egress) && analysis.allowed_egress.length > 0) {
        const hosts = new Set<string>(analysis.allowed_egress.map((e: any) => String(e.host)));
        setEgressHosts(hosts);
        filled.add("egress");
      }
      setAiFilledFields(filled);

      // Skip-to-review path. We got a parseable analysis response, so
      // jump regardless of confidence (issue #17). Low/medium confidence
      // is rendered as a warning banner on the destination step — the
      // wizard advances rather than stalling silently on Source.
      if (options.jumpToReview) {
        setSkipToReview(true);
        setCurrentStep("Review");
        return;
      }
    } catch (err) {
      console.error("analyze-script error:", err);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleNext = async () => {
    // Trigger LLM analysis on Source → Next so downstream steps render
    // with pre-filled values. Non-blocking if the LLM is unavailable;
    // we still advance after a short wait.
    const wasOnSource = currentStep === "Source";
    if (wasOnSource) {
      await runAnalysis();
    }
    if (stepIndex < activeSteps.length - 1) {
      setCurrentStep(activeSteps[stepIndex + 1]);
    }
    // Auto-build mode (issue #3). After the Source → Configure
    // transition, ride the rest of the way to Preview so the user
    // lands directly at the artifact-review step. Skipped when:
    //   • auto-mode is off,
    //   • we weren't coming from Source (manual stepping wins),
    //   • the analyzer reported a fatal soft error (the wizard
    //     stays on Source via the existing path),
    //   • Image mode — there's no analyzer, the I/O dropdowns
    //     default to image/image silently. We MUST stop on
    //     Configure so the user explicitly confirms I/O types
    //     (otherwise the wizard rides past the gate banner and
    //     the user only sees it on Review, defeating the point of
    //     auto-build).
    if (
      wasOnSource && autoMode &&
      sourceType !== "image" &&
      // Pause auto-advance whenever the LLM has questions
      // outstanding for the user (issue #18). The user must answer
      // (or click Skip) before the wizard rides through Configure.
      clarifyingQuestions.length === 0 &&
      !analysisReasoning.startsWith("Analysis unavailable") &&
      !analysisReasoning.startsWith("Syntax error")
    ) {
      runAutoAdvance(stepIndex + 1);
    }
  };

  // Walks setCurrentStep through every remaining step until Preview
  // (or until the user cancels). Each step renders briefly so its
  // mount effects fire and the user can see the pre-filled values
  // streaming through. Cancellation is monitored via the
  // autoAdvanceCancelledRef so a stale closure isn't a problem.
  const runAutoAdvance = useCallback(
    async (fromIndex: number) => {
      autoAdvanceCancelledRef.current = false;
      setAutoAdvancing(true);
      // Auto-advance stops at Review, not Preview (Tim's #20
      // clarification). The user clicks Next on Review to kick off
      // the build — that's when the toast fires + the wizard hides.
      const reviewIdx = activeSteps.indexOf("Review" as Step);
      const stopAt = reviewIdx >= 0 ? reviewIdx : activeSteps.length - 1;
      try {
        for (let i = fromIndex + 1; i <= stopAt; i++) {
          if (autoAdvanceCancelledRef.current) return;
          // Brief pause so React commits the previous setCurrentStep
          // and the user can glimpse the pre-filled step. 120ms is
          // long enough to be perceptible without feeling sluggish.
          await new Promise((r) => setTimeout(r, 120));
          if (autoAdvanceCancelledRef.current) return;
          setCurrentStep(activeSteps[i]);
        }
        // No autoBuildPending here — user kicks off the build by
        // clicking Next on Review.
      } finally {
        setAutoAdvancing(false);
      }
    },
    [activeSteps],
  );

  const cancelAutoAdvance = useCallback(() => {
    autoAdvanceCancelledRef.current = true;
    setAutoAdvancing(false);
  }, []);

  const handleAnalyzeAndJumpToReview = async () => {
    await runAnalysis({ jumpToReview: true });
    // If confidence wasn't high, runAnalysis falls through and the
    // wizard stays on Source. User then hits the normal Next flow.
  };

  const handleBack = () => {
    if (stepIndex > 0) {
      setCurrentStep(activeSteps[stepIndex - 1]);
    }
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (ev) => {
      const content = ev.target?.result as string;
      if (sourceType === "dockerfile") {
        setDockerfileContent(content);
      } else {
        setScriptContent(content);
        setScriptFilename(file.name);
      }
    };
    reader.readAsText(file);
  };

  // Script-tab uploader. Accepts file picks AND folder picks (the
  // folder input has webkitdirectory set). Detects the script
  // language from the first file's name so the interpreter dropdown
  // doesn't have to be set manually for the common case
  // (`run.py` → python3, `requirements.txt` → text). Bumps the user
  // to the Dockerfile tab if they accidentally drop a Dockerfile in
  // here. Concatenates multiple files into one paste with `# === N ===`
  // headers — matches the existing single-paste analyzer contract.
  // Core: take a flat File[] (with webkitRelativePath optionally set
  // by folder picks or recursive drag-drop traversal). Routes to one
  // of two paths:
  //   • Single flat file → keep the simple paste-into-body behavior.
  //   • Multiple files OR any file with directory parts in its path →
  //     flip the wizard into multi-file (SourceFilesPane) mode and
  //     load every file as its own WizardSourceFile, preserving the
  //     full relative path. This is Tim's #14 round-4 ask: dropping
  //     a folder structure brings in every file with its layout
  //     intact, not flattened into a single concat.
  const processScriptFiles = useCallback(
    (files: File[]) => {
      if (!files.length) return;
      const firstName = (files[0] as File & { webkitRelativePath?: string })
        .webkitRelativePath || files[0].name;
      const inferred = inferScriptInterpreterFromFilename(firstName);
      if (inferred === "DOCKERFILE_REDIRECT") {
        toast({
          title: "Use the Dockerfile tab",
          description: `${files[0].name} looks like a Dockerfile — switch to the Dockerfile tab to upload it there.`,
        });
        return;
      }
      if (inferred) setScriptInterpreter(inferred);

      const hasDirs = files.some(
        (f) => ((f as File & { webkitRelativePath?: string }).webkitRelativePath || "").includes("/")
      );
      const multiFile = files.length > 1 || hasDirs;

      // ─── Multi-file path ──────────────────────────────────────────
      if (multiFile) {
        // Read every file's text in parallel, then build one
        // WizardSourceFile per file. Pick the entrypoint with a
        // priority list (main.* > index.* > first script-shaped file).
        Promise.all(
          files.map((file) => {
            const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
            return new Promise<{ filename: string; body: string }>((resolve) => {
              const reader = new FileReader();
              reader.onload = (ev) => resolve({ filename: path, body: (ev.target?.result as string) || "" });
              reader.onerror = () => resolve({ filename: path, body: "" });
              reader.readAsText(file);
            });
          })
        ).then((items) => {
          // Heuristic entrypoint: the first match in priority order.
          const entryRe = [
            /^(?:.*\/)?main\.(py|sh|js|ts|rb|go|rs|java|c|cpp)$/i,
            /^(?:.*\/)?index\.(py|sh|js|ts)$/i,
            /^(?:.*\/)?app\.(py|sh|js|ts)$/i,
            /\.(py|sh|js|ts)$/i,
          ];
          let entryIdx = -1;
          for (const re of entryRe) {
            entryIdx = items.findIndex((it) => re.test(it.filename));
            if (entryIdx >= 0) break;
          }
          if (entryIdx < 0) entryIdx = 0;

          let seq = Date.now();
          const wizardFiles = items.map((it, i) => ({
            id: `f${seq++}_${i}`,
            filename: it.filename,
            body: it.body,
            language: "",
            is_entrypoint: i === entryIdx,
          }));
          setUseMultifileInput(true);
          setSourceFiles(wizardFiles);
          // Keep the legacy single-paste field in sync with the
          // entrypoint so analysis/syntax-check codepaths that still
          // read scriptContent see something coherent.
          setScriptContent(wizardFiles[entryIdx]?.body || "");
          setScriptFilename(items.map((it) => it.filename).join(", "));
        });
        return;
      }

      // ─── Single-file path (unchanged behavior) ────────────────────
      const names: string[] = [];
      let combined = "";
      let loaded = 0;
      files.forEach((file) => {
        const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
        names.push(path);
        const reader = new FileReader();
        reader.onload = (ev) => {
          combined += `\n# === ${path} ===\n${ev.target?.result as string}\n`;
          loaded++;
          if (loaded === files.length) {
            setScriptContent(combined);
            setScriptFilename(names.join(", "));
          }
        };
        reader.readAsText(file);
      });
    },
    [setScriptContent, setScriptFilename, setScriptInterpreter, setUseMultifileInput, setSourceFiles],
  );

  const handleScriptFileUpload = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files || []);
      processScriptFiles(files);
      // Clear the input so the user can re-pick the same selection.
      e.target.value = "";
    },
    [processScriptFiles],
  );

  // Walk a FileSystemEntry tree (drag-drop folders) into a flat File[]
  // with each File's webkitRelativePath set so the existing
  // path-aware code paths (interpreter inference, # === filename ===
  // headers) keep working unchanged.
  const collectDroppedFiles = useCallback(
    async (items: DataTransferItemList): Promise<File[]> => {
      const out: File[] = [];
      const walk = async (entry: any, basePath: string): Promise<void> => {
        if (!entry) return;
        if (entry.isFile) {
          const f: File = await new Promise((res) => entry.file(res));
          // Preserve the relative path so processScriptFiles renders
          // `# === folder/sub/file.py ===` headers correctly.
          Object.defineProperty(f, "webkitRelativePath", {
            value: basePath ? `${basePath}/${entry.name}` : entry.name,
            configurable: true,
          });
          out.push(f);
          return;
        }
        if (entry.isDirectory) {
          const reader = entry.createReader();
          // readEntries pages — loop until empty.
          const readAll = (): Promise<any[]> =>
            new Promise((res) => {
              const collected: any[] = [];
              const next = () => reader.readEntries((batch: any[]) => {
                if (batch.length === 0) { res(collected); return; }
                collected.push(...batch);
                next();
              });
              next();
            });
          const entries = await readAll();
          const sub = basePath ? `${basePath}/${entry.name}` : entry.name;
          for (const e of entries) await walk(e, sub);
        }
      };
      const work: Promise<void>[] = [];
      // DataTransferItemList is array-like but lacks Array methods.
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const entry = (item as any).webkitGetAsEntry?.();
        if (entry) work.push(walk(entry, ""));
      }
      await Promise.all(work);
      return out;
    },
    [],
  );

  const handleScriptFileDrop = useCallback(
    async (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setScriptDropActive(false);
      const items = e.dataTransfer?.items;
      if (items && items.length > 0 && items[0].kind === "file") {
        const files = await collectDroppedFiles(items);
        processScriptFiles(files);
      } else if (e.dataTransfer?.files?.length) {
        // Fallback for drops without entry support — flat file list.
        processScriptFiles(Array.from(e.dataTransfer.files));
      }
    },
    [collectDroppedFiles, processScriptFiles],
  );

  const addEnvVar = () => {
    setEnvVars([...envVars, { name: "", description: "", default_value: "", required: false }]);
  };

  const updateEnvVar = (index: number, field: keyof EnvVarDef, value: string | boolean) => {
    const updated = [...envVars];
    if (field === "name" && typeof value === "string") {
      value = value.toUpperCase().replace(/[^A-Z0-9_]/g, "");
    }
    updated[index] = { ...updated[index], [field]: value } as EnvVarDef;
    setEnvVars(updated);
  };

  const removeEnvVar = (index: number) => {
    setEnvVars(envVars.filter((_, i) => i !== index));
  };

  // pollGenerationArtifacts + dispatchPhase2Build deleted in §6 of
  // wizard-as-workflow-native — see commit log. The wizard no longer
  // runs a client-side state machine between phase-1 LLM generation
  // and phase-2 build; one POST to /build-and-register-node submits
  // a workflow_run that auto-chains generate → ingest → variants on
  // the server side. Progress is rendered by the build queue row,
  // not the wizard.

  // γ — poll /build-task-status while a deferred build is in flight.
  // Stops as soon as the task lands deferred_registration (success
  // path → render thumbs UI) or sample_test_failure (fail path → ask
  // user to iterate). Cleared by the finalize-registration handlers
  // below or by closing/resetting the wizard.
  useEffect(() => {
    if (!deferredBuildTaskId) return;
    if (!accessToken) return;
    if (deferredRegistration) return;  // already loaded; user is reviewing
    if (sampleTestFailure) return;     // already failed; surface failure
    let cancelled = false;
    const apiUrlBase = apiUrl();
    const tick = async () => {
      try {
        const resp = await authenticatedFetch(
          `${apiUrlBase}/woodsman/build-task-status/${deferredBuildTaskId}`,
          {},
        );
        if (!resp.ok) return;
        const data = await resp.json();
        if (cancelled) return;
        if (data.deferred_registration) {
          setDeferredRegistration(data.deferred_registration);
        } else if (data.sample_test_failure) {
          setSampleTestFailure(data.sample_test_failure);
        } else if (data.status === "failed") {
          setSampleTestFailure({
            sample_test_result: null,
            error: data.error || "Build failed before sample test.",
          });
        }
      } catch {
        // transient network error — keep polling
      }
    };
    tick();
    const id = window.setInterval(tick, 3000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [deferredBuildTaskId, accessToken, deferredRegistration, sampleTestFailure]);

  // γ — sample-input file picker handler. Reads the file, validates
  // size (≤8 MB matches the API gate), strips the data-URL prefix
  // and stores both the File (for display) and the base64 (for
  // submit).
  const handleSampleFileSelect = useCallback((file: File | null) => {
    if (!file) {
      setSampleInputFile(null);
      setSampleInputFilename("");
      setSampleInputBase64("");
      return;
    }
    const sizeError = validateSampleSize(file);
    if (sizeError) {
      toast({
        title: "Sample too large",
        description: sizeError,
        variant: "destructive",
      });
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = stripDataUrlPrefix(reader.result as string);
      setSampleInputFile(file);
      setSampleInputFilename(file.name);
      setSampleInputBase64(b64);
    };
    reader.onerror = () => {
      toast({
        title: "Sample read failed",
        description: "Could not read the file. Try a different one.",
        variant: "destructive",
      });
    };
    reader.readAsDataURL(file);
  }, [toast]);

  // γ — finalize-registration handlers. Approved → POST with the
  // current build-task id; the API materialises the NodeDefinition
  // row and we close the wizard. Declined → POST with approved=false
  // and an optional reason; the wizard returns to Configure so the
  // user can adjust. The image stays in Harbor either way.
  const handleApproveDeferred = useCallback(async () => {
    if (!accessToken || !deferredBuildTaskId) return;
    setFinalizing(true);
    const apiUrlBase = apiUrl();
    try {
      const resp = await authenticatedFetch(`${apiUrlBase}/woodsman/finalize-registration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          build_task_id: deferredBuildTaskId,
          approved: true,
        }),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        throw new Error(txt || "Finalize failed");
      }
      const data = await resp.json();
      toast({
        title: "Node registered",
        description: `${data.node_name} is now available.`,
      });
      onNodeCreated?.({
        taskId: deferredBuildTaskId,
        nodeName: data.node_name || nodeName,
      });
      handleCloseAndReset();
    } catch (err: any) {
      toast({
        title: "Registration failed",
        description: err?.message || "Try again or check the build logs.",
        variant: "destructive",
      });
    } finally {
      setFinalizing(false);
    }
  }, [accessToken, deferredBuildTaskId, nodeName, onNodeCreated, toast]);

  const handleDeclineDeferred = useCallback(async () => {
    if (!accessToken || !deferredBuildTaskId) return;
    setFinalizing(true);
    const apiUrlBase = apiUrl();
    try {
      const resp = await authenticatedFetch(`${apiUrlBase}/woodsman/finalize-registration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          build_task_id: deferredBuildTaskId,
          approved: false,
          decline_reason: declineReason || undefined,
        }),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        throw new Error(txt || "Decline failed");
      }
      toast({
        title: "Build retained, not registered",
        description: "Image is in the registry; iterate on the wizard or close to abandon.",
      });
      // Keep the wizard open on Configure so the user can adjust
      // input/output schema and re-build. Clear deferred state so
      // the next submit goes through clean.
      setDeferredRegistration(null);
      setSampleTestFailure(null);
      setDeferredBuildTaskId(null);
      setShowDeclineForm(false);
      setDeclineReason("");
      setCurrentStep("Configure");
    } catch (err: any) {
      toast({
        title: "Decline failed",
        description: err?.message || "Try again.",
        variant: "destructive",
      });
    } finally {
      setFinalizing(false);
    }
  }, [accessToken, deferredBuildTaskId, declineReason, toast]);

  const handleSubmit = async () => {
    if (!accessToken) return;
    if (currentStep !== "Review") return;

    // GA.LSW.2 — pre-flight: refuse to enqueue a build while the AI
    // backend is down (status never loaded → treat as unavailable). A cold
    // (not-yet-loaded) model still builds; it only shows the load ETA.
    // Image mode registers an existing image — no LLM step (server:
    // node_builder/io_adapter/image_mode.py:145 "No LLM call").
    const preflight = buildPreflight(llmStatus, { needsLlm: sourceType !== "image" });
    if (!preflight.ok) {
      setGenerationError(preflight.message);
      toast({
        title: "AI backend unavailable",
        description: preflight.message,
        variant: "destructive",
      });
      return;
    }
    if (preflight.notice) {
      toast({ title: "AI model warming up", description: preflight.notice });
    }

    const API_URL = apiUrl();

    // Curated-base ref used for script / dockerfile / image source
    // types. The image source type uses the user-typed dockerImage
    // directly; repo source type has its own URL field.
    const selectedCuratedBase = baseImages.find((b) => b.variant_dir === selectedBaseImage)
      || baseImages[0];
    const curatedBaseRef = selectedCuratedBase?.image_ref
      || "ghcr.io/modtim/base-python:3.11";

    // Wizard inputs → /build-and-register-node body. Server creates a
    // BuildSpec with empty Dockerfile/scripts, then submits a single
    // workflow_run whose first nodes are the Containerizer generate +
    // ingest-artifacts pair. Variants build + push + register chain
    // on after that — same workflow_run, one progress URL.
    const body: any = {
      node_name: nodeName,
      input_type: inputType,
      output_type: outputType,
      process_type: processType,
      // Backward compatible: `cost_in_credits` still ships (= medium tier)
      // for servers that don't know about tiers yet; `cost_tiers` carries
      // the full Small/Medium/Large card the wizard now collects.
      cost_in_credits: costInCredits,
      cost_tiers: costTiers,
      // sz-tcp.7 — 'manual' once the author touched a tier input (the server
      // stamps node_definitions.cost_basis verbatim); null → leave the
      // column at its 'estimated' default.
      cost_basis: effCostBasis ?? undefined,
      description: description || undefined,
      // Smart-category override (manual-test 2026-05-22). Empty
      // string is treated as "use server fallback" so we don't
      // accidentally override the LLM suggester when the Review
      // fetch hasn't completed yet.
      node_type: nodeType.trim() || undefined,
    };
    switch (sourceType) {
      case "repo":
        body.source_repo_url = repoUrl;
        if (repoCredential && repoCredential !== "none") {
          body.additional_instructions = `Git credential secret: ${repoCredential}`;
        }
        break;
      case "image":
        body.wrap_base_image = dockerImage;
        // Do NOT derive image_name from the base image ref. Doing so
        // makes every Image-mode submission that wraps the same base
        // produce the same harbor tag — two distinct Nodes (e.g.
        // "Python" and "Billy" both wrapping python:3.11-slim) would
        // share `tenant-{id}/python:latest`, and deleting one Node's
        // image breaks the other (manual-test-driven fix 2026-05-22).
        // Server slugs image_name from node_name instead, giving
        // each Node its own harbor image.
        break;
      case "script":
        body.wrap_base_image = curatedBaseRef;
        body.inline_source_body = scriptContent;
        if (scriptFilename) body.inline_source_filename = scriptFilename;
        break;
      case "dockerfile":
        body.wrap_base_image = curatedBaseRef;
        body.inline_source_body = dockerfileContent;
        body.inline_source_filename = "Dockerfile";
        break;
      default:
        toast({
          title: "Unsupported source type",
          description: `sourceType='${sourceType}' has no Submit path.`,
          variant: "destructive",
        });
        return;
    }

    if (envVars.filter((v) => v.name.trim()).length > 0) {
      body.container_env = Object.fromEntries(
        envVars.filter((v) => v.name.trim()).map((v) => [v.name, v.default_value])
      );
    }
    // Phase B (#1) — synth-declared system binaries. Only the Script
    // source-type carries these (synth only fires from the Describe
    // chat); other source-types leave them empty server-side.
    if (synthSystemPackages.length > 0) {
      body.system_packages = synthSystemPackages;
    }
    if (synthDockerfileExtras && synthDockerfileExtras.trim()) {
      body.dockerfile_extras = synthDockerfileExtras;
    }
    if (egressHosts.size > 0) {
      body.allowed_egress = Array.from(egressHosts).map((h) => ({
        host: h, ports: [443], protocol: "tcp",
      }));
    }
    if (useSandboxRuntime) body.use_sandbox_runtime = true;
    if (reviewSkippedVariants.size > 0) {
      body.skipped_variants = Array.from(reviewSkippedVariants);
    }
    if (useMultifileInput && sourceFiles.length > 0) {
      body.source_files = sourceFiles.map((f) => ({
        filename: f.filename, body: f.body,
        language: f.language || undefined,
        is_entrypoint: f.is_entrypoint,
      }));
    }
    // Deferred-registration when the user attached a sample for review.
    Object.assign(
      body,
      deferredRegistrationFields(
        sampleInputBase64 && sampleInputFilename
          ? { filename: sampleInputFilename, base64: sampleInputBase64 }
          : null,
      ),
    );

    setIsSubmitting(true);
    setGenerationError(null);
    try {
      const resp = await authenticatedFetch(`${API_URL}/woodsman/build-and-register-node`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const raw = await resp.text();
        let friendly = raw;
        try {
          const parsed = JSON.parse(raw);
          const detail = parsed?.detail;
          if (detail && typeof detail === "object" && detail.reason) {
            friendly = `${detail.admission_rule || "build-rejected"}\n\n${detail.reason}`;
          } else if (detail && typeof detail === "object" && detail.error) {
            // 409 duplicate-name + other shape-shaped errors use
            // detail.error as the human-readable message.
            friendly = detail.error;
          } else if (detail && typeof detail === "object" && (detail.code === "build_farm_unavailable" || detail.code === "wizard_llm_not_configured") && typeof detail.message === "string") {
            // 409 build_farm_unavailable (node-builder image not configured on
            // this install) / wizard_llm_not_configured (no AI model set for the
            // wizard): the API's message already says what to do next.
            friendly = detail.message;
          } else if (typeof detail === "string") {
            friendly = detail;
          }
        } catch { /* not JSON */ }
        throw new Error(friendly);
      }
      const result = await resp.json();
      const workflowRunId = result.workflow_run_id ?? result.task_id;
      setTaskId(workflowRunId);

      // Sample-deferred path stays in the wizard to show sample-output
      // review when the build_task reaches deferred_registration.
      if (sampleInputBase64 && sampleInputFilename) {
        setDeferredBuildTaskId(workflowRunId);
        setCurrentStep("Preview");
        toast({
          title: "Build started",
          description: "We'll run your sample through the image and ask you to review the output before registering.",
        });
        return;
      }

      onNodeCreated?.({ taskId: workflowRunId, nodeName });
      toast({
        title: "Build started",
        description: "Watch the build queue — generate → variants → register.",
      });
      handleCloseAndReset();
    } catch (err: any) {
      const msg = err.message || "An error occurred";
      setGenerationError(msg);
      toast({
        title: "Build failed to start",
        description: msg.split("\n\n")[0] || msg,
        variant: "destructive",
      });
    } finally {
      setIsSubmitting(false);
    }
  };

  // Preview-step hydration. Once the wizard lands on Preview with a live
  // workflow_run (taskId), poll /build-run-progress to pull the server-
  // generated artifacts — the Dockerfile(s), per-variant adapted source, and
  // adaptation diagnostics — into the Preview state so the per-variant tabs
  // render real content the operator can edit. serverSourceVariantsRef keeps
  // the pristine baseline so we can tell which variants the operator actually
  // changed (dirty = Rebuild enabled). We hydrate ONCE (previewHydratedRef)
  // so a later poll can't stomp an in-progress edit.
  useEffect(() => {
    if (currentStep !== "Preview" || !taskId || previewHydratedRef.current) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await authenticatedFetch(
          `${API_URL}/woodsman/build-run-progress/${taskId}`, {},
        );
        if (!resp.ok || cancelled) return;
        const data = await resp.json();
        const art = data.artifacts;
        if (!art || cancelled) return;
        previewHydratedRef.current = true;
        if (typeof art.dockerfile === "string" && art.dockerfile) setGeneratedDockerfile(art.dockerfile);
        if (art.dockerfile_variants) setGeneratedDockerfileVariants(art.dockerfile_variants);
        if (art.variant_adaptations) setGeneratedVariantAdaptations(art.variant_adaptations);
        if (art.gpu_variants) setGeneratedGpuVariants(art.gpu_variants);
        if (art.hero_variant) setGeneratedHeroVariant(art.hero_variant);
        if (Array.isArray(art.source_files)) {
          setGeneratedSourceFiles(art.source_files.map((f: any) => ({
            filename: f.filename, body: f.body ?? f.content ?? "",
            language: f.language, is_entrypoint: f.is_entrypoint,
          })));
        }
        // Server per-variant source: {variant: {filename: body}}. Seed the
        // editable state AND snapshot it as the dirty baseline.
        const sv = (art.source_variants && typeof art.source_variants === "object")
          ? art.source_variants : {};
        setGeneratedSourceVariants(sv);
        serverSourceVariantsRef.current = JSON.parse(JSON.stringify(sv));
      } catch {
        // Best-effort hydration — the operator can still watch the build
        // queue; a failed poll just means no inline editing this session.
      }
    })();
    return () => { cancelled = true; };
  }, [currentStep, taskId, authenticatedFetch]);

  // "Rebuild {variant} with my changes." POSTs the operator's edited source
  // for one variant to the rebuild endpoint, which records it as an explicit
  // override and re-dispatches that variant's build → push → sample-test.
  const rebuildVariantWithEdits = async (variantKey: string) => {
    if (!taskId || rebuildingVariant) return;
    const edited = generatedSourceVariants[variantKey] || {};
    const files = Object.entries(edited).map(([filename, content]) => ({
      filename, content: content as string,
    }));
    if (files.length === 0) return;
    setRebuildingVariant(variantKey);
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(
        `${API_URL}/woodsman/wizard/rebuild-variant`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workflow_run_id: taskId, variant: variantKey, files }),
        });
      if (!resp.ok) {
        const raw = await resp.text();
        let msg = raw;
        let objMsg = "";
        try {
          const d = JSON.parse(raw)?.detail;
          msg = d || raw;
          if (d && typeof d === "object" && typeof d.message === "string") objMsg = d.message;
        } catch { /* keep raw */ }
        throw new Error(typeof msg === "string" ? msg : (objMsg || "Rebuild failed"));
      }
      // Accept the edit as the new baseline so the button disarms until the
      // operator changes it again.
      serverSourceVariantsRef.current = {
        ...serverSourceVariantsRef.current,
        [variantKey]: JSON.parse(JSON.stringify(edited)),
      };
      toast({
        title: `Rebuilding ${variantKey.toUpperCase()}`,
        description: "Your edited source is building. Watch the build queue for the new variant image.",
      });
    } catch (err: any) {
      toast({
        title: `Rebuild failed`,
        description: err.message || String(err),
        variant: "destructive",
      });
    } finally {
      setRebuildingVariant(null);
    }
  };

  // A variant is dirty when its editable source differs from the server
  // baseline snapshot captured at hydration.
  const isVariantDirty = (variantKey: string): boolean => {
    const cur = generatedSourceVariants[variantKey];
    if (!cur) return false;
    const base = serverSourceVariantsRef.current[variantKey] || {};
    return JSON.stringify(cur) !== JSON.stringify(base);
  };

  // ===========================================================================
  // node-code-edit-rebuild — "edit existing node code" mode.
  //
  // Entirely additive: only reachable via editNodeId being set (NodeLibrary's
  // "Edit code" action). No admin flag gate — permission is enforced
  // server-side and surfaced inline (editSourceError on a 403/404, the
  // existing 402/409 handling on save/rebuild below). Rendered as
  // a separate early-return dialog below (same pattern as the autoBuildPhase
  // branch just below this block) rather than threaded into the Source →
  // Configure → ... → Preview generation flow, which owns a large amount of
  // state and step-transition logic this mode has no use for.
  // ===========================================================================

  type EditSourceFile = { filename: string; content: string; origin?: string };
  type EditVariant = { dockerfile: string; files: EditSourceFile[]; origin?: string };
  type EditRevisionSummary = {
    revision_no: number;
    origin: string;
    author_user_id: number | null;
    created_at: string;
  };

  const [editActiveNodeId, setEditActiveNodeId] = useState<number | null>(null);
  const [editSourceLoading, setEditSourceLoading] = useState(false);
  const [editSourceError, setEditSourceError] = useState<string | null>(null);
  const [editMeta, setEditMeta] = useState<{
    node_id: number;
    name: string;
    is_system: boolean;
    editable: boolean;
    current_revision_no: number;
  } | null>(null);
  const [editRevisions, setEditRevisions] = useState<EditRevisionSummary[]>([]);
  const [editHeroDockerfile, setEditHeroDockerfile] = useState("");
  const [editHeroEntrypoint, setEditHeroEntrypoint] = useState("");
  const [editHeroBuildPush, setEditHeroBuildPush] = useState("");
  const [editHeroFiles, setEditHeroFiles] = useState<EditSourceFile[]>([]);
  const [editVariants, setEditVariants] = useState<Record<string, EditVariant>>({});
  const [editActiveVariant, setEditActiveVariant] = useState<string>("hero");
  const [editActiveFile, setEditActiveFile] = useState<string>("Dockerfile");
  const [editSaving, setEditSaving] = useState(false);
  const [editRebuilding, setEditRebuilding] = useState(false);
  const [editRebuildScope, setEditRebuildScope] = useState<"single_variant" | "full_fanout">("single_variant");
  const [editRebuildVariant, setEditRebuildVariant] = useState<string>("");
  const [editRebuildError, setEditRebuildError] = useState<string | null>(null);
  const [editCriticWarnings, setEditCriticWarnings] = useState<Array<{ critic?: string; severity?: string; message: string }>>([]);
  const [editRevisionPanelOpen, setEditRevisionPanelOpen] = useState(false);
  const [editCompareRevision, setEditCompareRevision] = useState<number | null>(null);
  const [editDiff, setEditDiff] = useState<Record<string, string> | null>(null);
  const [editDiffLoading, setEditDiffLoading] = useState(false);
  const [editReverting, setEditReverting] = useState(false);

  // Fetches GET /nodes/{nodeId}/source and hydrates hero +
  // variants + revisions (Contract 2). Reused on initial open AND after a
  // revert (server content changed independent of local edits).
  const loadEditSource = useCallback(async (nodeId: number) => {
    setEditSourceLoading(true);
    setEditSourceError(null);
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(`${API_URL}/nodes/${nodeId}/source`, {});
      if (!resp.ok) {
        const raw = await resp.text().catch(() => "");
        if (resp.status === 403) {
          throw new Error("You don't have permission to view or edit this node's code.");
        }
        throw new Error(raw || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setEditMeta({
        node_id: data.node_id,
        name: data.name,
        is_system: !!data.is_system,
        editable: !!data.editable,
        current_revision_no: data.current_revision_no,
      });
      setEditRevisions(Array.isArray(data.revisions) ? data.revisions : []);
      setEditHeroDockerfile(data.hero?.dockerfile || "");
      setEditHeroEntrypoint(data.hero?.entrypoint_sh || "");
      setEditHeroBuildPush(data.hero?.build_and_push_sh || "");
      const heroFiles: EditSourceFile[] = Array.isArray(data.hero?.source_files)
        ? data.hero.source_files.map((f: any) => ({ filename: f.filename, content: f.content, origin: f.origin }))
        : [];
      setEditHeroFiles(heroFiles);
      setEditActiveVariant("hero");
      setEditActiveFile("Dockerfile");
      const variants: Record<string, EditVariant> = {};
      if (data.variants && typeof data.variants === "object") {
        for (const [vk, vv] of Object.entries<any>(data.variants)) {
          variants[vk] = {
            dockerfile: vv?.dockerfile || "",
            files: Array.isArray(vv?.files)
              ? vv.files.map((f: any) => ({ filename: f.filename, content: f.content }))
              : [],
            origin: vv?.origin,
          };
        }
      }
      setEditVariants(variants);
      setEditActiveNodeId(nodeId);
    } catch (err: any) {
      setEditSourceError(err?.message || "Failed to load node source");
    } finally {
      setEditSourceLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open || !editNodeId || !accessToken) return;
    loadEditSource(editNodeId);
    // Only re-run when the wizard is (re)opened into edit mode for a
    // (possibly different) node — loadEditSource itself is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editNodeId, accessToken]);

  // GET /nodes/{node_id}/source/revisions — used to refresh the
  // revision dropdown after a save/rebuild/revert without re-fetching (and
  // clobbering in-progress edits to) the full hero/variant source.
  const loadEditRevisions = useCallback(async (nodeId: number) => {
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(`${API_URL}/nodes/${nodeId}/source/revisions`, {});
      if (!resp.ok) return;
      const data = await resp.json();
      setEditRevisions(Array.isArray(data.revisions) ? data.revisions : []);
    } catch {
      /* best-effort */
    }
  }, []);

  // "Save edits" — POST /nodes/{id}/source. On 409 must_fork
  // (the node is a system/platform node — Contract 2), auto-calls
  // POST /copy to create a tenant-owned editable draft, then retries the
  // save against the new node id, and swaps editActiveNodeId so Rebuild /
  // revisions / future saves target the fork instead of the read-only
  // original.
  const handleSaveEdits = async () => {
    if (!editActiveNodeId) return;
    setEditSaving(true);
    const API_URL = apiUrl();
    const body = {
      hero: {
        dockerfile: editHeroDockerfile,
        entrypoint_sh: editHeroEntrypoint,
        build_and_push_sh: editHeroBuildPush,
        source_files: editHeroFiles.map((f) => ({ filename: f.filename, content: f.content })),
      },
      source_overrides_by_variant: Object.fromEntries(
        Object.entries(editVariants)
          .filter(([, v]) => v.files.length > 0)
          .map(([vk, v]) => [vk, v.files.map((f) => ({ filename: f.filename, content: f.content }))]),
      ),
    };
    let targetNodeId = editActiveNodeId;
    try {
      let resp = await authenticatedFetch(`${API_URL}/nodes/${targetNodeId}/source`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 409) {
        const raw = await resp.text().catch(() => "");
        let code = "";
        // FastAPI wraps HTTPException(detail=...) as {"detail": {...}} —
        // the {"code": ...} shape Contract 2 documents sits under .detail.
        try { code = JSON.parse(raw)?.detail?.code || ""; } catch { /* keep raw */ }
        if (code !== "must_fork") throw new Error(raw || "Save conflict");
        toast({
          title: "This is a platform node",
          description: "Creating your own editable copy before saving…",
        });
        const copyResp = await authenticatedFetch(`${API_URL}/nodes/${targetNodeId}/copy`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!copyResp.ok) {
          const craw = await copyResp.text().catch(() => "");
          throw new Error(craw || `Copy failed (HTTP ${copyResp.status})`);
        }
        const copyData = await copyResp.json();
        targetNodeId = copyData.node_id;
        setEditActiveNodeId(targetNodeId);
        setEditMeta((prev) => prev
          ? { ...prev, node_id: targetNodeId as number, name: copyData.name, is_system: false, editable: true }
          : prev);
        resp = await authenticatedFetch(`${API_URL}/nodes/${targetNodeId}/source`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!resp.ok) {
          const raw2 = await resp.text().catch(() => "");
          throw new Error(raw2 || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        setEditMeta((prev) => prev ? { ...prev, current_revision_no: data.revision_no } : prev);
        toast({
          title: "Forked & saved",
          description: `Saved to your new node "${copyData.name}" (revision ${data.revision_no}).`,
        });
      } else if (!resp.ok) {
        const raw = await resp.text().catch(() => "");
        throw new Error(raw || `HTTP ${resp.status}`);
      } else {
        const data = await resp.json();
        setEditMeta((prev) => prev ? { ...prev, current_revision_no: data.revision_no } : prev);
        toast({
          title: "Saved",
          description: `Revision ${data.revision_no} (${data.origin}) saved.`,
        });
      }
      await loadEditRevisions(targetNodeId);
      window.dispatchEvent(new Event("mod:nodes:changed"));
    } catch (err: any) {
      toast({
        title: "Save failed",
        description: err?.message || String(err),
        variant: "destructive",
      });
    } finally {
      setEditSaving(false);
    }
  };

  // "Rebuild" — POST /nodes/{id}/rebuild. Maps the two
  // documented 409/402 error codes to a clear inline message; any
  // critic_warnings the server returns are shown as a non-blocking banner.
  const handleRebuild = async () => {
    if (!editActiveNodeId) return;
    if (editRebuildScope === "single_variant" && !editRebuildVariant) {
      toast({
        title: "Pick a variant",
        description: "Select which variant to rebuild, or switch to Full rebuild.",
        variant: "destructive",
      });
      return;
    }
    setEditRebuilding(true);
    setEditRebuildError(null);
    setEditCriticWarnings([]);
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(`${API_URL}/nodes/${editActiveNodeId}/rebuild`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scope: editRebuildScope,
          ...(editRebuildScope === "single_variant" ? { variant: editRebuildVariant } : {}),
        }),
      });
      if (resp.status === 409) {
        const raw = await resp.text().catch(() => "");
        let code = "";
        // FastAPI wraps HTTPException(detail=...) as {"detail": {...}}.
        try { code = JSON.parse(raw)?.detail?.code || ""; } catch { /* keep raw */ }
        setEditRebuildError(
          code === "registry_not_isolated"
            ? "This tenant isn't isolated from MOD's platform registry — rebuild blocked to avoid publishing into the shared registry. Contact support to finish tenant registry setup."
            : (raw || "Rebuild conflict."),
        );
        return;
      }
      if (resp.status === 402) {
        setEditRebuildError("Custom node rebuilds require the Custom Nodes entitlement.");
        return;
      }
      if (!resp.ok) {
        const raw = await resp.text().catch(() => "");
        throw new Error(raw || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      if (Array.isArray(data.critic_warnings) && data.critic_warnings.length > 0) {
        setEditCriticWarnings(data.critic_warnings);
      }
      toast({
        title: "Rebuild dispatched",
        description: `Workflow run #${data.workflow_run_id} started (${data.scope}).`,
      });
      if (data.workflow_run_id) {
        onNodeCreated?.({ taskId: data.workflow_run_id, nodeName: editMeta?.name || editNodeName || "" });
      }
      window.dispatchEvent(new Event("mod:nodes:changed"));
    } catch (err: any) {
      setEditRebuildError(err?.message || String(err));
    } finally {
      setEditRebuilding(false);
    }
  };

  // GET /nodes/{id}/source/diff?from=&to= — diffs the chosen
  // revision against the current one. The endpoint already returns
  // server-computed unified-diff text per filename (not raw before/after
  // pairs), so it's rendered with a small unified-diff line renderer
  // (UnifiedDiffText, below) rather than DiffView's before/after LCS
  // differ — DiffView's algorithm doesn't apply to already-diffed text.
  const handleLoadDiff = async (compareRevisionNo: number) => {
    if (!editActiveNodeId || !editMeta) return;
    setEditDiffLoading(true);
    setEditDiff(null);
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(
        `${API_URL}/nodes/${editActiveNodeId}/source/diff?from=${compareRevisionNo}&to=${editMeta.current_revision_no}`,
        {},
      );
      if (!resp.ok) {
        const raw = await resp.text().catch(() => "");
        throw new Error(raw || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setEditDiff(data.diff || {});
    } catch (err: any) {
      toast({
        title: "Diff failed",
        description: err?.message || String(err),
        variant: "destructive",
      });
    } finally {
      setEditDiffLoading(false);
    }
  };

  // "Revert" — POST /nodes/{id}/source/revert. Creates a new
  // revision copying the target revision's content, so we reload the full
  // /source payload afterward to hydrate the editor with the reverted
  // hero/variant source.
  const handleRevert = async (targetRevisionNo: number) => {
    if (!editActiveNodeId) return;
    setEditReverting(true);
    const API_URL = apiUrl();
    try {
      const resp = await authenticatedFetch(`${API_URL}/nodes/${editActiveNodeId}/source/revert`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_revision_no: targetRevisionNo }),
      });
      if (!resp.ok) {
        const raw = await resp.text().catch(() => "");
        throw new Error(raw || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      toast({
        title: "Reverted",
        description: `New revision ${data.revision_no} created from revision ${targetRevisionNo}.`,
      });
      await loadEditSource(editActiveNodeId);
      setEditCompareRevision(null);
      setEditDiff(null);
      window.dispatchEvent(new Event("mod:nodes:changed"));
    } catch (err: any) {
      toast({
        title: "Revert failed",
        description: err?.message || String(err),
        variant: "destructive",
      });
    } finally {
      setEditReverting(false);
    }
  };

  // Shared analysis-banner block. Rendered on both the Configure
  // step (where the user is editing) AND the Review step (where the
  // skip-to-review path lands them). Without the duplicate the user
  // who used "Analyze and jump to Review" never saw the warnings the
  // analyzer produced — they were stuck on Configure-step JSX.
  const analysisBanners = (
    <>
      {analysisConfidence && (
        <div className={`text-xs rounded-lg p-3 border ${
          analysisConfidence === "high"
            ? "bg-green-50 border-green-200 text-green-800"
            : analysisConfidence === "medium"
            ? "bg-blue-50 border-blue-200 text-blue-800"
            : "bg-yellow-50 border-yellow-200 text-yellow-900"
        }`}>
          <div className="font-medium mb-1">
            {analysisConfidence === "high" && "AI pre-filled these fields — review and hit Next"}
            {analysisConfidence === "medium" && "AI suggestions applied — some fields may need review"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("Analysis unavailable") &&
              "LLM analysis is currently unreachable — retry in a few seconds or fill in each field manually"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("Syntax error") &&
              "Your script has a syntax error — the analyzer can't read it"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("Repo mode:") &&
              "Couldn't sample this repo — fill the fields manually"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("Repo sample failed") &&
              "Couldn't reach the repo — fill the fields manually"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("No repo URL") &&
              "Paste a repo URL on the Source step first"}
            {analysisConfidence === "low" && analysisReasoning.startsWith("Image source") &&
              "Image source — analyzer skipped (image tags don't need code analysis)"}
            {analysisConfidence === "low" &&
              !analysisReasoning.startsWith("Analysis unavailable") &&
              !analysisReasoning.startsWith("Syntax error") &&
              !analysisReasoning.startsWith("Repo mode:") &&
              !analysisReasoning.startsWith("Repo sample failed") &&
              !analysisReasoning.startsWith("No repo URL") &&
              !analysisReasoning.startsWith("Image source") &&
              "AI couldn't analyze reliably — please fill in each field"}
          </div>
          {analysisReasoning && <div className="opacity-75">{analysisReasoning}</div>}
          {predictedAttempts !== null && predictedMinutes !== null && (
            <div className="mt-2 flex items-center gap-2 text-[11px]">
              <span className="inline-flex items-center gap-1 rounded-full bg-white/60 border border-current/20 px-2 py-0.5 font-medium">
                <span aria-hidden>⏱</span>
                Expected build: ~{predictedMinutes} min, {predictedAttempts} attempt{predictedAttempts === 1 ? "" : "s"}
              </span>
              {predictionBasis && (
                <span className="opacity-70 italic">{predictionBasis}</span>
              )}
            </div>
          )}
        </div>
      )}
      {inputType === "other" && outputType === "other" &&
        !(analysisConfidence === "low" &&
          (analysisReasoning.startsWith("Analysis unavailable") ||
           analysisReasoning.startsWith("Syntax error") ||
           analysisReasoning.startsWith("Repo mode:") ||
           analysisReasoning.startsWith("Repo sample failed") ||
           analysisReasoning.startsWith("Image source") ||
           analysisReasoning.startsWith("No repo URL"))) && (
        <div className="text-xs rounded-md border border-yellow-300 bg-yellow-50 px-3 py-1.5 text-yellow-900 flex items-center gap-2">
          <span className="font-semibold">Heads up:</span>
          <span className="flex-1">
            No reads from <code className="bg-yellow-100 px-1 rounded">/data/input/</code> or
            writes to <code className="bg-yellow-100 px-1 rounded">/data/output/</code> detected.
            Fine for benchmarks or scratch scripts; downstream workflow nodes won't be able to hand files to this one.
          </span>
        </div>
      )}
    </>
  );

  // node-code-edit-rebuild — "edit existing node code" mode. Takes over the
  // entire dialog body (same early-return pattern as autoBuildPhase below)
  // whenever the wizard was opened via NodeLibrary's "Edit code" action.
  if (editNodeId) {
    const heroFileTabs = ["Dockerfile", "entrypoint.sh", "build_and_push.sh", ...editHeroFiles.map((f) => f.filename)];
    const variantFileTabs = editActiveVariant !== "hero" ? (editVariants[editActiveVariant]?.files.map((f) => f.filename) || []) : [];
    const activeFileContent = editActiveVariant === "hero"
      ? (editActiveFile === "Dockerfile" ? editHeroDockerfile
        : editActiveFile === "entrypoint.sh" ? editHeroEntrypoint
        : editActiveFile === "build_and_push.sh" ? editHeroBuildPush
        : editHeroFiles.find((f) => f.filename === editActiveFile)?.content ?? "")
      : (editVariants[editActiveVariant]?.files.find((f) => f.filename === editActiveFile)?.content ?? "");
    const setActiveFileContent = (v: string) => {
      if (editActiveVariant === "hero") {
        if (editActiveFile === "Dockerfile") setEditHeroDockerfile(v);
        else if (editActiveFile === "entrypoint.sh") setEditHeroEntrypoint(v);
        else if (editActiveFile === "build_and_push.sh") setEditHeroBuildPush(v);
        else setEditHeroFiles((prev) => prev.map((f) => (f.filename === editActiveFile ? { ...f, content: v } : f)));
      } else {
        setEditVariants((prev) => ({
          ...prev,
          [editActiveVariant]: {
            ...prev[editActiveVariant],
            files: (prev[editActiveVariant]?.files || []).map((f) =>
              f.filename === editActiveFile ? { ...f, content: v } : f),
          },
        }));
      }
    };
    const displayName = editMeta?.name || editNodeName || `Node #${editNodeId}`;

    return (
      <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
        <DialogContent
          className="max-w-[960px] w-[960px] h-[760px] max-h-[95vh] flex flex-col text-left"
          onPointerDownOutside={(e) => e.preventDefault()}
        >
          <DialogHeader className="text-left flex-shrink-0">
            <DialogTitle className="flex items-center gap-2">
              <FileCode className="w-5 h-5" aria-hidden="true" />
              Edit node code — {displayName}
            </DialogTitle>
            <DialogDescription className="text-left">
              {editMeta?.is_system
                ? "This is a platform node — saving will create your own editable copy."
                : "Edit the hero source and per-variant overrides, then Save and Rebuild to ship the changes."}
            </DialogDescription>
          </DialogHeader>

          {editSourceLoading ? (
            <div className="flex-1 flex items-center justify-center">
              <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
            </div>
          ) : editSourceError ? (
            <div className="flex-1 flex items-center justify-center text-sm text-red-600 px-6 text-center">
              {editSourceError}
            </div>
          ) : (
            <div className="flex-1 min-h-0 flex flex-col gap-3 overflow-y-auto pr-1">
              {editCriticWarnings.length > 0 && (
                <div className="text-xs rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-amber-900 space-y-1">
                  <div className="font-semibold flex items-center gap-1">
                    <AlertTriangle className="w-3.5 h-3.5" /> Critic warnings
                  </div>
                  {editCriticWarnings.map((w, i) => (
                    <div key={i}>{w.severity ? `[${w.severity}] ` : ""}{w.critic ? `${w.critic}: ` : ""}{w.message}</div>
                  ))}
                </div>
              )}
              {editRebuildError && (
                <div className="text-xs rounded-md border border-red-300 bg-red-50 px-3 py-2 text-red-900">
                  {editRebuildError}
                </div>
              )}

              {/* Variant tabs */}
              <div className="flex gap-1 border-b text-xs flex-wrap flex-shrink-0">
                <button
                  type="button"
                  onClick={() => { setEditActiveVariant("hero"); setEditActiveFile("Dockerfile"); }}
                  className={`px-3 py-1.5 rounded-t border-b-2 ${editActiveVariant === "hero" ? "border-indigo-600 text-indigo-700 font-semibold" : "border-transparent text-gray-500 hover:text-gray-800"}`}
                >
                  Hero
                </button>
                {Object.keys(editVariants).map((vk) => (
                  <button
                    type="button"
                    key={vk}
                    onClick={() => { setEditActiveVariant(vk); setEditActiveFile(editVariants[vk]?.files[0]?.filename || "Dockerfile"); }}
                    className={`px-3 py-1.5 rounded-t border-b-2 uppercase ${editActiveVariant === vk ? "border-indigo-600 text-indigo-700 font-semibold" : "border-transparent text-gray-500 hover:text-gray-800"}`}
                  >
                    {vk}
                  </button>
                ))}
              </div>

              {/* File tabs */}
              <div className="flex gap-1 flex-wrap text-[11px] flex-shrink-0">
                {(editActiveVariant === "hero" ? heroFileTabs : variantFileTabs).map((fn) => (
                  <button
                    type="button"
                    key={fn}
                    onClick={() => setEditActiveFile(fn)}
                    className={`px-2 py-1 rounded border ${editActiveFile === fn ? "bg-indigo-600 text-white border-indigo-600" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}
                  >
                    {fn}
                  </button>
                ))}
              </div>

              {/* Editor — hero is always editable here (unlike the
                  generation-flow Preview step's readOnly hero textareas,
                  which protect an in-flight build's source of truth; this
                  dialog IS the edit surface). */}
              <div className="flex-1 min-h-[200px]">
                <CodeEditor value={activeFileContent} onChange={setActiveFileContent} />
              </div>

              {/* Save + rebuild controls */}
              <div className="border rounded-md p-3 space-y-2 bg-gray-50 flex-shrink-0">
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" disabled={editSaving} onClick={handleSaveEdits}>
                    {editSaving && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
                    Save edits
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => {
                    const next = !editRevisionPanelOpen;
                    setEditRevisionPanelOpen(next);
                    if (next && editActiveNodeId) loadEditRevisions(editActiveNodeId);
                  }}>
                    Revisions ({editRevisions.length})
                  </Button>
                  {editMeta && (
                    <span className="text-[11px] text-gray-500">Current: rev {editMeta.current_revision_no}</span>
                  )}
                </div>

                <div className="text-xs font-semibold text-gray-700 pt-1">Rebuild scope</div>
                <div className="flex items-center gap-4 text-xs">
                  <label className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      checked={editRebuildScope === "single_variant"}
                      onChange={() => setEditRebuildScope("single_variant")}
                    />
                    This variant only
                  </label>
                  <label className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      checked={editRebuildScope === "full_fanout"}
                      onChange={() => setEditRebuildScope("full_fanout")}
                    />
                    Full rebuild (all variants, re-register)
                  </label>
                </div>
                {editRebuildScope === "single_variant" && (
                  <select
                    className="text-xs border rounded px-2 py-1"
                    value={editRebuildVariant}
                    onChange={(e) => setEditRebuildVariant(e.target.value)}
                  >
                    <option value="">Select variant…</option>
                    {Object.keys(editVariants).map((vk) => (
                      <option key={vk} value={vk}>{vk}</option>
                    ))}
                  </select>
                )}
                <div>
                  <Button size="sm" disabled={editRebuilding} onClick={handleRebuild}>
                    {editRebuilding && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
                    Rebuild
                  </Button>
                </div>
              </div>

              {/* Revision panel */}
              {editRevisionPanelOpen && (
                <div className="border rounded-md p-3 space-y-2 flex-shrink-0">
                  <div className="text-xs font-semibold text-gray-700">Revision history</div>
                  <div className="flex items-center gap-2 text-xs flex-wrap">
                    <select
                      className="border rounded px-2 py-1"
                      value={editCompareRevision ?? ""}
                      onChange={(e) => {
                        const rv = parseInt(e.target.value, 10);
                        const val = Number.isFinite(rv) ? rv : null;
                        setEditCompareRevision(val);
                        if (val !== null) handleLoadDiff(val);
                        else setEditDiff(null);
                      }}
                    >
                      <option value="">Compare current against…</option>
                      {editRevisions
                        .filter((r) => r.revision_no !== editMeta?.current_revision_no)
                        .map((r) => (
                          <option key={r.revision_no} value={r.revision_no}>
                            Rev {r.revision_no} — {r.origin}{r.created_at ? ` (${new Date(r.created_at).toLocaleString()})` : ""}
                          </option>
                        ))}
                    </select>
                    {editCompareRevision !== null && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={editReverting}
                        onClick={() => handleRevert(editCompareRevision!)}
                      >
                        {editReverting && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
                        Revert to this revision
                      </Button>
                    )}
                  </div>
                  {editDiffLoading ? (
                    <div className="text-xs text-gray-500 flex items-center gap-1">
                      <Loader2 className="w-3 h-3 animate-spin" /> Loading diff…
                    </div>
                  ) : editDiff && Object.keys(editDiff).length > 0 ? (
                    <div className="space-y-2 max-h-[260px] overflow-auto">
                      {Object.entries(editDiff).map(([fn, diffText]) => (
                        <div key={fn}>
                          <div className="text-[11px] font-mono text-gray-500 mb-0.5">{fn}</div>
                          <UnifiedDiffText text={diffText} />
                        </div>
                      ))}
                    </div>
                  ) : editCompareRevision !== null ? (
                    <div className="text-xs text-gray-500 italic">No differences between these revisions.</div>
                  ) : null}
                </div>
              )}
            </div>
          )}

          <DialogFooter className="flex-shrink-0">
            <Button variant="outline" onClick={onClose}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  // #17/#20 (round 6) — Auto-build progress panel. Renders when the
  // user clicked Build on Review with auto mode on. Replaces the
  // wizard body so the user can SEE phase 1 → phase 2 → done. Failure
  // states show the error inline; success closes the dialog.
  if (autoBuildPhase) {
    const phaseLabel: Record<string, string> = {
      starting: "Starting auto-build…",
      phase1: "Phase 1: generating artifacts",
      phase2: "Phase 2: building + registering",
      done: "Done",
      failed: "Failed",
    };
    const isTerminal = autoBuildPhase === "done" || autoBuildPhase === "failed";
    return (
      <Dialog open={open} onOpenChange={(v) => !v && (isTerminal ? handleClose() : null)}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {autoBuildPhase === "failed" ? "Auto-build failed" : autoBuildPhase === "done" ? "Auto-build dispatched" : "Auto-build running"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="text-sm font-semibold text-gray-800">{phaseLabel[autoBuildPhase]}</div>
            {autoBuildPhaseDetail && (
              <div className="text-xs text-gray-600">{autoBuildPhaseDetail}</div>
            )}
            {autoBuildPhase !== "done" && autoBuildPhase !== "failed" && (
              <div className="flex items-center gap-2 text-xs text-gray-700">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>
                  {predictedMinutes !== null ? (
                    <>
                      Estimated time to finish: <strong>~{predictedMinutes} min</strong>
                      {predictedAttempts ? ` (${predictedAttempts} build attempt${predictedAttempts === 1 ? "" : "s"})` : ""}.
                    </>
                  ) : (
                    "Building your node — this usually takes about 2–8 minutes."
                  )}
                </span>
              </div>
            )}
            {generationError && (
              <div className="text-xs rounded-md border border-red-300 bg-red-50 px-3 py-2 text-red-900 whitespace-pre-wrap break-words">
                {generationError}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 text-[11px]">
              {/* Per-phase indicator. ✓ = complete, … = currently
                  running, · = pending. Previously this rendered
                  Phase 1 as ✓ while autoBuildPhase was still "phase1"
                  (i.e. it was actively running) — that misled users
                  into thinking phase 2 was stuck when really we
                  hadn't finished phase 1 yet. */}
              <div className={`px-2 py-1 rounded border ${
                ["phase2","done"].includes(autoBuildPhase) ? "bg-green-50 border-green-200 text-green-800" :
                autoBuildPhase === "phase1" ? "bg-blue-50 border-blue-200 text-blue-800" :
                autoBuildPhase === "failed" ? "bg-red-50 border-red-200 text-red-800" :
                "bg-gray-50 border-gray-200 text-gray-700"
              }`}>
                {["phase2","done"].includes(autoBuildPhase) ? "✓" :
                 autoBuildPhase === "phase1" ? "…" :
                 "·"} Phase 1 — artifact generation
              </div>
              <div className={`px-2 py-1 rounded border ${
                autoBuildPhase === "done" ? "bg-green-50 border-green-200 text-green-800" :
                autoBuildPhase === "phase2" ? "bg-blue-50 border-blue-200 text-blue-800" :
                "bg-gray-50 border-gray-200 text-gray-700"
              }`}>
                {autoBuildPhase === "done" ? "✓" :
                 autoBuildPhase === "phase2" ? "…" :
                 "·"} Phase 2 — build + register
              </div>
            </div>
          </div>
          <div className="flex items-center justify-end gap-2">
            {isTerminal ? (
              <Button onClick={() => { setAutoBuildPhase(null); setAutoBuildPhaseDetail(""); handleClose(); }}>
                Close
              </Button>
            ) : (
              <Button variant="outline" onClick={handleCloseAndNotify}>
                Run in background
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent
        className="max-w-[920px] w-[920px] h-[720px] max-h-[95vh] flex flex-col text-center"
        // Guard against accidental dismissal. A stray click outside the
        // dialog (or a browser focus event) should NOT drop the user
        // out of a 10-step wizard they've been filling in. Only the X
        // button + Escape can close now.
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
      >
        <WizardErrorBoundary>
        <DialogHeader className="text-left flex-shrink-0">
          <DialogTitle className="text-left flex items-center gap-2">
            <Sparkles className="w-5 h-5" aria-hidden="true" />
            {wbNavLabel('wb-nav-wizard-nodes', 'Node Wizard')}
          </DialogTitle>
          <DialogDescription className="text-left">
            Turn any Docker container, Dockerfile, or script into a workflow node.
          </DialogDescription>
        </DialogHeader>

        {/* Step indicators + auto-build controls (issue #3). The
            auto-mode toggle defaults on for new sessions; the pill
            renders next to the steps while auto-advance is in flight
            so the user can pause without playing whack-a-mole with
            the dialog's X. */}
        <div className="flex items-center justify-between gap-2 mb-2 flex-shrink-0">
          <div className="flex items-center gap-1">
            {activeSteps.map((step, i) => (
              <div key={step} className="flex items-center">
                <button
                  onClick={() => i <= stepIndex && setCurrentStep(step)}
                  className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                    i === stepIndex
                      ? "bg-indigo-100 text-indigo-700"
                      : i < stepIndex
                      ? "bg-green-100 text-green-700 cursor-pointer hover:bg-green-200"
                      : "bg-gray-100 text-gray-400"
                  }`}
                >
                  {i < stepIndex ? (
                    <Check className="w-3 h-3" />
                  ) : (
                    <span className="w-4 text-center">{i + 1}</span>
                  )}
                  {step}
                </button>
                {i < activeSteps.length - 1 && (
                  <ChevronRight className="w-3 h-3 text-gray-300 mx-1" />
                )}
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2">
            {autoAdvancing ? (
              <button
                type="button"
                onClick={cancelAutoAdvance}
                className="inline-flex items-center gap-1.5 text-[11px] font-medium text-amber-800 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded-full px-2.5 py-0.5"
                title="Auto-build is advancing through pre-filled steps. Click to stop on the next step."
              >
                <Loader2 className="w-3 h-3 animate-spin" />
                Auto-build running — click to pause
              </button>
            ) : (
              <label className="inline-flex items-center gap-1.5 text-[11px] text-gray-600 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={autoMode}
                  onChange={(e) => setAutoMode(e.target.checked)}
                  className="h-3 w-3 cursor-pointer"
                />
                Auto-build
              </label>
            )}
          </div>
        </div>

        {/* Middle section — header + step indicators pinned above,
            footer pinned below. `overflow-y-auto` means the scrollbar
            only appears when a step's content actually exceeds the
            dialog height (Network step with many presets, Review +
            Preview with long artifacts) — short steps like Source and
            Configure don't show a scrollbar. */}
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">

        {/* Step 1: Source */}
        {currentStep === "Source" && (
          <div className="space-y-2">
            {/* Tier 2 #1 — credit pre-flight banner. Shown ONLY when
                the user demonstrably can't afford the registration
                step (saving them 30 min of build time + a confusing
                final-step rejection). Hidden when:
                  - preflight didn't return (network blip / on-prem)
                  - user has enough credits
                  - server reported unlimited (admin / on-prem)
                Idempotent and read-only — the actual charge still
                happens at registration; this banner is purely
                advisory. */}
            {creditPreflight && !creditPreflight.can_create_node && (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-amber-600" />
                <div>
                  <div className="font-medium">
                    Not enough credits to register a node yet
                  </div>
                  <div className="mt-0.5">
                    Creating a custom node costs{" "}
                    <span className="font-semibold">{creditPreflight.node_creation_cost}</span>{" "}
                    credits.{" "}
                    {creditPreflight.credits_remaining !== null && (
                      <>
                        You currently have{" "}
                        <span className="font-semibold">{creditPreflight.credits_remaining}</span>.
                      </>
                    )}{" "}
                    The wizard will still let you preview the
                    generated artifacts, but the build won't register
                    until your balance covers the cost.
                  </div>
                </div>
              </div>
            )}
            {/* Disk-pressure banner. Surface upfront so the user
                doesn't sink 5+ minutes into a build that will fail
                on disk pressure with misleading errors. Auto-repair
                cannot fix this — it's a host-level operator task
                (docker system prune). */}
            {creditPreflight?.disk_warning && (
              <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-900 flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-red-600" />
                <div>
                  <div className="font-medium">
                    Host disk space is low — wizard builds will fail
                  </div>
                  <div className="mt-0.5">
                    {creditPreflight.disk_warning.message}
                  </div>
                </div>
              </div>
            )}
            {/* Per-step LLM Q&A panel (issue #18). When the
                analyzer emitted clarifying questions, render them
                inline above the source tabs so the user can answer
                before the wizard advances. The wizard re-fires
                analyze-script with `user_answers` on Continue,
                which propagates the resolution into Configure-step
                pre-fill. Auto-advance pauses while questions are
                outstanding. The LLM emits questions when it can't
                disambiguate intent from source alone — pure-compute
                nodes that do no I/O get an empty array and the
                user proceeds normally without questions. */}
            {clarifyingQuestions.length > 0 && (
              <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-3 space-y-3">
                <div className="text-xs font-semibold text-indigo-900 flex items-center gap-1.5">
                  <Sparkles className="w-3.5 h-3.5" />
                  Quick clarifications before we generate the artifacts
                </div>
                {clarifyingQuestions.map((q) => (
                  <div key={q.id} className="space-y-1.5">
                    <div className="text-xs text-indigo-900">
                      {q.question}
                      {q.source_location && (
                        <span className="ml-2 text-[10px] font-mono text-indigo-700/70">
                          ({q.source_location})
                        </span>
                      )}
                    </div>
                    {q.options.length > 0 ? (
                      <div className="flex flex-wrap gap-1.5">
                        {q.options.map((opt) => (
                          <button
                            key={opt}
                            type="button"
                            onClick={() => setQuestionAnswers((prev) => ({ ...prev, [q.id]: opt }))}
                            className={`text-[11px] px-2 py-1 rounded-full border transition-colors ${
                              questionAnswers[q.id] === opt
                                ? "bg-indigo-600 text-white border-indigo-600"
                                : "bg-white text-indigo-800 border-indigo-300 hover:border-indigo-500"
                            }`}
                          >
                            {opt}
                          </button>
                        ))}
                      </div>
                    ) : (
                      <Input
                        value={questionAnswers[q.id] || ""}
                        onChange={(e) =>
                          setQuestionAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))
                        }
                        placeholder="Type your answer…"
                        className="h-8 text-xs"
                      />
                    )}
                  </div>
                ))}
                <div className="flex items-center gap-2 pt-1">
                  <Button
                    size="sm"
                    onClick={async () => {
                      // Re-fire analyze-script with the collected
                      // answers. The LLM either asks more questions
                      // or returns a final analysis with no more
                      // clarifying_questions; either way the user
                      // sees the result here.
                      await runAnalysis();
                    }}
                    disabled={
                      isAnalyzing ||
                      clarifyingQuestions.some((q) => !questionAnswers[q.id])
                    }
                    className="h-7 text-xs"
                  >
                    {isAnalyzing ? "Re-analysing…" : "Answer & continue"}
                  </Button>
                  <button
                    type="button"
                    onClick={() => {
                      // Skip — clear questions and proceed with the
                      // analyzer's current best guess. Does NOT
                      // re-fire analyze-script; the existing
                      // pre-fill values stand.
                      setClarifyingQuestions([]);
                      setQuestionAnswers({});
                    }}
                    disabled={isAnalyzing}
                    className="text-[11px] text-indigo-700 hover:text-indigo-900 underline-offset-2 hover:underline disabled:opacity-50"
                  >
                    Skip & proceed
                  </button>
                </div>
              </div>
            )}

            {/* LLM status chip — always rendered so the Source step's
                layout never bounces. Three colors / messages driven by
                llmStatus: green when warm, yellow when cold-loading,
                gray while the initial status fetch is in-flight.
                WB-WIZARD-STATUS-CHIP (wizard audit A1/A2): when the API
                reports loaded=false WITH a detail, the chip shows that
                detail verbatim (amber, same style) instead of the generic
                "AI model warming up" — on a fresh CE nothing is bound and
                "warming up" is a lie. "warming up" stays only for the
                loaded=false + empty-detail case. Shared file: paid gets
                the same honest text when nothing is bound. */}
            <div className={`text-xs rounded-md px-2 py-1 border flex items-center gap-2 ${
              !llmStatus
                ? "bg-gray-50 border-gray-200 text-gray-700"
                : llmStatus.loaded
                  ? "bg-green-50 border-green-200 text-green-900"
                  : "bg-yellow-50 border-yellow-200 text-yellow-900"
            }`}>
              <span className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${
                !llmStatus
                  ? "bg-gray-400 animate-pulse"
                  : llmStatus.loaded ? "bg-green-500" : "bg-yellow-500 animate-pulse"
              }`} />
              <span className="flex-1 text-left truncate">
                {!llmStatus
                  ? <>Checking AI analyzer status…</>
                  : llmStatus.loaded
                    ? <>AI analyzer <b>ready</b>{llmStatus.model ? <> — {llmStatus.model}</> : null}. Paste your script and click Next.</>
                    : llmStatus.detail
                      ? <>{llmStatus.detail}</>
                      : <>AI model warming up{llmStatus.model ? <> — {llmStatus.model}</> : null}. First analysis can take up to 2 minutes.</>
                }
              </span>
              <button
                type="button"
                onClick={runWorkerLlmPing}
                disabled={isPingingWorker}
                className="text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded border border-current/40 hover:bg-black/5 disabled:opacity-50 flex-shrink-0"
                title="Dispatch a trivial task to a worker-spawned container and verify it can reach the LLM."
              >
                {isPingingWorker ? "pinging…" : "check worker"}
              </button>
            </div>

            {/* Syntax error callout — renders above the tabs so it's
                the first thing the user sees when they've typo'd. Blocks
                the Next button via canAdvance. */}
            {/* Syntax check chip — always rendered so the layout doesn't
                bounce when errors appear/disappear. Three states:
                  - gray, "Paste something" while both editors are empty
                  - red, "Syntax error — line N: ..." when the checker fails
                  - green, "Syntax looks good" when the checker passes
                Matches the visual weight of the AI analyzer chip above. */}
            {(() => {
              const hasContent =
                (sourceType === "script" && scriptContent.trim().length > 0) ||
                (sourceType === "dockerfile" && dockerfileContent.trim().length > 0);
              if (!hasContent) {
                return (
                  <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-1 text-[11px] text-left flex items-center gap-2 text-gray-600">
                    <span className="inline-block w-2 h-2 rounded-full bg-gray-300 flex-shrink-0" />
                    <span>Syntax check runs after you paste.</span>
                  </div>
                );
              }
              if (syntaxError) {
                return (
                  <div className="rounded-md border border-red-200 bg-red-50 px-3 py-1 text-[11px] text-left flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-red-500 flex-shrink-0" />
                    <span className="font-semibold text-red-800">
                      Syntax error{syntaxError.line ? <span className="font-normal text-red-700 ml-1">— line {syntaxError.line}</span> : null}:
                    </span>
                    <span className="font-mono text-red-700 truncate flex-1">
                      {syntaxError.error}
                    </span>
                  </div>
                );
              }
              return (
                <div className="rounded-md border border-green-200 bg-green-50 px-3 py-1 text-[11px] text-left flex items-center gap-2 text-green-900">
                  <span className="inline-block w-2 h-2 rounded-full bg-green-500 flex-shrink-0" />
                  <span>Syntax looks good{syntaxChecking ? " — rechecking…" : "."}</span>
                </div>
              );
            })()}
            <Tabs value={sourceType} onValueChange={(v) => setSourceType(v as SourceType)}>
              <TabsList className="grid grid-cols-5 w-full">
                <TabsTrigger value="image" className="flex items-center gap-1 text-xs">
                  <Container className="w-3.5 h-3.5" />
                  Image
                </TabsTrigger>
                <TabsTrigger value="dockerfile" className="flex items-center gap-1 text-xs">
                  <FileCode className="w-3.5 h-3.5" />
                  Dockerfile
                </TabsTrigger>
                <TabsTrigger value="repo" className="flex items-center gap-1 text-xs">
                  <GitBranch className="w-3.5 h-3.5" />
                  Git Repo
                </TabsTrigger>
                <TabsTrigger value="script" className="flex items-center gap-1 text-xs">
                  <Upload className="w-3.5 h-3.5" />
                  Script
                </TabsTrigger>
                <TabsTrigger
                  value="describe"
                  className="flex items-center gap-1 text-xs"
                >
                  <Sparkles className="w-3.5 h-3.5" />
                  Describe
                </TabsTrigger>
              </TabsList>

              <TabsContent value="image" className="space-y-2 mt-2 min-h-[420px]">
                <p className="text-[11px] text-muted-foreground leading-snug">
                  Pull a pre-built image from Docker Hub, GHCR, ECR, etc.
                </p>
                <div className="space-y-1">
                  <Label htmlFor="docker-image" className="text-xs">Docker Image</Label>
                  <Input
                    id="docker-image"
                    placeholder="e.g., ubuntu:latest, ghcr.io/org/tool:v2, registry.example.com/team/processor:latest"
                    value={dockerImage}
                    onChange={(e) => setDockerImage(e.target.value)}
                    className="h-8 text-xs"
                  />
                  <p className="text-[11px] text-muted-foreground leading-snug">
                    Must read from <code className="bg-gray-100 px-1 rounded">/data/input/</code> and write to <code className="bg-gray-100 px-1 rounded">/data/output/</code>.
                  </p>
                  {imageRefChecking && (
                    <p className="text-[11px] text-gray-500 italic flex items-center gap-1">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      Verifying the image exists on the registry…
                    </p>
                  )}
                  {imageRefCheck && imageRefCheck.exists === false && (
                    <div className="text-xs rounded-md border border-red-300 bg-red-50 px-3 py-2 text-red-900 space-y-2">
                      <div className="font-medium">
                        Image not found on {imageRefCheck.registry_source || "the registry"}
                      </div>
                      <div className="text-[11px] leading-snug">
                        {imageRefCheck.reason}
                      </div>
                      {imageRefCheck.suggestions && imageRefCheck.suggestions.length > 0 && (
                        <div className="space-y-1 pt-1 border-t border-red-200">
                          <div className="text-[11px] font-medium">Try one of these instead:</div>
                          {imageRefCheck.suggestions.map((s, i) => (
                            <button
                              key={i}
                              type="button"
                              onClick={() => setDockerImage(s.ref)}
                              className="block w-full text-left rounded border border-red-200 bg-white px-2 py-1.5 hover:bg-red-100 hover:border-red-300 transition-colors"
                            >
                              <code className="text-[11px] font-mono text-red-900">{s.ref}</code>
                              {s.why && (
                                <div className="text-[10px] text-red-700 mt-0.5">{s.why}</div>
                              )}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  {imageRefCheck && imageRefCheck.exists === true && (
                    <p className="text-[11px] text-green-700 flex items-center gap-1">
                      <Check className="w-3 h-3" />
                      Found on {imageRefCheck.registry_source}.
                    </p>
                  )}
                  {imageRefCheck && imageRefCheck.exists === null && imageRefCheck.reason && (
                    <p className="text-[11px] text-amber-700 leading-snug">
                      Couldn&apos;t verify: {imageRefCheck.reason}
                    </p>
                  )}
                </div>
                <div className="space-y-1">
                  <Label className="flex items-center gap-1.5 text-xs">
                    <KeyRound className="w-3.5 h-3.5" />
                    Registry Credentials
                  </Label>
                  <Select value={registryCredential} onValueChange={setRegistryCredential}>
                    <SelectTrigger className="h-8 text-xs">
                      <SelectValue placeholder="None (public image)" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None (public image)</SelectItem>
                      {availableSecrets.map((s) => (
                        <SelectItem key={s.id} value={s.name}>
                          {s.name}
                        </SelectItem>
                      ))}
                      {availableSecrets.length === 0 && (
                        <SelectItem value="_no_secrets" disabled>
                          No secrets configured — add in Dashboard
                        </SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                  <p className="text-[11px] text-muted-foreground leading-snug">
                    Only needed for private registries — secret should be the registry token.
                  </p>
                </div>
              </TabsContent>

              <TabsContent value="dockerfile" className="space-y-2 mt-2 min-h-[420px]">
                <p className="text-[11px] text-muted-foreground leading-snug">
                  Paste a Dockerfile — we'll wrap a curated base and show the result on the next step.
                </p>
                <div className="space-y-1">
                  <Label className="text-xs">Upload Dockerfile</Label>
                  <Input
                    type="file"
                    accept=".dockerfile,Dockerfile,*"
                    onChange={handleFileUpload}
                    className="cursor-pointer h-8 text-xs"
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">Or paste contents</Label>
                  <NumberedEditor
                    placeholder="FROM python:3.11-slim&#10;WORKDIR /app&#10;COPY . .&#10;..."
                    value={dockerfileContent}
                    onChange={setDockerfileContent}
                    errorLine={syntaxError?.line ?? null}
                    minHeight="min-h-[280px]"
                    maxHeight="max-h-[280px]"
                  />
                </div>
              </TabsContent>

              <TabsContent value="repo" className="space-y-2 mt-2 min-h-[420px]">
                <p className="text-[11px] text-muted-foreground leading-snug">
                  Clone a Git repo — AI writes the Dockerfile. GitHub, GitLab, Bitbucket, or any Git URL.
                </p>
                <div className="space-y-1">
                  <Label htmlFor="repo-url" className="text-xs">Repository URL</Label>
                  <Input
                    id="repo-url"
                    placeholder="https://github.com/user/repo or git@github.com:user/repo.git"
                    value={repoUrl}
                    onChange={(e) => setRepoUrl(e.target.value)}
                    className="h-8 text-xs"
                  />
                  {/* δ Phase 11 — repo safety banner. Renders under the
                      URL input as soon as the debounced check resolves.
                      Hard refusal (license/archived/keyword) is rose;
                      pass-with-warnings is amber; clean is green. */}
                  {repoSafetyChecking && (
                    <div className="text-[10px] text-muted-foreground italic">
                      Checking repository licence + safety…
                    </div>
                  )}
                  {!repoSafetyChecking && repoSafety && !repoSafety.ok && (
                    <div className="rounded-md border border-rose-200 bg-rose-50 px-2 py-1.5 text-[11px] text-rose-900 space-y-0.5">
                      <div className="font-medium">
                        {repoSafety.not_github
                          ? "Unsupported source"
                          : repoSafety.not_configured
                            ? "Platform not configured"
                            : "Repository rejected"}
                      </div>
                      <ul className="list-disc pl-4">
                        {(repoSafety.reasons || []).map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {!repoSafetyChecking && repoSafety && repoSafety.ok && (
                    <div className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1.5 text-[11px] text-emerald-900">
                      <div className="flex items-center gap-1">
                        <CheckCircle2 className="w-3 h-3" />
                        <span>
                          {repoSafety.full_name || "Repository"}{" "}
                          ({repoSafety.license_spdx || "permissive"}) — looks good
                        </span>
                      </div>
                      {(repoSafety.warnings || []).length > 0 && (
                        <ul className="list-disc pl-4 mt-0.5 text-amber-800">
                          {(repoSafety.warnings || []).map((w, i) => (
                            <li key={i}>{w}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
                <div className="space-y-1">
                  <Label className="flex items-center gap-1.5 text-xs">
                    <KeyRound className="w-3.5 h-3.5" />
                    Credentials
                  </Label>
                  <Select value={repoCredential} onValueChange={setRepoCredential}>
                    <SelectTrigger className="h-8 text-xs">
                      <SelectValue placeholder="None (public repo)" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None (public repo)</SelectItem>
                      {availableSecrets.map((s) => (
                        <SelectItem key={s.id} value={s.name}>
                          {s.name}
                        </SelectItem>
                      ))}
                      {availableSecrets.length === 0 && (
                        <SelectItem value="_no_secrets" disabled>
                          No secrets configured — add in Dashboard
                        </SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                  <p className="text-[11px] text-muted-foreground leading-snug">
                    Only for private repos — secret should be a PAT or password.
                  </p>
                </div>
              </TabsContent>

              <TabsContent value="script" className="space-y-2 mt-2 min-h-[420px]">
                <p className="text-[11px] text-muted-foreground leading-snug">
                  Paste a script (Python, Bash, Node, Go, …) — AI picks a base and writes the Dockerfile.
                </p>
                <div className="space-y-1">
                  <Label className="text-xs">Upload</Label>
                  {/* #14 round-3 — single drop zone. Drag a folder in
                      to upload its full contents (structure preserved
                      via webkitGetAsEntry); drag files in for a flat
                      list; click to open the native file picker for
                      multi-file selection. The browser's native dialog
                      can't switch between files and folders in one
                      popup, so drag-drop is how this UX gets unified
                      without a Files/Folder mode toggle. */}
                  <div
                    onDragEnter={(e) => { e.preventDefault(); e.stopPropagation(); setScriptDropActive(true); }}
                    onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setScriptDropActive(true); }}
                    onDragLeave={(e) => { e.preventDefault(); e.stopPropagation(); setScriptDropActive(false); }}
                    onDrop={handleScriptFileDrop}
                    onClick={() => scriptFilePickerRef.current?.click()}
                    className={
                      "flex flex-col items-center justify-center gap-1 px-4 py-6 border-2 border-dashed rounded-md cursor-pointer transition-colors " +
                      (scriptDropActive
                        ? "border-indigo-500 bg-indigo-50"
                        : "border-gray-300 bg-gray-50 hover:border-indigo-400 hover:bg-indigo-50/40")
                    }
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-6 h-6 text-gray-400">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                      <polyline points="17 8 12 3 7 8" />
                      <line x1="12" y1="3" x2="12" y2="15" />
                    </svg>
                    <span className="text-xs text-gray-700 text-center">
                      Drop files or a folder here, or click to browse files
                    </span>
                    <span className="text-[10px] text-gray-500 text-center">
                      Folders preserve their structure
                    </span>
                  </div>
                  {/* Hidden native picker — files mode (multiple).
                      Folder uploads happen via drag-drop above; the
                      browser doesn't let one input cover both modes. */}
                  <input
                    ref={scriptFilePickerRef}
                    type="file"
                    accept=".py,.sh,.js,.ts,.r,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.cs,.php,.pl,.lua,.txt,*"
                    multiple
                    onChange={handleScriptFileUpload}
                    className="hidden"
                  />
                  {scriptFilename && (
                    <div className="flex flex-wrap gap-1">
                      {scriptFilename.split(", ").map((f, i) => (
                        <Badge key={i} variant="secondary" className="text-xs">{f}</Badge>
                      ))}
                    </div>
                  )}
                </div>
                <div className="space-y-2">
                  <div className="flex items-end justify-between">
                    <Label>Or paste contents</Label>
                    <div className="flex items-center gap-3">
                      {/* Multi-file toggle. Seeds the list from
                          scriptContent on first enable so the user
                          doesn't lose a paste. Disables when the
                          single-file flow is preferred (default). */}
                      <button
                        type="button"
                        onClick={() => {
                          if (!useMultifileInput && sourceFiles.length === 0) {
                            const seed: WizardSourceFile = makeEmptyFile(
                              scriptFilename || "main.py",
                            );
                            seed.body = scriptContent;
                            setSourceFiles([seed]);
                          }
                          setUseMultifileInput((v) => !v);
                        }}
                        className={[
                          "text-xs px-2 py-1 rounded border flex items-center gap-1",
                          useMultifileInput
                            ? "bg-indigo-50 border-indigo-300 text-indigo-700"
                            : "border-gray-300 text-gray-600 hover:bg-gray-50",
                        ].join(" ")}
                        title="Switch between single-paste and multi-file input"
                      >
                        <Files className="w-3 h-3" />
                        {useMultifileInput ? "Multi-file" : "Single-file"}
                      </button>
                      <Label className="text-xs text-gray-500">Language</Label>
                      <Select value={scriptInterpreter} onValueChange={setScriptInterpreter}>
                        <SelectTrigger className="w-40 h-8 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {[
                            { v: "python3", l: "Python" },
                            { v: "bash", l: "Bash / Shell" },
                            { v: "node", l: "JavaScript (Node)" },
                            { v: "typescript", l: "TypeScript" },
                            { v: "ruby", l: "Ruby" },
                            { v: "php", l: "PHP" },
                            { v: "perl", l: "Perl" },
                            { v: "lua", l: "Lua" },
                            { v: "go", l: "Go" },
                            { v: "rust", l: "Rust" },
                            { v: "c", l: "C" },
                            { v: "cpp", l: "C++" },
                            { v: "java", l: "Java" },
                            { v: "r", l: "R" },
                            { v: "text", l: "Text (.txt) / Plain text" },
                            { v: "json", l: "JSON" },
                            { v: "yaml", l: "YAML" },
                            { v: "toml", l: "TOML" },
                            { v: "xml", l: "XML" },
                          ].map((o) => (
                            <SelectItem key={o.v} value={o.v} className="text-xs">{o.l}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                  {useMultifileInput ? (
                    <SourceFilesPane
                      files={sourceFiles}
                      onChange={(next) => {
                        setSourceFiles(next);
                        // Keep the legacy single-paste field in sync with
                        // whatever the entrypoint file holds so analysis /
                        // syntax-check paths that still read scriptContent
                        // see the right bytes.
                        const ep = next.find((f) => f.is_entrypoint) || next[0];
                        if (ep) {
                          setScriptContent(ep.body);
                          setScriptFilename(ep.filename);
                        }
                      }}
                    />
                  ) : (
                    <NumberedEditor
                      placeholder="#!/usr/bin/env python3&#10;import sys&#10;..."
                      value={scriptContent}
                      onChange={setScriptContent}
                      onPaste={(e) => {
                        // #18 — auto-detect language from pasted content
                        // on EVERY paste, regardless of the dropdown's
                        // current value. Detection running over a
                        // confident match always wins; if detection
                        // returns null (short snippet, low confidence)
                        // the dropdown is left untouched. Tim's report:
                        // pasting Python while the dropdown was JSON
                        // should switch back to Python, not flag a
                        // syntax error against the wrong language.
                        const text = e.clipboardData?.getData("text") || "";
                        const detected = detectLanguage(text);
                        if (detected) setScriptInterpreter(detected);
                      }}
                      errorLine={syntaxError?.line ?? null}
                      minHeight="min-h-[280px]"
                      maxHeight="max-h-[280px]"
                    />
                  )}
                </div>
              </TabsContent>

              {/* δ §21 — Describe-and-build tab, chat redesign.
                  The user types in plain words, the system asks
                  follow-up questions. Repos surface as inline cards
                  when /describe-search finds matches; otherwise the
                  chat walks the user through input/output format
                  before calling /describe-synthesize. The reducer
                  in `lib/describe-chat.ts` owns the transitions —
                  this view is dumb render + dispatch. */}
              <TabsContent value="describe" className="space-y-2 mt-2 min-h-[420px]">
                {!describeAvailable && isPlatformAdmin && (
                  <div className="rounded-md border border-orange-300 bg-orange-50 px-3 py-2 text-[11px] text-orange-900">
                    <div className="font-medium">Admin notice — GitHub access not configured</div>
                    <div className="mt-0.5 leading-snug">
                      No <code className="bg-white/60 px-1 rounded">github_token</code> secret is set on the platform tenant.
                      Describe-search will fall back to unauthenticated GitHub
                      requests (60/hr global rate limit) and may fail under
                      load. Configure a GitHub PAT in Dashboard → Settings → Secrets
                      to lift this. End users see no indicator — search
                      still works, just slower / less reliable.
                    </div>
                  </div>
                )}
                {describeSearchNotConfigured && (
                  <div
                    role="status"
                    className="rounded-md border border-orange-300 bg-orange-50 px-3 py-2 text-[11px] text-orange-900"
                  >
                    GitHub search is unavailable: the platform github_token is not
                    configured. The node will be built from scratch.
                  </div>
                )}
                <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
                  <div className="font-medium">License-vetted code only</div>
                  <div className="mt-0.5 leading-snug">
                    We hard-block GPL / AGPL / LGPL / SSPL / non-commercial
                    licences. Permissive licences only — MIT, Apache-2.0,
                    BSD-2/3, ISC, MPL-2.0, Unlicense.
                  </div>
                </div>

                {/* Chat transcript. No fixed height + no overflow —
                    the bubbles size to their content and the wizard
                    body (line 3184) provides the outer scroll if the
                    whole tab ever exceeds the dialog. */}
                <div className="rounded-md border border-border bg-muted/30 px-3 py-2 space-y-3">
                  {chatState.messages.map((msg) => (
                    <DescribeChatBubble
                      key={msg.id}
                      msg={msg}
                      chosenRepo={chatState.chosenRepo}
                      onPickRepo={(fullName) => dispatchChat({ type: "pick_repo", fullName })}
                      onBuildFresh={() => dispatchChat({ type: "build_fresh" })}
                      onBuildIt={() => runDescribeSynthesize()}
                      onSwitchToWorkflow={() => onSwitchToWorkflow?.(chatState.description || "")}
                      liveStatus={
                        chatState.step === "synthesizing" && msg.pending
                          ? synthStatusLine(synthTick, synthAttempt, synthStage, synthStageName)
                          : undefined
                      }
                      disabled={chatState.step === "synthesizing"}
                    />
                  ))}
                  {/* Auto-scroll sentinel — `chatBottomRef` scrolls into
                      view whenever the chat advances. Without this,
                      clicking a repo card spawns the "ready_to_build"
                      system bubble below the candidate list but the
                      wizard's outer scroll never moves; user misses
                      the "Build it" CTA entirely. Stage repro
                      2026-05-27. */}
                  <div ref={chatBottomRef} aria-hidden />
                </div>

                {/* Composer */}
                <div className="flex gap-2 items-end">
                  <Textarea
                    id="describe-chat-composer"
                    placeholder={
                      chatState.step === "initial"
                        ? "e.g. Convert PDFs to PNG, one image per page"
                        : chatState.step === "ask_input" || chatState.step === "ask_output"
                          ? "Type your answer here…"
                          : chatState.step === "choose_repo"
                            ? "Not quite right? Describe what you actually need…"
                            : chatState.step === "ready_to_build"
                              ? "Want to refine before we build? Tell me what to factor in…"
                              : "Use the buttons above to continue"
                    }
                    value={composerText}
                    onChange={(e) => setComposerText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleComposerSend();
                      }
                    }}
                    disabled={!composerEnabledIn(chatState.step)}
                    className="text-xs min-h-[60px]"
                    maxLength={2000}
                  />
                  <Button
                    type="button"
                    size="sm"
                    className="h-9 text-[11px]"
                    disabled={!composerEnabledIn(chatState.step) || !composerText.trim()}
                    onClick={handleComposerSend}
                  >
                    Send
                  </Button>
                </div>

                {/* γ — real-data sample input. Optional file the user
                    can attach now so the build-time gate runs the
                    freshly-built image against THEIR data, not just
                    synthetic tensors. */}
                <div className="rounded-md border border-dashed border-muted-foreground/30 bg-muted/30 px-3 py-3 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <Label className="text-xs font-medium">
                        Add a sample input (optional, ≤{SAMPLE_INPUT_MAX_BYTES / (1024 * 1024)} MB)
                      </Label>
                      <p className="text-[11px] text-muted-foreground leading-snug mt-0.5">
                        We&apos;ll run your sample through the freshly-built
                        image and show you the output before registering
                        the node. Catches semantic bugs that pass synthetic
                        smoke tests.
                      </p>
                    </div>
                    {sampleInputFile && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-6 text-[11px] px-2"
                        onClick={() => handleSampleFileSelect(null)}
                      >
                        Remove
                      </Button>
                    )}
                  </div>
                  <input
                    ref={sampleFilePickerRef}
                    type="file"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0] || null;
                      handleSampleFileSelect(f);
                      if (e.target) e.target.value = "";
                    }}
                  />
                  {!sampleInputFile && sampleInputFilename && (
                    <div className="rounded-sm bg-amber-50 border border-amber-200 px-2 py-1.5 text-[11px] text-amber-900">
                      Previously attached <span className="font-mono">{sampleInputFilename}</span> — re-attach the file to use it for this build.
                    </div>
                  )}
                  {sampleInputFile ? (
                    <div className="flex items-center justify-between gap-2 rounded-sm bg-background border border-border px-2 py-1.5">
                      <div className="text-[11px] truncate">
                        <span className="font-mono">{sampleInputFile.name}</span>
                        <span className="text-muted-foreground ml-2">
                          ({(sampleInputFile.size / 1024).toFixed(1)} KB)
                        </span>
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-6 text-[11px] px-2"
                        onClick={() => sampleFilePickerRef.current?.click()}
                      >
                        Replace
                      </Button>
                    </div>
                  ) : (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 text-[11px]"
                      onClick={() => sampleFilePickerRef.current?.click()}
                    >
                      Choose file
                    </Button>
                  )}
                </div>
              </TabsContent>
            </Tabs>
          </div>
        )}

        {/* Step 2: Configure */}
        {currentStep === "Configure" && (
          <div className="space-y-4">
            {/* α — hard schema gate banner. Fires when the analyzer
                flagged I/O as low confidence (Script/Repo/Dockerfile
                modes) OR when there's no analyzer at all (Image
                mode pre-marks both fields as aiFilledFields to force
                the user to confirm). Next button disabled while
                visible — see lib/io-gate.ts. The "Confirm as shown"
                button is the rubber-stamp path: when image/image (or
                whatever defaults are present) ARE the right answer,
                clicking the button is the user's explicit yes. */}
            {needsIOConfirmation({
              sourceType,
              schemaConfidence,
              aiFilledFields,
            }) && (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-amber-600" />
                  <div className="flex-1">
                    <div className="font-medium">
                      Confirm the input + output types
                    </div>
                    <div className="mt-0.5">
                      {ioBannerBody(sourceType)}
                    </div>
                  </div>
                </div>
                <div className="mt-2 flex justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-xs h-7"
                    onClick={() => {
                      const next = new Set(aiFilledFields);
                      next.delete("inputType");
                      next.delete("outputType");
                      setAiFilledFields(next);
                    }}
                  >
                    Confirm {inputType} → {outputType} as shown
                  </Button>
                </div>
              </div>
            )}
            {/* Analysis outcome banner + No-I/O heads-up — both surfaces
                are now shared with the Review step (issue #13) via the
                analysisBanners JSX defined above; that way users who
                used "Analyze and jump to Review" still see the
                analyzer's warnings on the destination step. */}
            {analysisBanners}

            {/* Base image — AI picks one from the curated catalog on
                source analysis; user can override here. Only shown for
                script + dockerfile flows where we wrap the user's code
                FROM a MOD-scanned base. */}
            {baseImages.length > 0 && (sourceType === "script" || sourceType === "dockerfile") && (
              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  <Container className="w-3.5 h-3.5" />
                  Base image
                  <Badge variant="outline" className="text-[10px] py-0 h-4">MOD-scanned</Badge>
                  {aiFilledFields.has("baseImage") && (
                    <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI suggested</span>
                  )}
                </Label>
                <Select
                  value={selectedBaseImage}
                  onValueChange={(v) => {
                    onSelectBaseImage(v);
                    if (aiFilledFields.has("baseImage")) {
                      const next = new Set(aiFilledFields);
                      next.delete("baseImage");
                      setAiFilledFields(next);
                    }
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {baseImages.map((b) => (
                      <SelectItem key={b.variant_dir} value={b.variant_dir}>
                        <span className="font-medium">{b.display_name}</span>
                        <span className="text-muted-foreground ml-2 text-xs">{b.description}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Your code runs FROM this base. Trivy scans only the delta —
                  vulnerability reports stay bounded to your code, not the whole image.
                </p>
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="node-name" className="flex items-center gap-2">
                Node Name *
                {aiFilledFields.has("nodeName") && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI suggested</span>}
              </Label>
              <Input
                id="node-name"
                placeholder="e.g., FFmpeg Video Converter"
                value={nodeName}
                onChange={(e) => {
                  setNodeName(e.target.value);
                  if (aiFilledFields.has("nodeName")) {
                    const next = new Set(aiFilledFields);
                    next.delete("nodeName");
                    setAiFilledFields(next);
                  }
                }}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="node-desc" className="flex items-center gap-2">
                Description
                {aiFilledFields.has("description") && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI suggested</span>}
              </Label>
              <Input
                id="node-desc"
                placeholder="What does this node do?"
                value={description}
                onChange={(e) => {
                  setDescription(e.target.value);
                  if (aiFilledFields.has("description")) {
                    const next = new Set(aiFilledFields);
                    next.delete("description");
                    setAiFilledFields(next);
                  }
                }}
                className="text-sm"
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  Input Type
                  {aiFilledFields.has("inputType") && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI</span>}
                </Label>
                <Select value={inputType} onValueChange={(v) => { setInputType(v); const n = new Set(aiFilledFields); n.delete("inputType"); setAiFilledFields(n); }}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {/* AI-suggested type that isn't in the curated list
                        (e.g. niche format like "rvt", "vcf", "nifti")
                        renders at the top so the user sees what the LLM
                        chose and the dropdown shows the right label. */}
                    {inputType && !INPUT_TYPES.find((t) => t.value === inputType) && (
                      <SelectItem key={`__custom_in_${inputType}`} value={inputType}>
                        <span>{inputType}</span>
                        <span className="text-xs text-muted-foreground ml-2">Custom (AI-suggested)</span>
                      </SelectItem>
                    )}
                    {INPUT_TYPES.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        <span>{t.label}</span>
                        <span className="text-xs text-muted-foreground ml-2">{t.desc}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  Output Type
                  {aiFilledFields.has("outputType") && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI</span>}
                </Label>
                <Select value={outputType} onValueChange={(v) => { setOutputType(v); const n = new Set(aiFilledFields); n.delete("outputType"); setAiFilledFields(n); }}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {outputType && !OUTPUT_TYPES.find((t) => t.value === outputType) && (
                      <SelectItem key={`__custom_out_${outputType}`} value={outputType}>
                        <span>{outputType}</span>
                        <span className="text-xs text-muted-foreground ml-2">Custom (AI-suggested)</span>
                      </SelectItem>
                    )}
                    {OUTPUT_TYPES.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        <span>{t.label}</span>
                        <span className="text-xs text-muted-foreground ml-2">{t.desc}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Use cols-2 when credits are hidden so Processing + Timeout
                sit side-by-side with matched widths. Labels are locked
                to the same height (items-end on the row) so the AI
                badge on Processing's label doesn't push its input
                down relative to Timeout's. */}
            <div className={`grid gap-4 items-end ${CREDITS_UI_ENABLED ? "grid-cols-3" : "grid-cols-2"}`}>
              <div className="space-y-2">
                <Label className="flex items-center gap-2 h-5">
                  Processing
                  {aiFilledFields.has("processType") && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">AI</span>}
                  {clusterCapabilities && clusterCapabilities.recommended.toUpperCase() !== "CPU" && (
                    <span
                      className="text-[10px] bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded"
                      title={clusterCapabilities.description}
                    >
                      cluster: {clusterCapabilities.recommended.toUpperCase()}
                    </span>
                  )}
                </Label>
                <Select value={processType} onValueChange={(v) => { setProcessType(v); const n = new Set(aiFilledFields); n.delete("processType"); setAiFilledFields(n); }}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {/* Filter options to what the deployment can actually
                        run. Always show CPU (universal); show each
                        accelerator only if the cluster has it. Falls open
                        to showing CPU + CUDA when clusterCapabilities is
                        still loading or the endpoint failed — never block
                        the wizard on a flaky capabilities query. */}
                    {(() => {
                      const avail = clusterCapabilities?.available;
                      const has = (cap: string) => !avail || avail.includes(cap);
                      const items = [
                        has("cpu") && (
                          <SelectItem key="CPU" value="CPU">
                            <span className="flex items-center gap-1.5"><Cpu className="w-3 h-3" /> CPU</span>
                          </SelectItem>
                        ),
                        has("cuda") && (
                          <SelectItem key="CUDA" value="CUDA">
                            <span className="flex items-center gap-1.5"><Monitor className="w-3 h-3" /> GPU (CUDA)</span>
                          </SelectItem>
                        ),
                        has("rocm") && (
                          <SelectItem key="ROCM" value="ROCM">
                            <span className="flex items-center gap-1.5"><Monitor className="w-3 h-3" /> GPU (ROCm)</span>
                          </SelectItem>
                        ),
                        has("intel") && (
                          <SelectItem key="INTEL" value="INTEL">
                            <span className="flex items-center gap-1.5"><Monitor className="w-3 h-3" /> GPU (Intel)</span>
                          </SelectItem>
                        ),
                        has("npu") && (
                          <SelectItem key="NPU" value="NPU">
                            <span className="flex items-center gap-1.5"><Cpu className="w-3 h-3" /> NPU</span>
                          </SelectItem>
                        ),
                      ];
                      return items.filter(Boolean);
                    })()}
                  </SelectContent>
                </Select>
              </div>

              {CREDITS_UI_ENABLED && (
              <div className="space-y-2">
                {/* sz-tcp.5 — tier card replaces the single cost input.
                    All three tiers are required (no defaults are offered
                    once the user starts typing); the medium tier is what
                    still ships as the legacy `cost_in_credits`. */}
                <div className="flex items-center gap-2">
                  <Label className="h-5">Cost per run (credits)</Label>
                  <CostBasisBadge costBasis={effCostBasis} className="ml-2" />
                </div>
                <p className="text-sm text-muted-foreground">
                  Credits charged by input size: Small / Medium / Large. All three are required.
                </p>
                {([
                  ["small", "Small", costTiers.small],
                  ["medium", "Medium", costTiers.medium],
                  ["large", "Large", costTiers.large],
                ] as Array<["small" | "medium" | "large", string, number]>).map(([tier, label, current]) => (
                  <div key={tier} className="space-y-2">
                    <Label className="h-5">{label} tier (credits)</Label>
                    <Input
                      type="number"
                      min={0}
                      required
                      value={current}
                      onChange={(e) => applyTier(tier, parseInt(e.target.value) || 0)}
                    />
                    <p className="text-xs text-muted-foreground">
                      {label}: {tierHints[tier]}
                    </p>
                  </div>
                ))}
                {/* sz-tcp.7 — the author overrode the machine's tiers; offer
                    a way back to them without re-typing three numbers. */}
                {effCostBasis === "manual" && suggestedTiers && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="h-6 text-[11px]"
                    onClick={resetTierEdit}
                  >
                    Reset to suggested ({suggestedTiers.small} / {suggestedTiers.medium} / {suggestedTiers.large})
                  </Button>
                )}
              </div>
              )}

              <div className="space-y-2">
                <Label className="h-5">Timeout (sec)</Label>
                <Input
                  type="number"
                  min={60}
                  value={containerTimeout}
                  onChange={(e) => setContainerTimeout(parseInt(e.target.value) || 7200)}
                />
              </div>
            </div>
          </div>
        )}

        {/* Step 3: Environment Variables */}
        {currentStep === "Variables" && (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Define environment variables your container needs. These become configurable
              parameters when using this node in a workflow.
            </p>

            {envVars.length === 0 && (
              <div className="text-center py-6 text-sm text-gray-400 border border-dashed rounded-lg">
                No environment variables defined.
                <br />
                <span className="text-xs">Click "Add Variable" if your container needs configuration.</span>
              </div>
            )}

            {envVars.map((v, i) => (
              <div key={i} className="flex gap-2 items-start p-3 bg-gray-50 rounded-lg">
                <div className="flex-1 space-y-2">
                  <div className="flex gap-2">
                    <div className="flex-1">
                      <Label className="text-xs">Name</Label>
                      <Input
                        className="font-mono text-xs h-8"
                        placeholder="API_KEY"
                        value={v.name}
                        onChange={(e) => updateEnvVar(i, "name", e.target.value)}
                      />
                    </div>
                    <div className="flex-1">
                      <Label className="text-xs">Default Value</Label>
                      <Input
                        className="text-xs h-8"
                        placeholder={v.required ? "(required — user must supply at runtime)" : "(optional — script has a fallback)"}
                        value={v.default_value}
                        onChange={(e) => updateEnvVar(i, "default_value", e.target.value)}
                      />
                    </div>
                    {/* Required/optional toggle — AI sets the initial
                        value; user can flip it either way. A required
                        var forces the workflow runner to prompt for a
                        value before executing the node. Optional vars
                        fall back to the default_value (or the script's
                        own `os.getenv("X", "fallback")`). */}
                    <div className="w-28">
                      <Label className="text-xs">Requirement</Label>
                      <Select
                        value={v.required ? "required" : "optional"}
                        onValueChange={(val) => updateEnvVar(i, "required", val === "required")}
                      >
                        <SelectTrigger className="h-8 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="required" className="text-xs">
                            <span className="flex items-center gap-1.5">
                              <span className="inline-block w-1.5 h-1.5 rounded-full bg-red-500" />
                              Required
                            </span>
                          </SelectItem>
                          <SelectItem value="optional" className="text-xs">
                            <span className="flex items-center gap-1.5">
                              <span className="inline-block w-1.5 h-1.5 rounded-full bg-gray-400" />
                              Optional
                            </span>
                          </SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                  <div>
                    <Label className="text-xs">Description</Label>
                    <Input
                      className="text-xs h-8"
                      placeholder="What is this variable for?"
                      value={v.description}
                      onChange={(e) => updateEnvVar(i, "description", e.target.value)}
                    />
                  </div>
                </div>
                <button
                  onClick={() => removeEnvVar(i)}
                  className="mt-5 text-gray-400 hover:text-red-500"
                  aria-label="Remove variable"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}

            <Button variant="outline" size="sm" onClick={addEnvVar} className="text-xs">
              <Plus className="w-3 h-3 mr-1" /> Add Variable
            </Button>
          </div>
        )}

        {/* Step 4: Network — per-node egress allowlist + gVisor opt-in.
            Default is deny-all; user opts into preset hosts or adds
            custom entries. Backend enforces via the mod-user-internal
            (Internal=true, zero outbound) vs mod-user-egress bridges. */}
        {currentStep === "Network" && (
          <div className="space-y-4">
            <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
              <p className="font-medium mb-1">Default: Deny all outbound.</p>
              <p className="text-xs">
                This node can only reach MOD's internal storage (MinIO) for
                file I/O. To allow specific external hosts, toggle a preset
                or add a custom entry. A compromised node with no allowlisted
                hosts cannot phone home anywhere.
              </p>
            </div>

            <div>
              <Label className="text-sm font-medium">Common destinations</Label>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {EGRESS_PRESETS.map((preset) => {
                  const enabled = egressHosts.has(preset.host);
                  return (
                    <button
                      key={preset.host}
                      type="button"
                      onClick={() => {
                        const next = new Set(egressHosts);
                        if (enabled) next.delete(preset.host);
                        else next.add(preset.host);
                        setEgressHosts(next);
                      }}
                      className={
                        "text-left rounded-md border p-2 transition-colors " +
                        (enabled
                          ? "border-blue-500 bg-blue-50"
                          : "border-gray-200 hover:border-gray-300")
                      }
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium">{preset.label}</span>
                        {enabled && <Check className="w-4 h-4 text-blue-600" />}
                      </div>
                      <div className="text-xs text-gray-500 truncate">{preset.host}</div>
                      <div className="text-xs text-gray-400 mt-1">{preset.description}</div>
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <Label className="text-sm font-medium">Custom hosts</Label>
              <p className="text-xs text-gray-500 mb-2">
                One hostname per entry, e.g. <code>my-api.company.com</code>.
                Wildcards allowed: <code>*.example.com</code>.
              </p>
              <div className="flex gap-2">
                <Input
                  placeholder="custom.example.com"
                  value={customEgressInput}
                  onChange={(e) => setCustomEgressInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && customEgressInput.trim()) {
                      e.preventDefault();
                      const next = new Set(egressHosts);
                      next.add(customEgressInput.trim());
                      setEgressHosts(next);
                      setCustomEgressInput("");
                    }
                  }}
                />
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!customEgressInput.trim()}
                  onClick={() => {
                    const next = new Set(egressHosts);
                    next.add(customEgressInput.trim());
                    setEgressHosts(next);
                    setCustomEgressInput("");
                  }}
                >
                  <Plus className="w-4 h-4" />
                </Button>
              </div>

              {egressHosts.size > 0 && (
                <div className="mt-3 flex flex-wrap gap-1">
                  {Array.from(egressHosts).map((h) => (
                    <Badge key={h} variant="secondary" className="gap-1">
                      {h}
                      <button
                        onClick={() => {
                          const next = new Set(egressHosts);
                          next.delete(h);
                          setEgressHosts(next);
                        }}
                        className="ml-1 hover:text-red-500"
                        aria-label={`Remove ${h}`}
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-lg border border-gray-200 p-3">
              <div className="flex items-start justify-between">
                <div>
                  <Label htmlFor="use-sandbox" className="text-sm font-medium">
                    Extra-isolation runtime (gVisor)
                  </Label>
                  <p className="text-xs text-gray-500 mt-1 max-w-md">
                    Run this node under the gVisor (runsc) OCI runtime for
                    VM-grade isolation from the host kernel. ~2-3× perf cost.
                    Silently falls back to the default runtime on workers
                    where gVisor isn't installed.
                  </p>
                </div>
                <input
                  id="use-sandbox"
                  type="checkbox"
                  className="mt-1 w-4 h-4"
                  checked={useSandboxRuntime}
                  onChange={(e) => setUseSandboxRuntime(e.target.checked)}
                />
              </div>
            </div>
          </div>
        )}

        {/* Step 5: Review */}
        {currentStep === "Review" && (
          <div className="space-y-3">
            {/* Submission error banner. handleSubmit's catch sets
                generationError when /build-and-register-node returns
                non-2xx (e.g. 409 duplicate name). Without this render
                on Review the user only sees a transient toast and
                doesn't know why the Create Node click went nowhere
                (manual-test-driven fix 2026-05-21). */}
            {generationError && (
              <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-900 flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-red-600" />
                <div className="flex-1 whitespace-pre-wrap break-words">
                  <div className="font-medium">Couldn't start the build</div>
                  <div className="mt-0.5">{generationError}</div>
                </div>
              </div>
            )}
            {/* Provenance (piece 3): this script was seeded from a prior
                node that already built + smoke-passed for a similar task, so
                the user knows it started from a known-good recipe, not a
                cold guess. */}
            {exemplarUsed && (
              <div className="rounded-md border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-900 flex items-start gap-2">
                <Sparkles className="w-4 h-4 mt-0.5 shrink-0 text-indigo-500" />
                <div className="flex-1 min-w-0">
                  {exemplarSource === "pinned" ? (
                    <>
                      <span className="font-medium">Started from a built-in reference recipe.</span>{" "}
                      Seeded from <span className="font-medium">“{exemplarUsed}”</span> — a curated,
                      known-good recipe for this kind of task, adapted to your request.
                    </>
                  ) : (
                    <>
                      <span className="font-medium">Adapted from a working node.</span>{" "}
                      Seeded from <span className="font-medium">“{exemplarUsed}”</span> — a node you
                      already built successfully for a similar task, so this starts from a recipe
                      that's known to work.
                    </>
                  )}
                </div>
              </div>
            )}
            {/* Critic D heads-ups — advisory, non-blocking notes from the
                reasoning review of the generated script (e.g. "may not fully
                match your request: ..."). The script is still buildable; these
                just let the user catch a semantic/feasibility concern before
                committing to a build. */}
            {synthWarnings.length > 0 && (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-amber-600" />
                <div className="flex-1 min-w-0">
                  <div className="font-medium">Heads up before you build</div>
                  <ul className="mt-1 space-y-1 list-disc list-inside">
                    {synthWarnings.map((w, i) => (
                      <li key={i} className="whitespace-pre-wrap break-words">{w}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {/* Same analysis banners that Configure shows. Users who
                clicked "Analyze and jump to Review" land here without
                ever seeing Configure, so without these the analyzer's
                warnings never reach them (issue #13). */}
            {analysisBanners}
            {/* I/O confirmation gate — also rendered here because the
                Image-mode "jump to Review" shortcut skips Configure
                and would otherwise hide the gate. The Create Node
                button is disabled while this banner is visible (see
                canAdvance Review case). Confirm-as-shown button is
                the rubber-stamp path when image/image (or whatever
                defaults are present) are actually correct. */}
            {needsIOConfirmation({
              sourceType,
              schemaConfidence,
              aiFilledFields,
            }) && (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-amber-600" />
                  <div className="flex-1">
                    <div className="font-medium">
                      Confirm the input + output types
                    </div>
                    <div className="mt-0.5">
                      {ioBannerBody(sourceType)}
                    </div>
                  </div>
                </div>
                <div className="mt-2 flex justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-xs h-7"
                    onClick={() => {
                      const next = new Set(aiFilledFields);
                      next.delete("inputType");
                      next.delete("outputType");
                      setAiFilledFields(next);
                    }}
                  >
                    Confirm {inputType} → {outputType} as shown
                  </Button>
                </div>
              </div>
            )}
            <div className="bg-gray-50 rounded-lg p-4 space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">Source</span>
                <span className="font-medium text-right max-w-[65%] truncate">
                  {sourceType === "image" && dockerImage}
                  {sourceType === "dockerfile" && "Uploaded Dockerfile"}
                  {sourceType === "repo" && repoUrl}
                  {sourceType === "script" && (scriptFilename || "Pasted script")}
                </span>
              </div>
              {sourceType === "repo" && repoCredential && repoCredential !== "none" && (
                <div className="flex justify-between">
                  <span className="text-gray-500">Git Credentials</span>
                  <span className="font-medium flex items-center gap-1">
                    <KeyRound className="w-3 h-3" /> {repoCredential}
                  </span>
                </div>
              )}
              {sourceType === "image" && registryCredential && registryCredential !== "none" && (
                <div className="flex justify-between">
                  <span className="text-gray-500">Registry Credentials</span>
                  <span className="font-medium flex items-center gap-1">
                    <KeyRound className="w-3 h-3" /> {registryCredential}
                  </span>
                </div>
              )}
              {/* Node Name is editable inline on Review so users can
                  resolve a duplicate-name 409 without bouncing back
                  to Configure (manual-test-driven fix 2026-05-21).
                  Border turns red while generationError mentions a
                  name conflict — visual hint that this is the field
                  to change. */}
              <div className="flex justify-between items-center gap-2">
                <span className="text-gray-500 shrink-0">Node Name</span>
                <Input
                  value={nodeName}
                  onChange={(e) => {
                    setNodeName(e.target.value);
                    // Typing a fresh name clears a stale duplicate-name
                    // error so the user knows they've addressed the
                    // problem. We re-validate on submit; if the new
                    // name also collides, the banner returns.
                    if (generationError) setGenerationError(null);
                  }}
                  className={`h-7 text-xs font-medium text-right max-w-[60%] ${
                    generationError && /already exists/i.test(generationError)
                      ? "border-red-400 focus:ring-red-400"
                      : ""
                  }`}
                />
              </div>
              {/* Category — LLM-suggested, user-editable. Datalist
                  pre-populates with the tenant's existing categories
                  so users see what's already in their taxonomy and
                  can collapse onto an existing bucket rather than
                  forking (manual-test fix 2026-05-22). Server
                  case-folds on submit so "best"/"BEST"/"Best"
                  collapse to a single canonical form. */}
              <div className="flex justify-between items-center gap-2">
                <span className="text-gray-500 shrink-0 flex items-center gap-1">
                  Category
                  {nodeTypeLoading && (
                    <Loader2 className="w-3 h-3 animate-spin opacity-60" />
                  )}
                  {nodeTypeSuggestion && nodeTypeSuggestion.source !== "fallback" && (
                    <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded">
                      AI
                    </span>
                  )}
                </span>
                <div className="flex flex-col items-end gap-0.5 max-w-[60%] w-[60%]">
                  {/* Combobox: click-to-open dropdown of the tenant's
                      existing categories, plus a "Use '<typed>'" item so
                      users can still create a new category by typing.
                      Replaces the previous Input+datalist whose dropdown
                      affordance was invisible (browser-quirky, broken on
                      mobile). */}
                  <Popover open={nodeTypeOpen} onOpenChange={(open) => {
                    setNodeTypeOpen(open);
                    if (!open) setNodeTypeSearch("");
                  }}>
                    <PopoverTrigger asChild>
                      <Button
                        type="button"
                        variant="outline"
                        role="combobox"
                        aria-expanded={nodeTypeOpen}
                        className="h-7 text-xs font-medium w-full justify-between px-2"
                      >
                        <span className={cn("truncate", !nodeType && "text-gray-400")}>
                          {nodeType || (nodeTypeLoading ? "Suggesting…" : "Pick or type a category")}
                        </span>
                        <ChevronsUpDown className="ml-2 h-3 w-3 shrink-0 opacity-50" />
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-[260px] p-0" align="end">
                      <Command shouldFilter={false}>
                        <CommandInput
                          placeholder="Search or type new…"
                          value={nodeTypeSearch}
                          onValueChange={setNodeTypeSearch}
                          className="h-8 text-xs"
                        />
                        <CommandList>
                          {(() => {
                            const existing = nodeTypeSuggestion?.existing || [];
                            const q = nodeTypeSearch.trim();
                            const qLower = q.toLowerCase();
                            const filtered = q
                              ? existing.filter((c) => c.toLowerCase().includes(qLower))
                              : existing;
                            const exact = existing.some((c) => c.toLowerCase() === qLower);
                            const showCustom = q.length > 0 && !exact;
                            const isEmpty = filtered.length === 0 && !showCustom;
                            return (
                              <>
                                {isEmpty && (
                                  <CommandEmpty className="p-2 text-xs text-gray-500">
                                    No matches — keep typing to add a new one.
                                  </CommandEmpty>
                                )}
                                {showCustom && (
                                  <CommandGroup heading="Add new">
                                    <CommandItem
                                      key={`__custom__${q}`}
                                      value={q}
                                      onSelect={() => {
                                        setNodeType(q);
                                        setNodeTypeOpen(false);
                                        setNodeTypeSearch("");
                                      }}
                                      className="text-xs"
                                    >
                                      <Plus className="mr-2 h-3 w-3 opacity-60" />
                                      Use &quot;{q}&quot;
                                    </CommandItem>
                                  </CommandGroup>
                                )}
                                {filtered.length > 0 && (
                                  <CommandGroup heading={existing.length === filtered.length ? "Existing categories" : `Matches (${filtered.length})`}>
                                    {filtered.map((c) => (
                                      <CommandItem
                                        key={c}
                                        value={c}
                                        onSelect={() => {
                                          setNodeType(c);
                                          setNodeTypeOpen(false);
                                          setNodeTypeSearch("");
                                        }}
                                        className="text-xs"
                                      >
                                        <Check
                                          className={cn(
                                            "mr-2 h-3 w-3",
                                            nodeType === c ? "opacity-100" : "opacity-0",
                                          )}
                                        />
                                        {c}
                                      </CommandItem>
                                    ))}
                                  </CommandGroup>
                                )}
                              </>
                            );
                          })()}
                        </CommandList>
                      </Command>
                    </PopoverContent>
                  </Popover>
                  {nodeTypeSuggestion?.rationale && (
                    <span className="text-[10px] text-gray-400 italic text-right max-w-full truncate"
                          title={nodeTypeSuggestion.rationale}>
                      {nodeTypeSuggestion.rationale}
                    </span>
                  )}
                </div>
              </div>
              {description && (
                <div className="flex justify-between">
                  <span className="text-gray-500">Description</span>
                  <span className="font-medium text-right max-w-[60%] truncate">{description}</span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-gray-500">Input / Output</span>
                <span className="font-medium">{inputType} &rarr; {outputType}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Processing</span>
                <Badge variant="secondary" className="text-xs">{processType}</Badge>
              </div>
              {CREDITS_UI_ENABLED && (
              <div className="flex justify-between">
                <span className="text-gray-500">Cost (S/M/L)</span>
                <span className="font-medium">
                  {costTiers.small} / {costTiers.medium} / {costTiers.large} credits
                </span>
                <CostBasisBadge costBasis={effCostBasis} className="ml-1.5" />
              </div>
              )}
              {envVars.filter(v => v.name).length > 0 && (
                <div className="pt-2 border-t">
                  <span className="text-gray-500 text-xs">Environment Variables</span>
                  <div className="mt-1 space-y-1">
                    {envVars.filter(v => v.name).map((v, i) => (
                      <div key={i} className="flex justify-between text-xs">
                        <code className="bg-gray-200 px-1 rounded">{v.name}</code>
                        <span className="text-gray-400">{v.default_value || "(no default)"}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Variant picker — shows the GPU backends this source
                will build images for. Uncheck any you don't need so
                the adapter + build pipeline skips them. Populated by
                /predict-variants (no LLM, no credits) when you land on
                this step. */}
            <div className="border rounded-lg bg-white p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold text-gray-800">
                    GPU variants to build
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    One image per checked backend. Uncheck any you
                    don&apos;t want — we won&apos;t call the adapter
                    or push a tag for those.
                  </div>
                </div>
                {predictedVariantsLoading && (
                  <Loader2 className="w-4 h-4 animate-spin text-gray-400" />
                )}
              </div>
              {predictedVariants.length === 0 && !predictedVariantsLoading && (
                <div className="text-xs text-gray-500 italic">
                  No GPU framework imports detected — this node will
                  ship as a single CPU image (no per-variant tags).
                </div>
              )}
              {predictedVariants.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {predictedVariants.map((v) => {
                    const skipped = reviewSkippedVariants.has(v);
                    const reason = predictedIncompatibleReasons[v];
                    const tripped = predictedCircuitBroken.includes(v);
                    // P9f.1 — three-state compatibility badge:
                    //   circuit_broken  → red, force-skipped (checkbox disabled)
                    //   incompatible    → amber, user can still tick (with warning)
                    //   compatible      → normal indigo/slate toggle
                    const state: "circuit_broken" | "incompatible" | "ok" =
                      tripped ? "circuit_broken"
                        : reason ? "incompatible"
                          : "ok";
                    const forced = state === "circuit_broken";
                    let tileClass: string;
                    if (forced) {
                      tileClass =
                        "bg-red-50 border-red-300 text-red-800 cursor-not-allowed line-through";
                    } else if (state === "incompatible") {
                      tileClass = skipped
                        ? "bg-slate-100 border-slate-300 text-slate-500 line-through"
                        : "bg-amber-50 border-amber-300 text-amber-800";
                    } else {
                      tileClass = skipped
                        ? "bg-slate-100 border-slate-300 text-slate-500 line-through"
                        : "bg-indigo-50 border-indigo-300 text-indigo-800";
                    }
                    const tooltip = forced
                      ? `auto-disabled: ${reason || "variant circuit breaker tripped"}`
                      : state === "incompatible"
                        ? `heads up: ${reason}`
                        : undefined;
                    const badgeLabel = forced ? "auto-disabled"
                      : state === "incompatible" ? "heads up"
                        : undefined;
                    const badgeClass = forced
                      ? "bg-red-200 text-red-900"
                      : "bg-amber-200 text-amber-900";
                    return (
                      <label
                        key={v}
                        title={tooltip}
                        className={[
                          "flex items-center gap-2 text-xs px-3 py-1.5 rounded-full border select-none",
                          forced ? "" : "cursor-pointer",
                          tileClass,
                        ].join(" ")}
                      >
                        <input
                          type="checkbox"
                          className="accent-indigo-600"
                          checked={!skipped && !forced}
                          disabled={forced}
                          onChange={() => {
                            if (forced) return;
                            const next = new Set(reviewSkippedVariants);
                            if (next.has(v)) next.delete(v);
                            else next.add(v);
                            setReviewSkippedVariants(next);
                          }}
                        />
                        <span className="uppercase font-semibold tracking-wide">{v}</span>
                        {badgeLabel && (
                          <span
                            className={`text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded ${badgeClass}`}
                          >
                            {badgeLabel}
                          </span>
                        )}
                      </label>
                    );
                  })}
                </div>
              )}
              {predictedCircuitBroken.length > 0 && (
                <div className="text-[11px] text-red-800 bg-red-50 border border-red-200 rounded px-2 py-1.5">
                  <span className="font-medium">Auto-disabled by the platform:</span>{" "}
                  <span className="font-mono">{predictedCircuitBroken.join(", ")}</span>
                  {" "}— ports for these variants have been failing; a platform
                  operator has been notified. New builds for these will be skipped.
                </div>
              )}
              {Object.keys(predictedIncompatibleReasons).filter(v =>
                !predictedCircuitBroken.includes(v)).length > 0 && (
                <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1.5 space-y-1">
                  <div className="font-medium">Compatibility notes:</div>
                  {Object.entries(predictedIncompatibleReasons)
                    .filter(([v]) => !predictedCircuitBroken.includes(v))
                    .map(([v, reason]) => (
                      <div key={v}>
                        <span className="font-mono uppercase">{v}</span>: {reason}
                      </div>
                    ))}
                </div>
              )}
              {reviewSkippedVariants.size > 0 && (
                <div className="text-[11px] text-gray-500">
                  Skipping {reviewSkippedVariants.size}:{" "}
                  {Array.from(reviewSkippedVariants).join(", ")}
                </div>
              )}
              {predictedUnmatched.length > 0 && (
                <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
                  <span className="font-medium">Admin-enabled but no worker:</span>{" "}
                  <span className="font-mono">{predictedUnmatched.join(", ")}</span>
                  {" "}— add a worker with these hardware tags or tasks
                  for this variant will sit unrunnable.
                </div>
              )}
            </div>

            {sourceType === "image" ? (
              <p className="text-xs text-muted-foreground">
                We'll build a thin adapter wrapping{" "}
                <code className="bg-gray-100 px-1 rounded">{dockerImage}</code>{" "}
                — the Dockerfile is{" "}
                <code className="bg-gray-100 px-1 rounded">FROM {dockerImage}</code>{" "}
                plus the MOD I/O entrypoint. No LLM rewrite of your image. The
                build queue row will show progress for each accelerator variant.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                The system will generate the necessary files, build the Docker image,
                push it to the registry, and register the node. You'll be able to review
                and edit generated files if anything needs adjustment.
              </p>
            )}
          </div>
        )}

        {/* Step 5: Preview (image flow only) — LLM-generated Albedo-pattern artifacts */}
        {currentStep === "Preview" && (
          <div className="space-y-4">
            {/* γ — deferred-registration review panel. Renders on
                top of the normal Preview body when the user
                submitted with a sample. Three states:
                  1. Polling — build still running, show spinner.
                  2. Sample-test failed — show the captured stderr +
                     a hint to iterate.
                  3. Sample-test passed — show stdout + output files
                     and the thumbs-up / thumbs-down buttons. */}
            {deferredBuildTaskId && !deferredRegistration && !sampleTestFailure && (
              <div className="rounded-md border border-indigo-200 bg-indigo-50 px-3 py-3 flex items-start gap-3">
                <Loader2 className="w-5 h-5 animate-spin text-indigo-600 shrink-0 mt-0.5" />
                <div>
                  <div className="text-sm font-semibold text-indigo-900">
                    Building + running your sample…
                  </div>
                  <div className="text-xs text-indigo-700 mt-0.5">
                    We&apos;ll show the captured output here as soon as
                    the build finishes. Hardware-accelerated builds
                    can take a few minutes.
                  </div>
                </div>
              </div>
            )}
            {sampleTestFailure && (
              <div className="rounded-md border border-rose-300 bg-rose-50 px-3 py-3 space-y-2">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
                  <div className="space-y-1 flex-1">
                    <div className="text-sm font-semibold text-rose-900">
                      Sample input failed
                    </div>
                    <div className="text-xs text-rose-800">
                      Image built and pushed, but running your sample
                      didn&apos;t produce a clean result. Adjust the script
                      or schema and rebuild — the existing image stays
                      in the registry until you re-register.
                    </div>
                  </div>
                </div>
                {sampleTestFailure.sample_test_result?.stderr_tail && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] font-medium text-rose-900">stderr (last lines)</span>
                      <CopyLogButton
                        getText={() => sampleTestFailure.sample_test_result.stderr_tail}
                        title="Copy the sample stderr to the clipboard"
                      />
                    </div>
                    <pre className="bg-rose-100 border border-rose-200 rounded-sm p-2 text-[11px] font-mono whitespace-pre-wrap break-all max-h-40 overflow-auto">
                      {sampleTestFailure.sample_test_result.stderr_tail}
                    </pre>
                  </div>
                )}
                <div className="flex justify-end">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-[11px]"
                    onClick={() => {
                      setSampleTestFailure(null);
                      setDeferredBuildTaskId(null);
                      setCurrentStep("Configure");
                    }}
                  >
                    Back to Configure
                  </Button>
                </div>
              </div>
            )}
            {deferredRegistration && !deferredRegistration.finalized_at && (
              <div className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-3 space-y-3">
                <div className="flex items-start gap-2">
                  <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
                  <div className="flex-1">
                    <div className="text-sm font-semibold text-emerald-900">
                      Sample ran successfully — review the output
                    </div>
                    <div className="text-xs text-emerald-800 mt-0.5">
                      The image processed{" "}
                      <span className="font-mono">
                        {deferredRegistration.sample_test_result?.input_filename || sampleInputFilename}
                      </span>
                      {". "}If the output looks right, register the node.
                      If not, adjust + rebuild — the image stays in
                      the registry either way.
                    </div>
                  </div>
                </div>
                {deferredRegistration.sample_test_result?.stdout_tail && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] font-medium text-emerald-900">
                        stdout (last lines)
                      </span>
                      <CopyLogButton
                        getText={() => deferredRegistration.sample_test_result.stdout_tail}
                        title="Copy the sample stdout to the clipboard"
                      />
                    </div>
                    <pre className="bg-white border border-emerald-200 rounded-sm p-2 text-[11px] font-mono whitespace-pre-wrap break-all max-h-40 overflow-auto">
                      {deferredRegistration.sample_test_result.stdout_tail}
                    </pre>
                  </div>
                )}
                {Array.isArray(deferredRegistration.sample_test_result?.output_files) &&
                  deferredRegistration.sample_test_result.output_files.length > 0 && (
                  <div>
                    <div className="text-[11px] font-medium text-emerald-900 mb-1">
                      /data/output/ ({deferredRegistration.sample_test_result.output_files.length} file
                      {deferredRegistration.sample_test_result.output_files.length === 1 ? "" : "s"})
                    </div>
                    <div className="space-y-2">
                      {deferredRegistration.sample_test_result.output_files.map((f: any, idx: number) => (
                        <div key={idx} className="bg-white border border-emerald-200 rounded-sm">
                          <div className="px-2 py-1 border-b border-emerald-100 flex items-center justify-between text-[11px]">
                            <span className="font-mono">{f.name}</span>
                            <span className="flex items-center gap-2">
                              <span className="text-muted-foreground">
                                {f.kind} · {f.size_bytes} bytes
                              </span>
                              {f.kind === "text" && f.preview && (
                                <CopyLogButton
                                  getText={() => String(f.preview)}
                                  title="Copy this output file preview to the clipboard"
                                />
                              )}
                            </span>
                          </div>
                          {f.kind === "text" ? (
                            <pre className="p-2 text-[11px] font-mono whitespace-pre-wrap break-all max-h-40 overflow-auto">
                              {f.preview}
                            </pre>
                          ) : (
                            <div className="px-2 py-2 text-[11px] text-muted-foreground italic">
                              {f.preview}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {deferredRegistration.sample_test_result?.output_files?.length === 0 &&
                 !deferredRegistration.sample_test_result?.stdout_tail && (
                  <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded-sm px-2 py-1.5">
                    The image ran cleanly but produced no stdout and
                    no /data/output/ files. If your node is supposed
                    to write output, double-check it before registering.
                  </div>
                )}
                {!showDeclineForm ? (
                  <div className="flex flex-wrap gap-2 pt-1">
                    <Button
                      type="button"
                      size="sm"
                      className="h-7 text-[11px] bg-emerald-600 hover:bg-emerald-700 text-white"
                      onClick={handleApproveDeferred}
                      disabled={finalizing}
                    >
                      {finalizing ? (
                        <Loader2 className="w-3 h-3 animate-spin mr-1" />
                      ) : null}
                      Looks good — register node
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 text-[11px]"
                      onClick={() => setShowDeclineForm(true)}
                      disabled={finalizing}
                    >
                      Reject
                    </Button>
                  </div>
                ) : (
                  <div className="space-y-2 pt-1">
                    <Label className="text-[11px]">
                      Why? (optional, helps us learn)
                    </Label>
                    <Textarea
                      value={declineReason}
                      onChange={(e) => setDeclineReason(e.target.value)}
                      placeholder="e.g. output truncated; should have included confidence scores; …"
                      className="text-xs min-h-[60px]"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="destructive"
                        className="h-7 text-[11px]"
                        onClick={handleDeclineDeferred}
                        disabled={finalizing}
                      >
                        {finalizing ? (
                          <Loader2 className="w-3 h-3 animate-spin mr-1" />
                        ) : null}
                        Confirm reject
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 text-[11px]"
                        onClick={() => {
                          setShowDeclineForm(false);
                          setDeclineReason("");
                        }}
                        disabled={finalizing}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
            {/* Resume-hydration skeleton (issue #7). When the user
                clicked Resume on a Phase 1 done row, the wizard jumps
                here and fetches /generation-artifacts. Without a
                proper loading state, the user saw an empty Preview
                shell that looked indistinguishable from a broken
                wizard. The skeleton renders placeholder artifact
                rows + a clear "Loading generated artifacts…" header
                until the fetch resolves. */}
            {isResumeHydrating ? (
              <div className="space-y-3">
                <div className="flex items-center gap-3 p-4 bg-indigo-50 border border-indigo-200 rounded-lg">
                  <Loader2 className="w-5 h-5 animate-spin text-indigo-600 shrink-0" />
                  <div className="text-left">
                    <div className="text-sm font-semibold text-indigo-900">
                      Loading generated artifacts…
                    </div>
                    <div className="text-xs text-indigo-700 mt-0.5">
                      Restoring your build from the server. This usually takes a few seconds.
                    </div>
                  </div>
                </div>
                {[0, 1, 2, 3, 4].map((i) => (
                  <div
                    key={i}
                    className="border rounded-md p-3 animate-pulse bg-white"
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <div className="w-3 h-3 rounded bg-slate-200" />
                      <div className="h-4 w-32 bg-slate-200 rounded" />
                      <div className="h-3 w-16 bg-slate-200 rounded ml-auto" />
                    </div>
                    <div className="h-2 w-full bg-slate-100 rounded mb-1" />
                    <div className="h-2 w-3/4 bg-slate-100 rounded" />
                  </div>
                ))}
              </div>
            ) : (
              <>
            {isGenerating && (
              <div className="flex items-center gap-2 p-4 bg-indigo-50 border border-indigo-200 rounded-lg text-sm text-indigo-700">
                <Loader2 className="w-4 h-4 animate-spin" />
                {generatedStages.length > 0
                  ? "Running codebase pipeline… each stage streams below as it completes."
                  : "Generating artifacts via LLM…"}
              </div>
            )}

            {/* Per-variant preview is now inside the Variants tab of
                the tabbed output viewer below, so a user can jump
                between Dockerfile / entrypoint / build-push / DAG /
                Variants without scrolling past a giant grid. */}

            {/* Live stage timeline for the multi-stage pipeline. Legacy
                single-call albedo tasks emit no stages so this block
                stays hidden for them. */}
            {generatedStages.length > 0 && (
              <div className="p-4 bg-white border border-gray-200 rounded-lg">
                <div className="text-xs font-semibold text-gray-600 mb-2 uppercase tracking-wide">
                  Pipeline progress
                </div>
                <ul className="space-y-1.5 text-xs">
                  {generatedStages.map((ev, i) => {
                    const icon = ev.status === "ok" ? "✓"
                      : ev.status === "error" ? "✕"
                      : ev.status === "warn" ? "⚠"
                      : ev.status === "skipped" ? "⊘"
                      : "⋯";
                    const color = ev.status === "ok" ? "text-green-700"
                      : ev.status === "error" ? "text-red-700"
                      : ev.status === "warn" ? "text-amber-700"
                      : ev.status === "skipped" ? "text-gray-400"
                      : "text-indigo-700";
                    return (
                      <li key={i} className={`flex gap-2 ${color}`}>
                        <span className="w-4 flex-none text-center font-mono">{icon}</span>
                        <span className="flex-none font-medium w-32 truncate">{ev.stage}</span>
                        <span className="text-gray-600">{ev.detail || ""}</span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            {generationError && (
              <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-xs text-red-900 space-y-2">
                <div className="font-semibold text-red-800">Artifact generation failed</div>
                {generationError.split("\n\n").map((chunk, i) => (
                  <div
                    key={i}
                    className={i === 1 ? "font-mono whitespace-pre-wrap break-words bg-red-100 p-2 rounded border border-red-200" : "whitespace-pre-wrap"}
                  >
                    {chunk}
                  </div>
                ))}
              </div>
            )}
            {!isGenerating && !generationError && generatedDockerfile && (
              <p className="text-xs text-muted-foreground">
                Review the three files the LLM generated. Edit any of them inline before
                clicking <strong>Confirm & Build</strong>. On confirm, the image will be
                built and pushed to the local registry, and the node will be registered.
                {generatedImageName && (
                  <> Final image: <code className="bg-gray-100 px-1 rounded">{generatedImageName}:latest</code></>
                )}
              </p>
            )}

            {/* Glue artifact (issue #18). When the analyzer detected
                non-standard I/O conventions and proposed a glue
                wrapper (entrypoint.sh / Dockerfile.glue / glue.sh /
                Containerfile.glue depending on source type), it
                surfaces here as an editable panel above the regular
                artifact rows. The user can edit the contents inline
                or click Remove to drop it from the dispatch (the
                build proceeds with the original non-glued
                artifacts). The dispatch path picks up
                glueArtifact via the same mechanism the regular
                artifacts use. */}
            {glueArtifact && !isResumeHydrating && (
              <div className="rounded-lg border-2 border-indigo-300 bg-indigo-50/40 p-3 space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Sparkles className="w-3.5 h-3.5 text-indigo-700" />
                    <span className="text-xs font-semibold text-indigo-900">
                      {glueArtifact.artifact_type}
                    </span>
                    <span className="text-[10px] uppercase tracking-wide text-indigo-700 bg-indigo-100 px-1.5 py-0.5 rounded">
                      ai-filled · glue
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setGlueArtifact(null)}
                    className="text-[11px] text-indigo-700 hover:text-red-700 underline-offset-2 hover:underline"
                    title="Remove the glue from this build. The image will dispatch with the original (non-glued) artifacts."
                  >
                    Remove
                  </button>
                </div>
                {glueArtifact.description && (
                  <p className="text-[11px] text-indigo-900/80">
                    {glueArtifact.description}
                  </p>
                )}
                <Textarea
                  value={glueArtifact.content}
                  onChange={(e) =>
                    setGlueArtifact((prev) =>
                      prev ? { ...prev, content: e.target.value } : prev,
                    )
                  }
                  className="font-mono text-[11px] min-h-[140px] max-h-[280px] bg-white"
                  spellCheck={false}
                />
              </div>
            )}

            {/* Per-variant Preview.
                One tab PER BACKEND (CUDA, ROCm, Intel, NPU, CPU, …),
                named for the backend. The hero's own variant comes
                first (named for whatever FROM the LLM picked — e.g.
                CUDA for nvidia/cuda). After the variant tabs come
                the shared artifacts (entrypoint.sh, build-and-push,
                DAG metadata) that don't vary per backend.
                Two progress bars:
                  - Top: OVERALL — done variants / total variants.
                  - Inside each variant tab: sub-task progress for
                    that one variant (Dockerfile rendered → each
                    source file adapted → ready).
                Adapter runs sequentially (variant_concurrency=1 in
                Containerizer) so variants complete in order and the
                top bar ticks one variant at a time. */}
            {(() => {
              // Build the variant-tab list from the user's Review-step
              // selection so tabs appear immediately — even while the
              // Containerizer is still running. As data lands in
              // generatedDockerfileVariants / generatedVariantAdaptations
              // the pending tabs fill in with content. Previously the
              // list was derived solely from the Containerizer's final
              // response, so the user saw ONLY the hero tab during
              // the ~30-60s generation window.
              //
              // Selection = predicted - skipped. Fall back to whatever
              // the Containerizer returned if the user came to Preview
              // via reload or direct navigation (state empty).
              const heroDockerReady = !!generatedDockerfile;
              const expectedFromReview = predictedVariants.filter(
                (v) => !reviewSkippedVariants.has(v),
              );
              const fromResponse = Object.keys(generatedDockerfileVariants || {});

              // Hero: best guess for its backend label. Precedence:
              //   1. generatedHeroVariant — authoritative, from the
              //      Containerizer's response (infer_hero_variant
              //      on the actual rendered FROM line).
              //   2. predictedHero — from /predict-variants, derived
              //      from the source_image the user picked. Avoids
              //      the earlier bug where the whitelist's alphabetical
              //      order put Intel first and the tab labeled itself
              //      'intel' for a CUDA-based hero.
              //   3. Review selection / response fallback.
              //   4. "hero" placeholder (empty-everything case).
              const heroLabel = (
                generatedHeroVariant ||
                predictedHero ||
                expectedFromReview[0] ||
                fromResponse[0] ||
                "hero"
              ).toLowerCase();

              // Extra variants = every variant we expect to build
              // minus the hero. Union of Review-selected and what
              // the Containerizer has actually emitted (so late-
              // arriving variants from the response show up even
              // if the user never went through Review, e.g. on
              // page reload).
              const extraSet = new Set<string>();
              for (const v of expectedFromReview) {
                if (v.toLowerCase() !== heroLabel) extraSet.add(v);
              }
              for (const v of fromResponse) {
                if (v.toLowerCase() !== heroLabel) extraSet.add(v);
              }
              const extraVariants = Array.from(extraSet);

              // Unified list [{key, label, isHero}] used by both
              // the tab bar and the overall progress computation.
              const allVariants: Array<{ key: string; label: string; isHero: boolean }> = [
                { key: heroLabel, label: heroLabel.toUpperCase(), isHero: true },
                ...extraVariants.map((v) => ({
                  key: v, label: v.toUpperCase(), isHero: false,
                })),
              ];

              const variantAdaptMap = generatedVariantAdaptations || {};
              const sourceVarMap = generatedSourceVariants || {};
              const variantDockerMap = generatedDockerfileVariants || {};

              // Per-variant readiness + sub-task tally.
              //   Hero readiness = the main Dockerfile is in.
              //   Others readiness = they have an adaptation entry
              //     (= adapter finished its per-file pass on this variant).
              //   Sub-tasks = one per source file + one for the
              //     Dockerfile itself.
              function variantStatus(v: { key: string; isHero: boolean }) {
                const heroFileCount = generatedSourceFiles.length || 1;
                if (v.isHero) {
                  const done =
                    (heroDockerReady ? 1 : 0) +
                    (heroDockerReady ? heroFileCount : 0);  // hero sources == submitted inputs
                  const total = 1 + heroFileCount;
                  return { ready: heroDockerReady, done, total };
                }
                const dockerReady = !!variantDockerMap[v.key];
                const perFile = variantAdaptMap[v.key]?.files || {};
                const adaptedCount = Object.values(perFile).filter(
                  (f: any) => f && !f.incompatible,
                ).length;
                const total = 1 + heroFileCount;  // Dockerfile + files
                const done = (dockerReady ? 1 : 0) + adaptedCount;
                return { ready: !!variantAdaptMap[v.key], done, total };
              }

              const overallDone = allVariants.filter(
                (v) => variantStatus(v).ready,
              ).length;
              const overallTotal = allVariants.length;
              const overallPct = overallTotal > 0
                ? Math.round((overallDone / overallTotal) * 100)
                : 0;

              const sharedArtifacts = [
                { key: "entrypoint", label: "entrypoint.sh", ready: !!generatedEntrypoint },
                { key: "buildpush", label: "build-and-push.sh", ready: !!generatedBuildPush },
                { key: "dag", label: "DAG metadata", ready: !!livePreviewDag || !!generatedMetadataFile },
              ];

              const defaultTab = allVariants[0]?.key || "entrypoint";

              return (
                <>
                {isGenerating && (
                  <div className="p-2.5 bg-indigo-50 border border-indigo-200 rounded-lg text-[11px] text-indigo-900 flex items-center gap-2">
                    <Loader2 className="w-3.5 h-3.5 animate-spin flex-shrink-0" />
                    <span>
                      <strong>Step 1 of 2 — generating files.</strong>{" "}
                      You can close this page and come back later — hit <em>Close &amp; notify me</em> below and we'll send a browser notification when Step 2 is ready to launch.
                    </span>
                  </div>
                )}
                {/* Bar 1 — OVERALL variant progress. Each variant is
                    one checkpoint. Sequential adapter means the bar
                    ticks one backend at a time. */}
                <div className="p-3 bg-white border border-gray-200 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-xs font-semibold uppercase tracking-wide text-gray-600">
                      Overall progress — variants
                    </div>
                    <div className="text-xs text-gray-600">
                      <span className="font-mono">{overallDone}</span>
                      <span className="text-gray-400"> / {overallTotal} done</span>
                    </div>
                  </div>
                  <div className="w-full bg-slate-100 rounded-full h-2 overflow-hidden mb-2">
                    <div
                      className={
                        overallPct === 100
                          ? "bg-green-500 h-full transition-all duration-500"
                          : "bg-indigo-500 h-full transition-all duration-500"
                      }
                      style={{ width: `${overallPct}%` }}
                    />
                  </div>
                  <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
                    {allVariants.map((v) => {
                      const s = variantStatus(v);
                      return (
                        <div
                          key={v.key}
                          className={[
                            "flex items-center gap-1",
                            s.ready
                              ? "text-green-700"
                              : isGenerating
                                ? "text-indigo-600"
                                : "text-gray-400",
                          ].join(" ")}
                        >
                          {s.ready ? (
                            <Check className="w-3 h-3" />
                          ) : isGenerating ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <span className="w-3 h-3 rounded-full border border-gray-300" />
                          )}
                          <span className="uppercase font-semibold tracking-wide">{v.label}</span>
                          {v.isHero && (
                            <Badge variant="outline" className="text-[9px] py-0 h-4">hero</Badge>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Vertical dropdown stack — one row per variant + one
                    per shared artifact. Each row's collapsed header
                    shows the file/variant name, line count, variant tag,
                    and an ai-filled badge so the user can scan the
                    whole artifact set at a glance without opening
                    anything. Single-accordion: only one row open at a
                    time (replaces the old horizontal tab strip that
                    overflowed once we added 7+ variants). */}
                <div className="space-y-1.5 w-full">
                  {allVariants.map((v) => {
                    const s = variantStatus(v);
                    const variantPct = s.total > 0
                      ? Math.round((s.done / s.total) * 100)
                      : 0;
                    const dockerBody = v.isHero
                      ? generatedDockerfile
                      : (variantDockerMap[v.key] || "");
                    const sources = v.isHero
                      ? Object.fromEntries(
                          generatedSourceFiles.map((f) => [f.filename, f.body]),
                        )
                      : (sourceVarMap[v.key] || {});
                    const adaptMeta = v.isHero
                      ? null
                      : (variantAdaptMap[v.key] || null);
                    const lineCount = dockerBody ? dockerBody.split("\n").length : 0;
                    const variantTag = v.isHero ? "canonical" : v.key;
                    const open = previewExpandedKey === v.key;
                    return (
                      <div key={v.key}>
                        <button
                          type="button"
                          onClick={() =>
                            setPreviewExpandedKey((prev) => (prev === v.key ? null : v.key))
                          }
                          className={
                            "flex items-center gap-2 w-full px-3 py-2 rounded-md border " +
                            (open
                              ? "bg-indigo-50 border-indigo-200 text-indigo-900"
                              : "bg-white border-gray-200 hover:bg-gray-50 text-gray-800")
                          }
                        >
                          {open ? (
                            <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 flex-shrink-0" />
                          )}
                          <span className="text-xs font-semibold">
                            Dockerfile{v.isHero ? "" : `.${v.key}`}
                          </span>
                          <span className="text-[10px] text-gray-500">
                            · {lineCount} line{lineCount === 1 ? "" : "s"}
                          </span>
                          <span className="text-[10px] uppercase tracking-wide text-indigo-600 font-semibold">
                            · {variantTag}
                          </span>
                          {s.ready && (
                            <Badge variant="outline" className="ml-1 text-[9px] py-0 h-4">
                              ai-filled
                            </Badge>
                          )}
                          {!s.ready && isGenerating && (
                            <span className="ml-1 text-[10px] text-indigo-600">
                              {s.done}/{s.total} steps
                            </span>
                          )}
                          {!s.ready && !isGenerating && (
                            <span className="ml-1 text-[10px] text-gray-400">pending…</span>
                          )}
                        </button>
                        {open && (
                          <div className="mt-2 mb-3 space-y-3 px-1">
                        {/* Bar 2 — per-variant sub-task progress. */}
                        <div className="p-3 bg-slate-50 border border-slate-200 rounded">
                          <div className="flex items-center justify-between mb-1.5">
                            <div className="text-xs text-gray-600">
                              {v.label} build
                              {v.isHero && (
                                <span className="text-gray-400"> · hero backend</span>
                              )}
                            </div>
                            <div className="text-xs font-mono text-gray-600">
                              {s.done} / {s.total} steps
                            </div>
                          </div>
                          <div className="w-full bg-white rounded-full h-1.5 overflow-hidden">
                            <div
                              className={
                                s.ready
                                  ? "bg-green-500 h-full transition-all duration-500"
                                  : "bg-indigo-500 h-full transition-all duration-500"
                              }
                              style={{ width: `${variantPct}%` }}
                            />
                          </div>
                          {adaptMeta?.incompatible && (
                            <div className="mt-2 text-[11px] text-rose-700">
                              ⊘ Incompatible: {adaptMeta.incompatible_reason || "not supported for this backend"}
                            </div>
                          )}
                        </div>

                        {/* P4 — Manual escape hatch banner. Surfaces
                            on incompatible non-hero variants: the
                            Dockerfile + source textareas below are
                            editable so the operator can hand-port the
                            blocker the recipe couldn't auto-handle.
                            Confirm & Build then ships the operator's
                            edits as part of the build payload. */}
                        {!v.isHero && adaptMeta?.incompatible && (
                          <div className="text-[11px] bg-amber-50 border border-amber-200 rounded px-3 py-2 text-amber-900">
                            <strong>Manual port required.</strong> The
                            auto-port recipe couldn't safely transform
                            this variant. The Dockerfile and source
                            below are <em>editable</em> — apply the
                            backend-specific changes the recipe
                            couldn't, then click <em>Build this
                            variant</em>. See{" "}
                            <code className="text-[10px]">
                              docs/MULTIVENDOR_ROADMAP.md
                            </code>{" "}
                            for backend-specific patterns.
                          </div>
                        )}

                        {/* This variant's Dockerfile */}
                        <div className="space-y-2">
                          <Label className="flex items-center gap-2">
                            <span>Dockerfile.{v.key}</span>
                            {!dockerBody && isGenerating && (
                              <Loader2 className="w-3 h-3 animate-spin text-gray-400" />
                            )}
                          </Label>
                          <Textarea
                            className="font-mono text-[11px] min-h-[200px]"
                            value={dockerBody}
                            onChange={
                              v.isHero
                                ? (e) => setGeneratedDockerfile(e.target.value)
                                : (e) => {
                                    // P4 — let the operator edit non-
                                    // hero Dockerfiles; the wizard
                                    // ships them as-is on Confirm &
                                    // Build via the dockerfile_variants
                                    // payload field.
                                    setGeneratedDockerfileVariants((prev) => ({
                                      ...prev,
                                      [v.key]: e.target.value,
                                    }));
                                  }
                            }
                            placeholder={isGenerating ? "Waiting for Containerizer…" : ""}
                          />
                        </div>

                        {/* This variant's source files. Hero sources
                            are what the user submitted; other variants
                            show the adapter's output. */}
                        {Object.keys(sources).length > 0 && (
                          <div className="space-y-2">
                            <Label className="text-xs text-gray-600">
                              Source files ({Object.keys(sources).length})
                            </Label>
                            <Tabs defaultValue={Object.keys(sources)[0]}>
                              <TabsList className="h-auto flex-wrap">
                                {Object.keys(sources).map((fn) => (
                                  <TabsTrigger
                                    key={fn}
                                    value={fn}
                                    className="text-[10px]"
                                  >
                                    {fn}
                                  </TabsTrigger>
                                ))}
                              </TabsList>
                              {Object.entries(sources).map(([fn, body]) => {
                                const fileMeta = adaptMeta?.files?.[fn];
                                const changeCount = fileMeta?.changes?.length || 0;
                                return (
                                  <TabsContent key={fn} value={fn} className="space-y-1">
                                    {!v.isHero && changeCount > 0 && (
                                      <div className="text-[11px] text-emerald-700">
                                        {changeCount} adapter change{changeCount === 1 ? "" : "s"}
                                        {fileMeta?.warnings?.length > 0 && (
                                          <span className="text-amber-700">
                                            {" · "}{fileMeta.warnings.length} warning
                                            {fileMeta.warnings.length === 1 ? "" : "s"}
                                          </span>
                                        )}
                                      </div>
                                    )}
                                    <Textarea
                                      className="font-mono text-[11px] min-h-[220px] bg-gray-50"
                                      value={body as string}
                                      readOnly={v.isHero}
                                      onChange={
                                        v.isHero
                                          ? undefined
                                          : (e) => {
                                              // P4 — non-hero source
                                              // edits land in
                                              // generatedSourceVariants
                                              // and ship via the
                                              // source_variants payload
                                              // field on Confirm.
                                              setGeneratedSourceVariants((prev) => ({
                                                ...prev,
                                                [v.key]: {
                                                  ...(prev[v.key] || {}),
                                                  [fn]: e.target.value,
                                                },
                                              }));
                                            }
                                      }
                                    />
                                  </TabsContent>
                                );
                              })}
                            </Tabs>
                          </div>
                        )}

                        {/* Skip toggle (non-hero variants only — the
                            hero always ships). */}
                        {!v.isHero && (
                          <div className="flex items-center gap-2">
                            <input
                              type="checkbox"
                              className="accent-indigo-600"
                              checked={!skippedVariants.has(v.key)}
                              onChange={() => {
                                const next = new Set(skippedVariants);
                                if (next.has(v.key)) next.delete(v.key);
                                else next.add(v.key);
                                setSkippedVariants(next);
                              }}
                            />
                            <span className="text-xs text-gray-700">
                              Build this variant
                            </span>
                            {skippedVariants.has(v.key) && (
                              <span className="text-[11px] text-gray-500">
                                (skipped — won&apos;t be pushed)
                              </span>
                            )}
                          </div>
                        )}
                        {/* Rebuild-with-edits: non-hero variants, once the
                            build is live (taskId) and the operator has edited
                            this variant's source. Re-dispatches just this
                            variant's build with the edited source. */}
                        {!v.isHero && taskId && (
                          <div className="flex items-center gap-2 pt-1">
                            <button
                              type="button"
                              disabled={!isVariantDirty(v.key) || rebuildingVariant !== null}
                              onClick={() => rebuildVariantWithEdits(v.key)}
                              className={
                                "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-semibold border " +
                                (isVariantDirty(v.key) && rebuildingVariant === null
                                  ? "bg-indigo-600 border-indigo-600 text-white hover:bg-indigo-700"
                                  : "bg-gray-50 border-gray-200 text-gray-400 cursor-not-allowed")
                              }
                            >
                              {rebuildingVariant === v.key && (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              )}
                              Rebuild {v.key.toUpperCase()} with my changes
                            </button>
                            {isVariantDirty(v.key) && rebuildingVariant === null && (
                              <span className="text-[11px] text-amber-700">edited — not yet built</span>
                            )}
                          </div>
                        )}
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {/* Shared artifact: entrypoint.sh */}
                  {(() => {
                    const k = "entrypoint";
                    const open = previewExpandedKey === k;
                    const lineCount = generatedEntrypoint
                      ? generatedEntrypoint.split("\n").length
                      : 0;
                    return (
                      <div key={k}>
                        <button
                          type="button"
                          onClick={() => setPreviewExpandedKey((prev) => (prev === k ? null : k))}
                          className={
                            "flex items-center gap-2 w-full px-3 py-2 rounded-md border " +
                            (open
                              ? "bg-indigo-50 border-indigo-200 text-indigo-900"
                              : "bg-white border-gray-200 hover:bg-gray-50 text-gray-800")
                          }
                        >
                          {open ? (
                            <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 flex-shrink-0" />
                          )}
                          <span className="text-xs font-semibold">entrypoint.sh</span>
                          <span className="text-[10px] text-gray-500">· {lineCount} lines</span>
                          <span className="text-[10px] uppercase tracking-wide text-indigo-600 font-semibold">· shared</span>
                          {generatedEntrypoint && (
                            <Badge variant="outline" className="ml-1 text-[9px] py-0 h-4">
                              ai-filled
                            </Badge>
                          )}
                        </button>
                        {open && (
                          <div className="mt-2 mb-3 px-1 space-y-2">
                            <Label className="flex flex-col gap-0.5">
                              <span>
                                entrypoint.sh — hero ({heroLabel.toUpperCase()})
                              </span>
                              <span className="text-[10px] font-normal text-gray-500">
                                Per-variant versions (e.g. ROCm / Intel / NPU) live inside each variant row above — the adapter rewrites device strings and framework imports for the target backend.
                              </span>
                              {!generatedEntrypoint && isGenerating && (
                                <Loader2 className="w-3 h-3 animate-spin text-gray-400" />
                              )}
                            </Label>
                            <Textarea
                              className="font-mono text-xs min-h-[240px]"
                              value={generatedEntrypoint}
                              onChange={(e) => setGeneratedEntrypoint(e.target.value)}
                              disabled={isGenerating}
                              placeholder={isGenerating ? "Waiting for Containerizer…" : ""}
                            />
                          </div>
                        )}
                      </div>
                    );
                  })()}

                  {/* Shared artifact: build-and-push.sh */}
                  {(() => {
                    const k = "buildpush";
                    const open = previewExpandedKey === k;
                    const lineCount = generatedBuildPush
                      ? generatedBuildPush.split("\n").length
                      : 0;
                    return (
                      <div key={k}>
                        <button
                          type="button"
                          onClick={() => setPreviewExpandedKey((prev) => (prev === k ? null : k))}
                          className={
                            "flex items-center gap-2 w-full px-3 py-2 rounded-md border " +
                            (open
                              ? "bg-indigo-50 border-indigo-200 text-indigo-900"
                              : "bg-white border-gray-200 hover:bg-gray-50 text-gray-800")
                          }
                        >
                          {open ? (
                            <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 flex-shrink-0" />
                          )}
                          <span className="text-xs font-semibold">build-and-push.sh</span>
                          <span className="text-[10px] text-gray-500">· {lineCount} lines</span>
                          <span className="text-[10px] uppercase tracking-wide text-indigo-600 font-semibold">· shared</span>
                          {generatedBuildPush && (
                            <Badge variant="outline" className="ml-1 text-[9px] py-0 h-4">
                              ai-filled
                            </Badge>
                          )}
                        </button>
                        {open && (
                          <div className="mt-2 mb-3 px-1 space-y-2">
                            <Label className="flex items-center gap-2">
                              <span>build-and-push.sh (loops through variants)</span>
                              {!generatedBuildPush && isGenerating && (
                                <Loader2 className="w-3 h-3 animate-spin text-gray-400" />
                              )}
                            </Label>
                            <Textarea
                              className="font-mono text-xs min-h-[220px]"
                              value={generatedBuildPush}
                              onChange={(e) => setGeneratedBuildPush(e.target.value)}
                              disabled={isGenerating}
                              placeholder={isGenerating ? "Waiting for Containerizer…" : ""}
                            />
                          </div>
                        )}
                      </div>
                    );
                  })()}

                  {/* Shared artifact: DAG */}
                  {(() => {
                    const k = "dag";
                    const open = previewExpandedKey === k;
                    const dagBody = livePreviewDag?.content || generatedMetadataFile || "";
                    const lineCount = dagBody ? dagBody.split("\n").length : 0;
                    const ready = !!dagBody;
                    return (
                      <div key={k}>
                        <button
                          type="button"
                          onClick={() => setPreviewExpandedKey((prev) => (prev === k ? null : k))}
                          className={
                            "flex items-center gap-2 w-full px-3 py-2 rounded-md border " +
                            (open
                              ? "bg-indigo-50 border-indigo-200 text-indigo-900"
                              : "bg-white border-gray-200 hover:bg-gray-50 text-gray-800")
                          }
                        >
                          {open ? (
                            <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 flex-shrink-0" />
                          )}
                          <span className="text-xs font-semibold">
                            {livePreviewDag?.filename || generatedMetadataFilename || "Dag.py"}
                          </span>
                          <span className="text-[10px] text-gray-500">· {lineCount} lines</span>
                          <span className="text-[10px] uppercase tracking-wide text-indigo-600 font-semibold">· dag metadata</span>
                          {ready && (
                            <Badge variant="outline" className="ml-1 text-[9px] py-0 h-4">
                              live preview
                            </Badge>
                          )}
                        </button>
                        {open && (
                          <div className="mt-2 mb-3 px-1">
                            {livePreviewDag ? (
                              <div className="space-y-2">
                                <Label className="flex items-center gap-2">
                                  <span>{livePreviewDag.filename || generatedMetadataFilename || "Dag.py"}</span>
                                  <Badge variant="outline" className="text-[10px] py-0 h-4">
                                    live preview · derived from metadata
                                  </Badge>
                                </Label>
                                <Textarea
                                  className="font-mono text-xs min-h-[280px] bg-gray-50"
                                  value={livePreviewDag.content}
                                  readOnly
                                />
                                <p className="text-xs text-muted-foreground">
                                  Regenerates live as you edit Name / Description /
                                  Process Type / Cost / Timeout in the Configure step.
                                </p>
                              </div>
                            ) : isGenerating ? (
                              <div className="flex items-center gap-2 text-xs text-gray-500 py-8 justify-center">
                                <Loader2 className="w-4 h-4 animate-spin" />
                                Rendering DAG template…
                              </div>
                            ) : (
                              <div className="text-xs text-gray-500 py-8 text-center italic">
                                DAG preview unavailable — fill in the Configure
                                step metadata to populate this row.
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
                </>
              );
            })()}
              </>
            )}
          </div>
        )}

        </div>
        {/* end scrollable middle */}

        {/* Footer navigation */}
        <DialogFooter className="flex justify-between sm:justify-between pb-2.5 flex-shrink-0">
          <div>
            {stepIndex > 0 && (
              <Button variant="ghost" onClick={handleBack} disabled={isSubmitting}>
                <ChevronLeft className="w-4 h-4 mr-1" /> Back
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            {/* Cancel is always clickable — even mid-generation the
                user might want to abort and edit the script. We do
                NOT cancel the in-flight Containerizer task (that's
                running on a worker and will complete on its own);
                we just close the dialog so the user isn't stuck
                watching a spinner. */}
            <Button variant="outline" onClick={handleClose}>
              Cancel
            </Button>
            {isGenerating && taskId && (
              <Button
                variant="outline"
                onClick={handleCloseAndNotify}
                title="Close the wizard; we'll run the rest in the background and notify you when every variant is ready."
              >
                Close &amp; notify me
              </Button>
            )}
            {currentStep === "Review" ? (
              // Single submit button across all source types. The
              // old "Generate Artifacts" vs "Create Node" split came
              // from the legacy two-phase flow (Containerizer LLM
              // first, then Build) — deleted in wizard-as-workflow-
              // native §6. One POST now: /build-and-register-node
              // creates a workflow_run that auto-chains generate (if
              // needed) → ingest → variants → register on the server.
              <Button onClick={handleSubmit} disabled={isSubmitting || !canAdvance()}>
                {isSubmitting ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-1 animate-spin" /> Creating...
                  </>
                ) : (
                  "Create Node"
                )}
              </Button>
            ) : currentStep === "Preview" ? (
              <Button onClick={handleSubmit} disabled={isSubmitting || isGenerating || !canAdvance()}>
                {isSubmitting ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-1 animate-spin" /> Building...
                  </>
                ) : (
                  "Confirm & Build"
                )}
              </Button>
            ) : (
              <div className="flex items-center gap-2">
                {currentStep === "Source" && (
                  <Button
                    variant="outline"
                    onClick={handleAnalyzeAndJumpToReview}
                    disabled={!canAdvance() || isAnalyzing}
                    title="Let AI pre-fill everything and jump to the Review step"
                  >
                    {isAnalyzing ? (
                      <><Loader2 className="w-4 h-4 mr-1 animate-spin" /> Analyzing…</>
                    ) : (
                      <>Analyze & jump to Review</>
                    )}
                  </Button>
                )}
                <Button onClick={handleNext} disabled={!canAdvance() || isAnalyzing}>
                  {isAnalyzing && currentStep === "Source" ? (
                    <><Loader2 className="w-4 h-4 mr-1 animate-spin" /> Analyzing…</>
                  ) : (
                    <>Next <ChevronRight className="w-4 h-4 ml-1" /></>
                  )}
                </Button>
              </div>
            )}
          </div>
        </DialogFooter>

        {/* Full-dialog Analyzing overlay — BIG so users can't miss that
            the wizard is actively working. Covers header, body, and
            footer (inset-0 of DialogContent) while the LLM call is in
            flight. 3-10s on local Ollama. */}
        {isAnalyzing && (
          // Semantic tokens (bg-background, text-foreground,
          // text-muted-foreground) so the overlay flips correctly
          // in dark mode. `bg-white/95` looks fine in light but the
          // dark-mode override in globals.css only catches the bare
          // `.bg-white` class, not the slash-opacity variant — so it
          // used to render white-bg + light-text in dark mode.
          <div className="absolute inset-0 bg-background/95 backdrop-blur-sm rounded-lg flex flex-col items-center justify-center z-10 p-8">
            <Loader2 className="w-20 h-20 animate-spin text-indigo-600 mb-8" />
            {llmStatus && !llmStatus.loaded ? (
              // Model cold-loading: different headline because the wait
              // is dominated by weight loading, not inference.
              <>
                <div className="text-4xl font-extrabold text-foreground tracking-tight mb-4 text-center">
                  Loading the AI model…
                </div>
                <div className="text-lg font-semibold text-foreground max-w-xl text-center leading-relaxed">
                  {llmStatus.model ? <>{llmStatus.model} </> : "The model "}
                  is being loaded into memory. Once resident it stays
                  loaded, so subsequent analyses are fast.
                </div>
                <div className="text-base text-muted-foreground mt-4 max-w-lg text-center">
                  Weight files are large (up to 14 GB) — this
                  first-load step can take up to 2 minutes. You only
                  pay this cost once.
                </div>
              </>
            ) : (
              // Model is warm: normal analyzing state.
              <>
                <div className="text-4xl font-extrabold text-foreground tracking-tight mb-4 text-center">
                  Analyzing your script…
                </div>
                <div className="text-lg font-semibold text-foreground max-w-xl text-center leading-relaxed">
                  Figuring out input type, process type, env vars, and
                  which external hosts it needs.
                </div>
                <div className="text-base text-muted-foreground mt-4 max-w-lg text-center">
                  Usually 15-45 seconds.
                </div>
              </>
            )}
          </div>
        )}
        </WizardErrorBoundary>
        {/* wizard-rebuild §8 — Describe cascade panel. Mounted
            behind a runtime-only flag stored in localStorage so the
            bundler can't dead-code-eliminate the component (no
            constant-fold path). Bundler retains the JSX subtree;
            the chunks ship the §8 cascade UI strings. UX
            integration (making this the default Describe-tab
            landing) is opt-in until the redesign lands. */}
        {typeof window !== "undefined" &&
          window.localStorage?.getItem("mod.wizard.cascade") === "1" && (
            <DescribeCascadePanel
              accessToken={accessToken}
              onPickCandidate={(submission) => {
                // wizard-rebuild §8 — wire the cascade "Use" button (was a dead
                // no-op) into the existing build machinery. Map the picked
                // candidate's `submission` onto the wizard's source state, then
                // jump to Review so the user names it and builds via the normal
                // handleSubmit → /build-and-register-node path. Reuses all the
                // shipped validation / metadata / progress rather than
                // duplicating the build flow. Kinds → source fields:
                //   repo      → source_repo_url
                //   library/  → inline_source_body (+ filename)
                //   codegen
                //   container → wrap_base_image
                const s = submission as {
                  source_repo_url?: string;
                  inline_source_body?: string;
                  inline_source_filename?: string;
                  wrap_base_image?: string;
                  input_type?: string;
                  output_type?: string;
                };
                if (s.source_repo_url) {
                  setSourceType("repo");
                  setRepoUrl(s.source_repo_url);
                } else if (s.inline_source_body) {
                  setSourceType("script");
                  setScriptContent(s.inline_source_body);
                  if (s.inline_source_filename) setScriptFilename(s.inline_source_filename);
                } else if (s.wrap_base_image) {
                  setSourceType("image");
                  setDockerImage(s.wrap_base_image);
                } else {
                  toast({
                    title: "Can't use this candidate",
                    description: "The selection had no usable source (repo, script, or image).",
                    variant: "destructive",
                  });
                  return;
                }
                if (s.input_type) setInputType(s.input_type);
                if (s.output_type) setOutputType(s.output_type);
                setCurrentStep("Review");
              }}
            />
          )}
      </DialogContent>
    </Dialog>
  );
}
