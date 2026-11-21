"use client";

// Workflow Wizard side-rail panel.
// Mounts as a right-side overlay drawer the user toggles from the
// toolbar. Sends free-text turns to /support/workflow-wizard, renders
// the assistant reply, and — when the assistant emits a concrete
// proposal — surfaces "Preview" + "Apply" buttons. Apply calls
// /support/workflow-wizard/apply, then asks the parent to load the
// resulting workflow into the canvas.

import { wbNavLabel } from "@/lib/product-names";
import React, { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Send, Sparkles, WandSparkles, Upload } from "lucide-react";
import type { WizardCanvasPayload } from "@/lib/wizard-canvas";
import { componentiseProposal, applyComponentSelection } from "@/lib/wizard-canvas";
import { authenticatedFetch } from "@/lib/api-client";
import { WizardProposalDiff } from "./wizard-proposal-diff";
// WSIP 4.6a — side-by-side original-vs-fixed run output, rendered under the
// turn that emitted a fix_verification_succeeded SSE event carrying both
// output arrays.
import DebugOutputCompare, { OutputItem } from "./DebugOutputCompare";
// EXPR 5.7 — "Why this workflow?" rationale disclosure rendered below the
// proposal card on the turn that emitted the final SSE event.
import WizardRationale from "./WizardRationale";

import { getApiUrl } from "@/lib/urls";
const API_URL = getApiUrl();
// WSIP 4.5 — the default opening question for a debug-bound thread.
const DEBUG_RUN_QUESTION = "Why did this run fail?";

interface ProposalNode {
  id: string;
  type?: string;
  data?: {
    label?: string;
    nodeDefinitionName?: string;
    [k: string]: unknown;
  };
  position?: { x: number; y: number };
  // Merge proposals only — anchored side(s) of the edge reference an
  // existing canvas node id, not a proposal-local id. The diff preview
  // (§10) renders these as "Anchors" rather than "Added".
  anchor_source?: boolean;
  anchor_target?: boolean;
}

interface ProposalEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  anchor_source?: boolean;
  anchor_target?: boolean;
}

interface ProposalRemoves {
  node_ids: string[];
  edge_ids: string[];
}

interface Proposal {
  name: string;
  nodes: ProposalNode[];
  edges: ProposalEdge[];
  apply_mode?: "replace" | "merge";
  removes?: ProposalRemoves | null;
}

// Canvas-aware audit response — the server emits findings instead of a
// proposal when intent === "audit_graph". Rendered in §12 (audit panel
// mode); for now we just receive + persist on the chat turn.
interface AuditFinding {
  id: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  affected_node_ids: string[];
  affected_edge_ids: string[];
  suggested_action?: "ignore" | "fix_via_wizard" | "open_node" | null;
  fix_prompt?: string | null;
}

interface AuditBlock {
  summary: string;
  findings: AuditFinding[];
}

type WizardIntent =
  | "from_scratch"
  | "suggest_next"
  | "fill_gap"
  | "refactor_selection"
  | "audit_graph";

// workflow-agentic-execution-loop §G + §H — node-wizard handoff card.
// Server emits one of these per missing node referenced by the
// proposal (`type: "needs_node_build"` SSE event + `node_build_handoffs`
// array on the `final` event + the apply 422 detail). FE renders a
// "Build this missing node" CTA card per spec; clicking routes to
// `spec.prefill_url` (already URL-encoded by the server).
interface NodeBuildHandoff {
  node_name: string;
  input_type: string | null;
  output_type: string | null;
  description: string;
  process_type: string;
  suggested_wrap_base_image: string | null;
  prefill_url: string;
  rationale: string;
}

// workflow-agentic-execution-loop §7.7 + §H — tested-lineage chip
// data emitted on `final` when the agent loop ran (every test_run
// that fired during the turn).
interface TestedLineage {
  test_run_ids: number[];
  iteration_count: number;
}

interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  proposal?: Proposal | null;
  audit?: AuditBlock | null;
  intent?: WizardIntent | null;
  // workflow-wizard-missing-node-handoff — structured cards rendered
  // inline below the assistant reply when the proposal contains
  // placeholder nodes. Each card has a "Build it now" button.
  missingNodes?: MissingNodeHint[];
  // workflow-agentic-execution-loop §G + §H — server-inferred node
  // wizard handoff specs (one card per missing node, prefilled URL).
  nodeBuildHandoffs?: NodeBuildHandoff[];
  // workflow-agentic-execution-loop §7.7 — tested lineage chip data.
  // Present only when the agent loop ran (legacy pipeline emits null).
  testedLineage?: TestedLineage | null;
  // workflow-agentic-execution-loop §7.7 — render an amber border +
  // sub-label when the agent loop exhausted iterations without a
  // SATISFIES verdict. Legacy pipeline never sets this.
  bestEffort?: boolean;
  // workflow-wizard SSE stream — accumulated "thinking" lines that
  // appear live while the Builder+Critic loop runs, then collapse
  // behind a "Show thinking" toggle once the final event lands.
  streaming?: boolean;
  thinkingLines?: string[];
  thinkingExpanded?: boolean;
  // WSIP 4.4 — assistant message id; used to anchor quality-signal
  // (debug_fix_applied) after a successful Apply in debug mode.
  messageId?: number;
  // GSEC R-022 — the final event carried needs_confirmation: true
  // (platform setting wizard.require_human_confirm_before_exec is on),
  // so the wizard paused BEFORE dispatching its test run. The panel
  // renders an inline confirmation card from this data. "Confirm &
  // run" re-sends lastUserMessage with `confirmed: true`; "Not now"
  // clears the field back to null.
  needsConfirm?: {
    workflowName: string;
    nodeCount: number;
    edgeCount: number;
    nodeNames: string[];
    lastUserMessage: string;
  } | null;
  // WSIP 4.6a — sanitized original-vs-candidate run outputs captured from a
  // fix_verification_succeeded SSE event (debug mode only). Rendered below
  // the turn as a DebugOutputCompare card.
  debugCompare?: { original: OutputItem[]; candidate: OutputItem[] } | null;
  // EXPR 5.7 — rationale entries from the final SSE event (the "why this
  // workflow?" disclosure). Raw payload shape; sanitized by WizardRationale.
  rationale?: unknown;
}

/** WSIP 4.6a — sanitize one side of a fix_verification_succeeded payload:
 *  keep items with a string label, an allowed kind and a string value;
 *  drop invalid items; cap 50 per side. Returns null when the field isn't
 *  an array at all (so the caller can require BOTH sides before rendering). */
function sanitizeOutputItems(raw: unknown): OutputItem[] | null {
  if (!Array.isArray(raw)) return null;
  const out: OutputItem[] = [];
  for (const item of raw) {
    if (!item) continue;
    const kind = (item as any).kind;
    if (kind !== "text" && kind !== "json" && kind !== "image" && kind !== "url") continue;
    const label = (item as any).label;
    const value = (item as any).value;
    if (typeof label !== "string" || typeof value !== "string") continue;
    // image/url values are rendered as a live <img src>/<a href> — only
    // allow http(s) or site-relative storage refs, never javascript:/data:.
    if ((kind === "image" || kind === "url") && !/^(https?:\/\/|\/)/i.test(value)) continue;
    out.push({ label, kind, value });
    if (out.length >= 50) break;
  }
  return out;
}

/** Map a backend SSE event to a one-line human-readable label for the
 *  live thinking feed. Returns null when the event shouldn't be shown
 *  to the user (purely structural / redundant). */
function wizardEventLabel(evt: any): string | null {
  const t = evt?.type as string;
  const it = typeof evt?.iteration === "number" ? evt.iteration : null;
  switch (t) {
    case "started":
      return "Looking at your request…";
    case "audit_started":
      return "Reading your workflow…";
    case "audit_done":
      return `Audit complete (${evt.finding_count ?? 0} findings)`;
    case "builder_started":
      return it === 0
        ? "Drafting a workflow…"
        : `Trying a different angle (round ${(it ?? 0) + 1})…`;
    case "builder_done":
      if (evt.asked_clarification) return "Asked you a clarifying question";
      if (evt.proposal_name) {
        return `Drafted: ${evt.proposal_name} (${evt.node_count ?? 0} nodes)`;
      }
      return "Drafted a proposal";
    case "builder_failed":
      return "The wizard couldn't respond — giving up";
    case "critic_started":
      return "Reviewing the draft…";
    case "critic_done": {
      const verdict = evt.verdict as string;
      const firstIssue = (evt.user_facing_issues || [])[0];
      if (verdict === "APPROVED") return "Critic approved";
      if (verdict === "NEEDS_CLARIFICATION") return "Critic wants more info";
      // REJECTED — show first issue if it's user-facing
      if (firstIssue) return `Critic flagged: ${firstIssue}`;
      return "Critic wasn't sure — retrying";
    }
    case "validator_passed":
      return "Validated — done";
    case "validator_rejected": {
      const issues = evt.issues || [];
      return issues[0]
        ? `Validator caught: ${issues[0]} — retrying`
        : "Validator caught issues — retrying";
    }
    case "needs_clarification":
      return "Surfacing a question to you";
    case "loop_exhausted":
      return "Out of attempts — surfacing best effort";
    case "error":
      return "Something went wrong";
    // 'retry' is implicit (next builder_started follows immediately).
    case "retry":
    case "final":
      return null;
    // ---------------------------------------------------------------
    // workflow-agentic-execution-loop §7.5 + §B + §G + §H — agent
    // loop / port-type enforcement / handoff event vocabulary.
    // ---------------------------------------------------------------
    case "agent_started":
      return "Starting the agent loop…";
    case "test_input_selected": {
      const tier = evt.tier as string | undefined;
      const inT = evt.input_type as string | undefined;
      if (tier === "tenant_recent") return `Picked one of your ${inT || "files"} as a test input`;
      if (tier === "cross_tenant_sample") return `Using a sample ${inT || ""} for the test`.trim();
      if (tier === "generated_standin") return `Generating a tiny ${inT || ""} stand-in to test on`.trim();
      return "Picked a test input";
    }
    case "test_input_unavailable":
      return "Couldn't find a test input — asking you for clarification";
    case "test_run_dispatched":
      return `Running the draft against the test input (run ${evt.workflow_run_id ?? "?"})…`;
    case "test_run_succeeded":
      return "Test run finished";
    case "test_run_failed":
      return `Test run failed: ${evt.reason || "unknown"}`;
    case "output_critic_started":
      return "Reviewing the test output…";
    case "output_critic_verdict": {
      const v = evt.verdict as string;
      if (v === "SATISFIES_GOAL") return "Output looks right";
      if (v === "NEEDS_USER_CLARIFICATION") return `Critic wants more info: ${evt.user_question || "(no question)"}`;
      return `Critic flagged: ${evt.failure_mode || "needs fix"}`;
    }
    case "iteration_continuing":
      return `Trying again with that feedback (round ${(it ?? 0) + 2})…`;
    case "agent_finished": {
      const r = evt.result as string;
      if (r === "SUCCESS") return "Done — workflow verified against the test input";
      if (r === "USER_CLARIFICATION") return "Need more info from you to continue";
      if (r === "NO_INPUT") return "Need a sample input from you before I can test this";
      // BEST_EFFORT
      return "Surfacing best effort — couldn't verify within the iteration budget";
    }
    case "adapter_inserted":
      return `Auto-bridged ${evt.src_type || "?"} → ${evt.tgt_type || "?"} with ${evt.adapter_name || "adapter"}`;
    case "needs_node_build":
      return `Missing node: ${evt.node_name || "?"} — offering a one-click build`;
    // ---------------------------------------------------------------
    // WSIP 4.3 — conversational-debug SSE vocabulary (services/wizard_debug).
    // Same live-feed treatment as the build events: readable progress lines.
    // ---------------------------------------------------------------
    case "debug_started":
      return `Debugging run #${evt.bound_run_id ?? "?"}…`;
    case "context_bundle_assembled": {
      const nodes = evt.node_count ?? 0;
      const artifacts = evt.artifact_count ?? 0;
      const bits = [
        `${nodes} node${nodes === 1 ? "" : "s"}`,
        `${artifacts} artifact${artifacts === 1 ? "" : "s"}`,
      ];
      if (evt.lineage_truncated) bits.push("lineage truncated");
      return `Collected the run's story: ${bits.join(", ")}`;
    }
    case "root_cause_identified":
      return "Found the node that went wrong first";
    case "fix_proposed":
      return "Proposing a fix for the workflow";
    case "fix_verification_started":
      return "Re-running the fixed workflow to check it…";
    case "fix_verification_succeeded":
      return "Fix verified — the re-run passed";
    case "fix_verification_failed":
      return `Fix didn't hold: ${evt.reason || evt.error || "re-run failed"}`;
    case "apply_card_ready":
      return "Fix is ready to apply";
    default:
      return null;
  }
}

/** When `Apply to canvas` returns 422 with detail.error === "missing_nodes",
 *  the panel switches into a recovery state listing the missing names so
 *  the user can build them and retry without losing the conversation
 *  (issue #20). */
interface MissingNodesRecovery {
  missing: string[];
  /** project-agent §6.2 — the builder's own context for each missing
   *  node, when the proposal carried it. The API accepts either bare
   *  names or these objects; sending the objects means the node builder
   *  is told the expected input, purpose and expected output instead of
   *  re-deriving them from a label. Keyed by label. */
  hints?: Record<string, MissingNodeHint>;
  proposalName: string;
  /** Latest retry result — null while the user is still building, then
   *  one of: "still-missing" (some names unchanged), "other-error"
   *  (different failure), or null after success (recovery cleared). */
  retryStatus: "idle" | "retrying" | "still-missing" | "other-error";
}

/** workflow-wizard-missing-node-handoff — surfaced inline in the chat
 *  the moment the wizard proposes a workflow that references a node
 *  not in AVAILABLE NODES. Each entry carries enough info for the
 *  Build button to open the New Node Wizard with source_type + source
 *  + label + purpose pre-filled, so the user clicks Build instead of
 *  re-entering the problem. */
export interface MissingNodeHint {
  placeholder_id: string;
  label: string;
  purpose: string;
  suggested_source_type: "git" | "image" | "dockerfile" | "script" | string;
  suggested_source: string;
  input_schema_hint: Record<string, string>;
  output_schema_hint: Record<string, string>;
}

interface Props {
  open: boolean;
  onClose: () => void;
  accessToken: string | null;
  // node-wizard→workflow handoff: when the Node Wizard decides a request is
  // really a multi-step pipeline, it opens this panel seeded with the user's
  // original description. On open we auto-send it so the workflow composes
  // immediately ("switch to workflow wizard" → boom). Guarded so it fires once
  // per distinct prompt.
  initialPrompt?: string;
  // WSIP 4.5/4.6 — conversational debugging. When set, the panel opens in
  // DEBUG mode bound to a failed WorkflowRun: the header names the run, every
  // wizard turn ships `mode: "debug"` + `bound_run_id` so the server replays
  // that run's context bundle (lineage / artifacts / root cause) instead of
  // composing from scratch, and the user's first message defaults to
  // "Why did this run fail?" (auto-sent once per distinct run id, guarded the
  // same way as initialPrompt). Absent / null → today's build behaviour,
  // byte-for-byte.
  debugRunId?: number | null;
  // Called after Apply succeeds. Parent should load the workflow into
  // the canvas (e.g. via the same code path as the "Load" toolbar
  // action). Receives (workflowId, versionId, name, apply_mode). For
  // apply_mode="replace" the parent should close the panel (today's
  // behavior); for apply_mode="merge" the parent should refresh the
  // canvas to the new version but KEEP the panel open so the user
  // can issue another wizard turn against the freshly-merged graph
  // (canvas-aware-workflow-wizard §10.3). Older parents that ignore
  // apply_mode keep working — they just close on every apply.
  onApplied?: (
    workflowId: number,
    versionId: number,
    name: string,
    applyMode?: "replace" | "merge",
  ) => void;
  // Per-missing-node "Build this node" handler (issue #20 + workflow-
  // wizard-missing-node-handoff). When the wizard proposes a workflow
  // with a placeholder node, OR Apply returns 422 missing_nodes,
  // clicking Build closes this wizard and opens the New Node Wizard
  // pre-filled. The second argument is the structured hint (label /
  // purpose / suggested_source_type / suggested_source / schema hints)
  // — when absent (legacy 422 recovery), only the name is known.
  onBuildMissingNode?: (nodeName: string, hint?: MissingNodeHint) => void;
  // Called after "Build all missing nodes" successfully dispatches a
  // batch of Containerizer tasks (issue #20 / task 8.1). Parent uses
  // this to hand the user off to the Build Queue to watch progress.
  onAllMissingDispatched?: (tasks: Array<{ name: string; task_id: number }>) => void;
  // Canvas-aware-workflow-wizard §12: clicking "Open node" on an audit
  // finding closes the panel and selects the affected node on the
  // canvas. Optional — when absent, the action button hides.
  onSelectCanvasNode?: (nodeId: string) => void;
  // Canvas-aware wizard (canvas-aware-workflow-wizard §8). Pure-getter
  // form so the panel always pulls FRESH state on every send/apply —
  // capturing nodes/edges in props would stale-close on rerenders.
  // Returns null when the canvas is empty; the panel sees that as
  // "no canvas, fall through to from-scratch" and ships no canvas
  // field in the request body.
  getCanvas?: () => WizardCanvasPayload | null;
  // Workflow context for merge-mode applies. When the user has a saved
  // workflow loaded the parent supplies its id + the currently-loaded
  // version id; the apply path forwards both to the server so a merge
  // proposal can land on a NEW WorkflowVersion of the SAME Workflow.
  // Required when a wizard turn returns apply_mode === "merge"; absent
  // for unsaved canvases (the wizard's recommendation in that case is
  // to save first or use replace mode).
  currentWorkflowId?: number | null;
  currentVersionId?: number | null;
}

export const WorkflowWizardPanel: React.FC<Props> = ({
  open, onClose, accessToken, initialPrompt, debugRunId, onApplied, onBuildMissingNode, onAllMissingDispatched,
  getCanvas, currentWorkflowId, currentVersionId,
  onSelectCanvasNode,
}) => {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<MissingNodesRecovery | null>(null);
  /** project-agent §6.2 — the builder's own hints for missing nodes,
   *  keyed by label, accumulated across turns. A ref rather than state:
   *  the auto-queue dispatcher reads it from inside a closure where
   *  `turns` would be stale, and nothing renders from it. */
  const missingHintsRef = useRef<Record<string, MissingNodeHint>>({});
  // Tracks which auto-queued node names we're still waiting on. When
  // this set drains (every name has appeared in the user's
  // NodeDefinition library), we auto-retry the apply (issue #20 /
  // task 8.4). Polled against GET /node-definitions/ every 15s
  // while the recovery panel is open.
  const [awaitingMissing, setAwaitingMissing] = useState<string[] | null>(null);
  const [testRun, setTestRun] = useState<{
    runId: number;
    status: "dispatching" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMEOUT";
    error?: string | null;
    wizardTestRunId?: number | null;
  } | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  // WSIP 4.4 — guard against sending the quality-signal more than
  // once per assistant message. Only set when debugRunId != null.
  const qualitySignalSentRef = useRef<Set<number>>(new Set());
  // WSIP 4.6a — outputs captured from the most recent fix_verification_succeeded
  // SSE event that carried BOTH original_outputs and candidate_outputs.
  // Cleared whenever debugRunId changes (see the debug-seed effect below).
  const debugCompareRef = useRef<{ original: OutputItem[]; candidate: OutputItem[] } | null>(null);

  // Always scroll to bottom on new turns.
  useEffect(() => {
    if (messagesRef.current) {
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
    }
  }, [turns]);

  // Reset session when the panel reopens cold (no turns yet) so each
  // workflow problem gets a fresh thread.
  useEffect(() => {
    if (!open) return;
    if (turns.length === 0) {
      setSessionId(null);
      setError(null);
      setTestRun(null);
    }
  }, [open]);

  // Intent is never set explicitly from the client anymore — the §9 intent
  // buttons and the inferred-intent chip were removed (zero-thought wizard).
  // The server's _infer_wizard_intent owns intent resolution end-to-end.

  // Audit panel mode (canvas-aware-workflow-wizard §12).
  //   `mode` toggles between the chat transcript and the dedicated
  //   audit-findings surface. Auto-switches to "audit" when an
  //   audit_graph response arrives.
  //   `latestAudit` carries the most recent findings so the user can
  //   close + reopen the panel within a session and still see them.
  //   `ignoredFindingIds` is a client-side dismiss list (per-session;
  //   doesn't survive new-conversation reset).
  const [mode, setMode] = useState<"chat" | "audit">("chat");
  const [latestAudit, setLatestAudit] = useState<AuditBlock | null>(null);
  const [ignoredFindingIds, setIgnoredFindingIds] = useState<Set<string>>(
    () => new Set(),
  );

  // Per-turn multi-edit accept state (canvas-aware-workflow-wizard §11).
  // Default = all components accepted. Toggling off a component strips
  // it from the proposal at apply time. Keyed by turn index since each
  // assistant turn can carry its own proposal with its own components.
  const [acceptedComponentsByTurn, setAcceptedComponentsByTurn] = useState<
    Record<number, Set<string>>
  >({});

  /** Toggle one component on/off for a given turn's proposal. */
  const toggleComponent = (turnIdx: number, componentId: string) => {
    setAcceptedComponentsByTurn(prev => {
      const current = prev[turnIdx] || new Set<string>();
      const next = new Set(current);
      if (next.has(componentId)) next.delete(componentId);
      else next.add(componentId);
      return { ...prev, [turnIdx]: next };
    });
  };

  const testRunPollRef = useRef<{ cancel: () => void } | null>(null);

  // Inline test-input upload. When the wizard can't find a matching file in the
  // tenant's library it asks for one; rather than send the user off to the Files
  // page, they upload here and it lands in their library, where the test-run's
  // tenant_recent input selection then picks it up on the next "Test".
  const [inputUpload, setInputUpload] = useState<
    { state: "idle" | "uploading" | "done" | "error"; name?: string; msg?: string }
  >({ state: "idle" });

  const uploadTestInput = () => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.onchange = async () => {
      const file = inp.files && inp.files[0];
      if (!file) return;
      setInputUpload({ state: "uploading", name: file.name });
      try {
        const fd = new FormData();
        fd.append("file", file);
        const resp = await authenticatedFetch(`${API_URL}/files/upload`, {
          method: "POST",
          body: fd,
        });
        if (!resp.ok) {
          const b = await resp.json().catch(() => ({} as any));
          throw new Error((b && (b.detail as string)) || `HTTP ${resp.status}`);
        }
        setInputUpload({ state: "done", name: file.name });
        // Re-run the test automatically — the user shouldn't have to type
        // "test again" after uploading. The wizard re-enters its loop and now
        // finds the just-uploaded file via tenant_recent input selection.
        void send("I've uploaded a sample input — test the workflow now.");
      } catch (e: any) {
        setInputUpload({ state: "error", name: file.name, msg: e.message || String(e) });
      }
    };
    inp.click();
  };

  const dispatchTestRun = async () => {
    if (!sessionId || busy || testRun) return;
    setBusy(true);
    setError(null);
    setTestRun({ runId: 0, status: "dispatching" });
    try {
      const resp = await authenticatedFetch(`${API_URL}/support/workflow-wizard/test-run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const detail = body?.detail || `HTTP ${resp.status}`;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      const data = await resp.json();
      const runId = data.workflow_run_id as number;
      setTestRun({
        runId,
        status: "QUEUED",
        wizardTestRunId: data.wizard_test_run_id ?? null,
      });
      pollTestRun(runId);
    } catch (e: any) {
      setError(e.message || String(e));
      setTestRun(null);
    } finally {
      setBusy(false);
    }
  };

  const pollTestRun = (runId: number) => {
    testRunPollRef.current?.cancel();
    let cancelled = false;
    let ticks = 0;
    const MAX_TICKS = 120;
    const TICK_MS = 2_000;
    testRunPollRef.current = {
      cancel: () => { cancelled = true; },
    };
    const poll = async () => {
      if (cancelled) return;
      ticks += 1;
      try {
        const resp = await authenticatedFetch(
          `${API_URL}/support/workflow-wizard/test-run/${runId}/status`,
        );
        if (!resp.ok) return;
        const data = await resp.json();
        const s = data.status as string;
        setTestRun(prev => prev ? {
          ...prev,
          status: s as any,
          error: data.error ?? null,
          wizardTestRunId: data.wizard_test_run_id ?? prev.wizardTestRunId,
        } : null);
        if (["COMPLETED", "FAILED", "CANCELLED"].includes(s)) {
          cancelled = true;
          return;
        }
      } catch { /* next tick retries */ }
      if (!cancelled && ticks < MAX_TICKS) {
        setTimeout(poll, TICK_MS);
      } else if (ticks >= MAX_TICKS) {
        setTestRun(prev => prev ? { ...prev, status: "TIMEOUT", error: "timed out waiting" } : null);
      }
    };
    setTimeout(poll, TICK_MS);
  };

  useEffect(() => {
    return () => { testRunPollRef.current?.cancel(); };
  }, []);

  const send = async (
    overrideText?: string,
    opts?: { confirmed?: boolean },
  ) => {
    // Allow callers (e.g. the audit's Fix-via-wizard action) to bypass
    // the React-state composer and ship an explicit message — setting
    // `input` then calling send() in the same tick reads stale state
    // because React hasn't committed yet.
    const text = (overrideText ?? input).trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setInput("");
    setTestRun(null);
    // WSIP 4.6a — per-turn capture: only outputs seen during THIS send end up
    // on this turn's card.
    debugCompareRef.current = null;
    // EXPR 5.7 — reset per-turn rationale on each send.
    // (rationale is captured from the final event, so no ref needed.)
    setTurns(t => [...t, { role: "user", content: text }]);

    // Pull fresh canvas state on every send — getCanvas is called late
    // so a user editing the canvas between turns gets the right view.
    // Empty / null canvas means "from scratch" — we omit the field.
    const canvas = getCanvas ? getCanvas() : null;
    const requestBody: Record<string, unknown> = {
      message: text,
      session_id: sessionId,
    };
    // GSEC R-022 — human confirmation before test-run dispatch. The
    // server only honours this when the paused turn's proposal is
    // still the one on the table (re-send of the same turn).
    if (opts?.confirmed) requestBody.confirmed = true;
    if (canvas) requestBody.canvas = canvas;
    // WSIP 4.5 — debug mode. Every turn of a debug-bound thread ships the
    // mode + the run id; the server re-assembles that run's context bundle
    // before the Builder+Critic loop runs against it.
    if (debugRunId != null) {
      requestBody.mode = "debug";
      requestBody.bound_run_id = debugRunId;
    }
    // No `intent` is sent — the server infers it (_infer_wizard_intent):
    // empty canvas → from_scratch, verbs + selection → the right co-pilot
    // mode, LLM fallback for ambiguity. Zero-thought: the user just describes.

    // Insert a placeholder assistant turn immediately so the user sees
    // the live thinking feed while the Builder+Critic loop runs on the
    // server. The placeholder gets hydrated when the SSE `final` event
    // arrives (or marked as an error if the stream fails mid-way).
    const placeholderIdx = await new Promise<number>(resolve => {
      setTurns(t => {
        const next = [
          ...t,
          {
            role: "assistant" as const,
            content: "",
            streaming: true,
            thinkingLines: [],
          },
        ];
        resolve(next.length - 1);
        return next;
      });
    });

    // Helper: append a line to the placeholder turn's thinkingLines.
    const appendThinking = (line: string) => {
      setTurns(t => {
        if (placeholderIdx >= t.length || t[placeholderIdx].role !== "assistant") {
          return t;
        }
        const lines = t[placeholderIdx].thinkingLines || [];
        // De-dupe consecutive identical lines (the backend doesn't
        // emit dupes, but defensive — keeps the feed tidy).
        if (lines.length > 0 && lines[lines.length - 1] === line) return t;
        const next = [...t];
        next[placeholderIdx] = {
          ...next[placeholderIdx],
          thinkingLines: [...lines, line],
        };
        return next;
      });
    };

    try {
      // SSE endpoint — emits typed events as the Builder+Critic loop
      // progresses so the user sees live "thinking" feedback instead
      // of staring at a frozen panel for 30-60s.
      const resp = await authenticatedFetch(`${API_URL}/support/workflow-wizard/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(requestBody),
      });
      // Canvas-aware-workflow-wizard §8.4 — 413 still surfaces here
      // (validation runs server-side BEFORE the stream starts).
      if (resp.status === 413) {
        const body = await resp.json().catch(() => ({}));
        const limit = body?.detail?.limit || {};
        const limitNodes = limit.nodes ?? "?";
        const limitEdges = limit.edges ?? "?";
        const msg =
          `Your workflow is too large for the wizard ` +
          `(limit: ${limitNodes} nodes / ${limitEdges} edges). ` +
          `Select a region of the canvas first, or split the workflow.`;
        setError(msg);
        // Replace the placeholder with the error message.
        setTurns(t => {
          if (placeholderIdx >= t.length) return t;
          const next = [...t];
          next[placeholderIdx] = {
            role: "assistant",
            content: msg,
          };
          return next;
        });
        return;
      }
      // WSIP 4.5 — the debug flag is off for this organization (or the
      // wizard's debug entrypoint is otherwise disabled): the server answers
      // 404 {"detail":{"error":"feature_disabled"}} BEFORE any stream starts.
      // That is not a crash — it is a normal "you can't do this yet" state, so
      // render it as a friendly assistant turn instead of the generic
      // "wizard is unreachable" path. run_not_found / bound_run_id_required /
      // everything else keep falling through to the existing error path.
      if (resp.status === 404) {
        const body = await resp.json().catch(() => ({} as any));
        const detail = (body as any)?.detail;
        const errCode =
          typeof detail === "string"
            ? detail
            : ((detail as any)?.error ?? "");
        if (errCode === "feature_disabled") {
          const msg =
            "Conversational debugging isn't enabled for your organization.";
          // Render once, as a friendly assistant turn — NOT also as the
          // generic red error banner (that would show the same line twice).
          setTurns(t => {
            if (placeholderIdx >= t.length) return t;
            const next = [...t];
            const prevLines = next[placeholderIdx].thinkingLines || [];
            next[placeholderIdx] = {
              role: "assistant",
              content: msg,
              thinkingLines: prevLines.length ? prevLines : undefined,
              thinkingExpanded: false,
            };
            return next;
          });
          return;
        }
      }
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 300)}`);
      }
      let finalEvent: any = null;
      const hasStreamBody =
        resp.body && typeof (resp.body as any).getReader === "function";

      if (hasStreamBody) {
        // SSE reader loop. Events arrive as `data: {json}\n\n` lines.
        const reader = (resp.body as ReadableStream<Uint8Array>).getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        // `final` is the terminal event per the wizard SSE protocol — once it
        // lands the turn is done. Break out of the read loop on it (and stop
        // the reader) instead of waiting for the socket to close: if the
        // server's stream lingers (a slow flush / a stuck upstream), waiting
        // for `done` would hold `busy` past the terminal event and leave the
        // composer frozen. We still let the `finally` release the reader.
        let streamDone = false;
        let streamErrorMessage: string | null = null;
        while (!streamDone) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          // Split on the SSE record separator (blank line). The trailing
          // partial record stays in `buffer` for the next read.
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
            const label = wizardEventLabel(evt);
            if (label) appendThinking(label);
            // 2400LR-wb12f — the server names the cause in an `error` event
            // (e.g. model + host:port). Remember it so the catch below shows
            // it instead of the generic "unreachable" fallback.
            if (
              evt.type === "error" &&
              typeof evt.message === "string" &&
              evt.message.trim()
            ) {
              streamErrorMessage = evt.message.trim();
            }
            // WSIP 4.6a — a verified fix MAY carry both output arrays; when
            // both are present and valid, remember them so the hydrated turn
            // can render a DebugOutputCompare card (debug mode only).
            if (debugRunId != null && evt.type === "fix_verification_succeeded") {
              const orig = sanitizeOutputItems(evt.original_outputs);
              const cand = sanitizeOutputItems(evt.candidate_outputs);
              if (orig && cand) {
                debugCompareRef.current = { original: orig, candidate: cand };
              }
            }
            // EXPR 5.7 — rationale may land before `final`; capture it early.
            if (evt.rationale !== undefined && finalEvent === null) {
              const r = evt.rationale;
              if (Array.isArray(r)) {
                finalEvent = { ...finalEvent, rationale: r.slice(0, 50) };
              }
            }
            if (evt.type === "final") {
              finalEvent = evt;
              streamDone = true;
              break;
            }
          }
        }
        // Release the socket once we're done consuming the terminal event —
        // best-effort (the `finally` below clears `busy` regardless).
        try { await reader.cancel(); } catch { /* ignore */ }
        if (!finalEvent && streamErrorMessage) {
          const named: any = new Error(streamErrorMessage);
          named.serverMessage = true;
          throw named;
        }
      } else {
        // Fallback for environments without ReadableStream support on
        // the fetch response (notably the Jest jsdom test runner, whose
        // mocked Response objects expose `.json()` but no `.body`). The
        // server may have returned a single JSON blob in WizardResponse
        // shape instead of an SSE stream. Treat it as a synthetic
        // `final` event so the panel still hydrates correctly.
        const data = await resp.json();
        finalEvent = { type: "final", ...data };
      }

      if (!finalEvent) {
        throw new Error("wizard stream ended without a final event");
      }

      const data = finalEvent;
      setSessionId(data.session_id);
      // Audit response → store findings + auto-switch the panel into
      // Audit mode so the user lands on the dedicated surface
      // (canvas-aware-workflow-wizard §12).
      if (data.audit) {
        // §9.4 — fresh audit response, reset the per-audit acted set
        // so the dismiss-on-close flush has a clean baseline.
        actedFindingIdsRef.current = new Set();
        setLatestAudit(data.audit as AuditBlock);
        setMode("audit");
      }
      // GSEC R-022 — the wizard paused before dispatching its test
      // run, waiting for explicit human confirmation. Derive the
      // summary from the final event's proposal (= the full graph);
      // fall back to the graph_summary the `needs_confirmation` SSE
      // event may have echoed onto the final envelope.
      let needsConfirmData: ChatTurn["needsConfirm"] = null;
      if (data.needs_confirmation === true) {
        const p = data.proposal;
        const nodes = Array.isArray(p?.nodes) ? (p.nodes as ProposalNode[]) : [];
        const edges = Array.isArray(p?.edges) ? (p.edges as ProposalEdge[]) : [];
        const graphSummary = (data.graph_summary || {}) as Record<string, unknown>;
        const summaryNames = Array.isArray(graphSummary.node_names)
          ? (graphSummary.node_names as string[])
          : [];
        needsConfirmData = {
          workflowName:
            (typeof graphSummary.workflow_name === "string" && graphSummary.workflow_name) ||
            (typeof p?.name === "string" ? p.name : "") ||
            "Untitled workflow",
          nodeCount:
            (typeof graphSummary.node_count === "number" ? graphSummary.node_count : nodes.length),
          edgeCount:
            (typeof graphSummary.edge_count === "number" ? graphSummary.edge_count : edges.length),
          nodeNames:
            summaryNames.length > 0
              ? summaryNames
              : nodes.map(n => n.data?.label || n.data?.nodeDefinitionName || n.id),
          lastUserMessage: text,
        };
      }
      // Initialise the per-turn accept-set with ALL component ids so
      // the user starts with everything accepted (multi-edit toggling
      // only matters when the LLM emits a multi-component proposal).
      let initialAccepted: Set<string> | null = null;
      if (data.proposal && (data.proposal.apply_mode || "replace") === "merge") {
        const comps = componentiseProposal(data.proposal);
        if (comps.length >= 2) {
          initialAccepted = new Set(comps.map(c => c.id));
        }
      }
      // Hydrate the placeholder turn in place — preserves the
      // thinkingLines so the user can still "Show thinking" after
      // the final answer lands.
      setTurns(t => {
        if (placeholderIdx >= t.length) return t;
        const next = [...t];
        const prevLines = next[placeholderIdx].thinkingLines || [];
        next[placeholderIdx] = {
          role: "assistant" as const,
          content: data.response || "(empty reply)",
          proposal: data.proposal || null,
          audit: data.audit || null,
          intent: (data.intent as WizardIntent | null) || null,
          missingNodes: (() => {
            const hints = Array.isArray(data.missing_nodes)
              ? (data.missing_nodes as MissingNodeHint[])
              : [];
            // §6.2 — keep the builder's context for the auto-queue
            // dispatcher. Held in a ref rather than read back out of
            // `turns`, which is stale inside the dispatch handler's
            // closure.
            if (hints.length) {
              for (const h of hints) {
                if (h?.label) missingHintsRef.current[h.label] = h;
              }
            }
            return hints;
          })(),
          // workflow-agentic-execution-loop §G/§H — server-emitted
          // handoff specs land on the final event (also surfaced as
          // their own `needs_node_build` SSE events for live thinking).
          nodeBuildHandoffs: Array.isArray(data.node_build_handoffs)
            ? (data.node_build_handoffs as NodeBuildHandoff[])
            : [],
          testedLineage: (data.tested_lineage as TestedLineage | null) || null,
          bestEffort: Boolean(data.best_effort),
          needsConfirm: needsConfirmData,
          streaming: false,
          thinkingLines: prevLines,
          thinkingExpanded: false,
          messageId: typeof data.message_id === "number" ? data.message_id : undefined,
          // WSIP 4.6a — outputs captured from this turn's SSE stream (null
          // unless a fix_verification_succeeded event carried both arrays in
          // debug mode).
          debugCompare: debugCompareRef.current,
          // EXPR 5.7 — rationale entries from the final SSE event.
          rationale: data.rationale,
        };
        if (initialAccepted) {
          setAcceptedComponentsByTurn(prev => ({
            ...prev,
            [placeholderIdx]: initialAccepted!,
          }));
        }
        return next;
      });
    } catch (e: any) {
      setError(e.message || String(e));
      // Replace the placeholder with the error message — keep any
      // thinking lines we already accumulated so the user has
      // diagnostic context.
      setTurns(t => {
        if (placeholderIdx >= t.length) return t;
        const next = [...t];
        const prevLines = next[placeholderIdx].thinkingLines || [];
        next[placeholderIdx] = {
          role: "assistant",
          // Show the server's named cause when one arrived; the generic
          // text is only for failures that carried no server message.
          content: e?.serverMessage
            ? e.message
            : "Sorry — the wizard is unreachable. Try again in a moment.",
          thinkingLines: prevLines.length ? prevLines : undefined,
          thinkingExpanded: false,
        };
        return next;
      });
    } finally {
      setBusy(false);
    }
  };

  // node-wizard→workflow handoff: when opened with an initialPrompt (the
  // description the user typed in the Node Wizard before we decided it's really
  // a pipeline), start a fresh thread and auto-send it. Guarded by a ref so it
  // fires once per distinct prompt, and cleared on close.
  const seededPromptRef = useRef<string | null>(null);
  const sendRef = useRef(send);
  sendRef.current = send;
  useEffect(() => {
    if (!open) { seededPromptRef.current = null; return; }
    // A debug-bound thread seeds its own opening question instead — the
    // generic "describe this" prompt would fight it.
    if (debugRunId != null) return;
    const p = (initialPrompt || "").trim();
    if (p && seededPromptRef.current !== p) {
      seededPromptRef.current = p;
      setTurns([]);
      setSessionId(null);
      setError(null);
      setTestRun(null);
      void sendRef.current(p);
    }
  }, [open, initialPrompt]);

  // WSIP 4.5 — debug-mode seeding. Opening the panel with a bound run starts a
  // fresh thread and auto-sends the default question once per distinct run id
  // (same guard style as the initialPrompt handoff above: a ref keyed on the
  // id, cleared on close, so re-opening for another run re-seeds but a
  // re-render of the same run never double-sends).
  const seededDebugRunRef = useRef<number | null>(null);
  useEffect(() => {
    if (!open) { seededDebugRunRef.current = null; return; }
    if (debugRunId == null) return;
    if (seededDebugRunRef.current === debugRunId) return;
    seededDebugRunRef.current = debugRunId;
    // WSIP 4.6a — a new debug session clears any captured compare state.
    debugCompareRef.current = null;
    setTurns([]);
    setSessionId(null);
    setError(null);
    setTestRun(null);
    void sendRef.current(DEBUG_RUN_QUESTION);
  }, [open, debugRunId]);

  // Internal apply call. Returns one of several outcomes so callers can
  // distinguish missing-nodes recovery from generic errors and from
  // merge-specific failures (stale_canvas, dangling_reference).
  type ApplyOutcome =
    | {
        kind: "ok";
        data: {
          workflow_id: number;
          version_id: number;
          name: string;
          apply_mode?: "replace" | "merge";
          applied?: {
            added_node_ids: string[];
            added_edge_ids: string[];
            removed_node_ids: string[];
            removed_edge_ids: string[];
          } | null;
        };
      }
    | { kind: "missing"; missing: string[] }
    | { kind: "stale_canvas"; missing_anchor_node_ids: string[] }
    | { kind: "dangling_reference"; edges: Array<{ edge_id: string; source: string; target: string }> }
    | { kind: "error"; message: string };

  // §9.4 — track which findings the user already acted on so we can
  // post `dismissed_without_action` for the rest at panel close.
  const actedFindingIdsRef = useRef<Set<string>>(new Set());

  // wizard-learning-from-feedback §9.3 — fire-and-forget POST to the
  // audit-action endpoint when the user clicks Fix-via-wizard /
  // Open-node / Ignore on a finding. Errors logged, never block UX.
  const postAuditAction = (
    findingId: string,
    severity: "error" | "warning" | "info",
    action: "fix_via_wizard" | "open_node" | "ignore" | "dismissed_without_action",
  ) => {
    if (action !== "dismissed_without_action") {
      actedFindingIdsRef.current.add(findingId);
    }
    if (!sessionId) return;
    void authenticatedFetch(`${API_URL}/support/workflow-wizard/audit-action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audit_session_id: sessionId,
        finding_id: findingId,
        severity,
        action,
      }),
    }).catch((err) => {
      console.warn("wizard audit-action POST failed:", err);
    });
  };

  /** §9.4 — flush dismissed_without_action for findings the user
   *  never acted on. Called when the panel closes or the user
   *  starts a new conversation. Idempotent: re-running is safe
   *  because already-flushed findings are added to actedFindingIdsRef. */
  const flushDismissedAuditFindings = () => {
    if (!latestAudit) return;
    for (const f of latestAudit.findings) {
      if (actedFindingIdsRef.current.has(f.id)) continue;
      postAuditAction(f.id, f.severity, "dismissed_without_action");
      actedFindingIdsRef.current.add(f.id);
    }
  };

  // §9.4 — also fire the flush when the parent flips `open` from
  // true → false. Radix's onOpenChange only triggers on user
  // interaction (Esc / X / outside-click); programmatic closes
  // (e.g. parent navigates away) need this useEffect path.
  const wasOpenRef = useRef<boolean>(open);
  useEffect(() => {
    if (wasOpenRef.current && !open) {
      flushDismissedAuditFindings();
    }
    wasOpenRef.current = open;
    // intentionally only re-run on `open` toggle
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const callApply = async (
    proposalName: string,
    proposal: Proposal | null,
    wasEdited: boolean = false,
  ): Promise<ApplyOutcome> => {
    if (!sessionId) return { kind: "error", message: "No active wizard session" };
    const isMerge = (proposal?.apply_mode || "replace") === "merge";
    const requestBody: Record<string, unknown> = {
      session_id: sessionId,
      name: proposalName,
      // §9.2 — was_edited flows through the wire shape so the
      // server-side feedback event always has a value. §9.1 sets it
      // true when the user has touched the multi-edit toggles for
      // this turn.
      was_edited: wasEdited,
    };
    if (isMerge) {
      // Merge applies need to tell the server WHICH workflow + version
      // to merge into. If the parent didn't supply them (e.g. canvas
      // is unsaved), the server returns 422 missing_workflow_id and
      // we surface a clear message.
      if (currentWorkflowId != null) requestBody.workflow_id = currentWorkflowId;
      if (currentVersionId != null) requestBody.version_id = currentVersionId;
    }
    try {
      const resp = await authenticatedFetch(`${API_URL}/support/workflow-wizard/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      if (resp.status === 422) {
        const body = await resp.json().catch(() => ({}));
        const detail = body?.detail || {};
        // Distinguish merge-specific failures from missing_nodes so
        // the panel can render the right recovery affordance.
        if (detail.error === "stale_canvas") {
          return {
            kind: "stale_canvas",
            missing_anchor_node_ids: Array.isArray(detail.missing_anchor_node_ids)
              ? detail.missing_anchor_node_ids
              : [],
          };
        }
        if (detail.error === "dangling_reference") {
          return {
            kind: "dangling_reference",
            edges: Array.isArray(detail.edges) ? detail.edges : [],
          };
        }
        const missing: string[] = Array.isArray(detail.missing) ? detail.missing : [];
        return { kind: "missing", missing };
      }
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        return { kind: "error", message: `HTTP ${resp.status}: ${body.slice(0, 300)}` };
      }
      const data = await resp.json();
      return { kind: "ok", data };
    } catch (e: any) {
      return { kind: "error", message: e.message || String(e) };
    }
  };

  const apply = async (proposal: Proposal, turnIdx?: number) => {
    if (!sessionId || busy) return;
    // §9.1 — acceptedComponentsByTurn[turnIdx] is seeded to "every
    // component accepted" as soon as a multi-component proposal
    // arrives (see the `initialAccepted` hydration above), so its
    // mere presence does NOT mean the user touched anything. The
    // user-edited-the-proposal signal is that the accepted set no
    // longer equals the full default set — i.e. the user actually
    // deselected (or re-selected) at least one component toggle.
    // Pass through to the server so the feedback event records
    // "accepted with edits" rather than "accepted as-is".
    const wasEdited = (() => {
      if (turnIdx === undefined) return false;
      const accepted = acceptedComponentsByTurn[turnIdx];
      if (accepted === undefined) return false;
      const components = componentiseProposal(proposal);
      if (components.length < 2) return false;
      if (accepted.size !== components.length) return true;
      return components.some(c => !accepted.has(c.id));
    })();
    // Multi-edit accept/reject (canvas-aware-workflow-wizard §11):
    // when the user has toggled off some components, ship a TRIMMED
    // proposal. The server validator runs against what we send, so
    // partial accepts that break invariants get a clear error.
    let effectiveProposal = proposal;
    if (turnIdx !== undefined && acceptedComponentsByTurn[turnIdx]) {
      const components = componentiseProposal(proposal);
      if (components.length >= 2) {
        const accepted = acceptedComponentsByTurn[turnIdx];
        if (accepted.size === 0) {
          setError("Select at least one change to apply.");
          return;
        }
        effectiveProposal = applyComponentSelection(
          proposal, components, accepted,
        ) as Proposal;
      }
    }
    setBusy(true);
    setError(null);
    try {
      const outcome = await callApply(effectiveProposal.name, effectiveProposal, wasEdited);
      if (outcome.kind === "ok") {
        setRecovery(null);
        setAwaitingMissing(null);
        const isMerge = (outcome.data.apply_mode || "replace") === "merge";
        onApplied?.(
          outcome.data.workflow_id,
          outcome.data.version_id,
          outcome.data.name,
          outcome.data.apply_mode || "replace",
        );
        // WSIP 4.4 — debug-bound thread: record that the fix was applied.
        // Fire-and-forget: never blocks the Apply path, never surfaces
        // errors to the user (bare catch), at-most-once per proposal.
        const turnMsgId = turnIdx != null ? turns[turnIdx]?.messageId : undefined;
        if (debugRunId != null && turnMsgId != null
            && !qualitySignalSentRef.current.has(turnMsgId)) {
          qualitySignalSentRef.current.add(turnMsgId);
          void authenticatedFetch(`${API_URL}/support/workflow-wizard/quality-signal`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId ?? "",
              event_type: "debug_fix_applied",
              proposal_id: turnMsgId,
              applied_workflow_version_id: outcome.data.version_id,
              feedback_event_metadata: { bound_run_id: debugRunId },
            }),
          }).catch(() => { /* fire-and-forget; signal loss is non-fatal */ });
        }
        // Merge keeps the panel open so the user can issue another
        // wizard turn against the freshly-merged canvas without
        // re-opening the panel (canvas-aware-workflow-wizard §10.3).
        // Replace continues to close via parent's onApplied (today's
        // behavior).
        const summary = outcome.data.applied
          ? `Merged into canvas. ` +
            `${outcome.data.applied.added_node_ids.length} added, ` +
            `${outcome.data.applied.removed_node_ids.length} removed.`
          : `Applied. Loaded "${outcome.data.name}" into the canvas.`;
        setTurns(t => [...t, { role: "assistant", content: summary }]);
        return;
      }
      if (outcome.kind === "missing") {
        // Switch to the recovery state instead of toasting an error
        // (issue #20). The UI lists each missing name with a copy
        // affordance so the user can build them via + New Node and
        // come back to retry without losing the chat or proposal.
        setRecovery({
          missing: outcome.missing,
          hints: missingHintsRef.current,
          proposalName: proposal.name,
          retryStatus: "idle",
        });
        setError(null);
        return;
      }
      if (outcome.kind === "stale_canvas") {
        // Merge anchor referenced a node id that no longer exists in
        // the live workflow version — the user edited the canvas
        // between Generate and Apply. Re-generate against the fresh
        // canvas; existing recovery UI doesn't apply here.
        const missingIds = outcome.missing_anchor_node_ids.join(", ");
        const msg =
          `Your canvas changed since the wizard's last suggestion ` +
          (missingIds ? `(missing: ${missingIds}). ` : ". ") +
          `Re-run the wizard against the current canvas.`;
        setError(msg);
        setTurns(t => [...t, { role: "assistant", content: msg }]);
        return;
      }
      if (outcome.kind === "dangling_reference") {
        const ids = outcome.edges.map(e => e.edge_id).join(", ");
        const msg =
          `The proposed change would orphan canvas edge${outcome.edges.length === 1 ? "" : "s"} ` +
          `${ids}. Ask the wizard to remove or rewire ${outcome.edges.length === 1 ? "it" : "them"} as part of the change.`;
        setError(msg);
        setTurns(t => [...t, { role: "assistant", content: msg }]);
        return;
      }
      setError(outcome.message);
    } finally {
      setBusy(false);
    }
  };

  /** Retry the apply against the current proposal name, expecting the
   *  user to have built one or more missing nodes since the last
   *  attempt. Updates the recovery state in place. */
  const retryApply = async () => {
    if (!recovery || busy) return;
    setBusy(true);
    setError(null);
    setRecovery(r => r ? { ...r, retryStatus: "retrying" } : r);
    try {
      // Pull the proposal off the latest assistant turn so callApply
      // can route to merge mode if applicable. The recovery flow
      // shouldn't switch modes mid-stream, but we look it up to keep
      // callApply's signature consistent.
      const lastProposal: Proposal | null = (() => {
        for (let i = turns.length - 1; i >= 0; i--) {
          const t = turns[i];
          if (t.role === "assistant" && t.proposal) return t.proposal;
        }
        return null;
      })();
      const outcome = await callApply(recovery.proposalName, lastProposal);
      if (outcome.kind === "ok") {
        setRecovery(null);
        setAwaitingMissing(null);
        onApplied?.(
          outcome.data.workflow_id,
          outcome.data.version_id,
          outcome.data.name,
          outcome.data.apply_mode || "replace",
        );
        const summary = outcome.data.applied
          ? `Merged into canvas. ` +
            `${outcome.data.applied.added_node_ids.length} added, ` +
            `${outcome.data.applied.removed_node_ids.length} removed.`
          : `Applied. Loaded "${outcome.data.name}" into the canvas.`;
        setTurns(t => [...t, { role: "assistant", content: summary }]);
        return;
      }
      if (outcome.kind === "missing") {
        // Some nodes are still missing — refresh the list (the user
        // may have built only a subset). retryStatus tells the UI
        // whether to render "still missing" or "ready" hints.
        const stillMissing = outcome.missing;
        const sameSet = stillMissing.length === recovery.missing.length &&
          stillMissing.every(n => recovery.missing.includes(n));
        setRecovery({
          missing: stillMissing,
          hints: recovery.hints ?? missingHintsRef.current,
          proposalName: recovery.proposalName,
          retryStatus: sameSet ? "still-missing" : "idle",
        });
        return;
      }
      if (outcome.kind === "stale_canvas") {
        // Canvas edited mid-build — abandon the auto-retry, surface
        // the re-review affordance (canvas-aware-workflow-wizard §13.4).
        setRecovery(null);
        setAwaitingMissing(null);
        const missingIds = outcome.missing_anchor_node_ids.join(", ");
        setError(
          `Your canvas changed during the build wait ` +
          (missingIds ? `(missing: ${missingIds}). ` : ". ") +
          `Re-run the wizard.`
        );
        return;
      }
      setRecovery(r => r ? { ...r, retryStatus: "other-error" } : r);
      setError(outcome.kind === "error" ? outcome.message : "Apply failed.");
    } finally {
      setBusy(false);
    }
  };

  const dismissRecovery = () => {
    setRecovery(null);
    setAwaitingMissing(null);
  };

  /** Auto-queue dispatcher (issue #20 / task 8.1). Hits the new
   *  /support/workflow-wizard/queue-missing-nodes endpoint with the
   *  current recovery panel's missing list; on success hands the
   *  task IDs up to the parent so the Build Queue can show progress.
   *  The user watches the Build Queue, then clicks Try again here
   *  once the auto-queued nodes finish landing in the library. */
  const dispatchAllMissing = async () => {
    if (!recovery || !sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await authenticatedFetch(`${API_URL}/support/workflow-wizard/queue-missing-nodes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // §6.2 — send the builder's hint where we have one, so the node
        // builder gets expected input / purpose / expected output rather
        // than re-deriving them from the label. Falls back to the bare
        // name, which the API still accepts.
        body: JSON.stringify({
          session_id: sessionId,
          missing: recovery.missing.map(name => {
            const hint = recovery.hints?.[name];
            return hint
              ? {
                  name,
                  purpose: hint.purpose,
                  suggested_source_type: hint.suggested_source_type,
                  suggested_source: hint.suggested_source,
                  input_schema_hint: hint.input_schema_hint,
                  output_schema_hint: hint.output_schema_hint,
                }
              : name;
          }),
        }),
      });
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 300)}`);
      }
      const data = await resp.json();
      const tasks = Array.isArray(data?.tasks) ? data.tasks : [];
      onAllMissingDispatched?.(tasks);
      // Confirm in chat so the user sees the auto-queue fired.
      setTurns(t => [...t, {
        role: "assistant",
        content:
          `Dispatched ${tasks.length} Containerizer task${tasks.length === 1 ? "" : "s"} ` +
          `for the missing node${tasks.length === 1 ? "" : "s"}. Watch progress in the Build Queue. ` +
          `Once they're all built, I'll auto-retry the apply — or click "Try again" sooner.`,
      }]);
      // Start polling the node library so we can auto-retry once
      // every missing name appears (issue #20 / task 8.4). The
      // poll runs in the useEffect below; this just sets the
      // expected list. On manual Try again or Dismiss the
      // awaitingMissing state clears.
      setAwaitingMissing(recovery.missing.slice());
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  // Auto-retry poll (issue #20 / task 8.4). While we're awaiting
  // auto-queued node landings, poll GET /node-definitions/ every
  // 15s. When every name in awaitingMissing appears in the library,
  // auto-fire retryApply once and clear the awaiting state. Caps
  // total poll runtime at ~30 minutes (120 ticks × 15s) so a hung
  // build doesn't loop forever.
  useEffect(() => {
    if (!open || !awaitingMissing || awaitingMissing.length === 0) return;
    if (!accessToken) return;
    let cancelled = false;
    let ticks = 0;
    const TICK_MS = 15_000;
    const MAX_TICKS = 120;

    const checkOnce = async () => {
      if (cancelled) return;
      ticks += 1;
      try {
        const resp = await authenticatedFetch(
          `${API_URL}/nodes/?limit=500&offset=0`,
        );
        if (!resp.ok) return;
        const list = await resp.json();
        if (!Array.isArray(list)) return;
        const presentNames = new Set(
          list.map((n: any) => String(n?.name || "")).filter(Boolean),
        );
        const stillMissing = awaitingMissing.filter((n) => !presentNames.has(n));
        if (stillMissing.length === 0 && !cancelled) {
          // Every awaited name has landed — auto-retry the apply.
          setAwaitingMissing(null);
          // Use the latest recovery snapshot via a state-setter
          // closure so we don't capture a stale value.
          setRecovery((r) => {
            if (!r) return r;
            // Fire the retry asynchronously; setRecovery returning
            // the same value avoids touching state mid-render.
            void retryApply();
            return { ...r, retryStatus: "retrying" as const };
          });
        }
      } catch {
        /* ignore — next tick retries */
      }
      if (!cancelled && ticks >= MAX_TICKS) {
        // Stop the loop. User can manually click Try again.
        setAwaitingMissing(null);
      }
    };

    // Run the first check after the tick interval, not immediately —
    // the first poll right after dispatch would always miss.
    const id = window.setInterval(checkOnce, TICK_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, awaitingMissing, accessToken]);

  return (
    // Match the new-node wizard's modal shell (centered floating Dialog
    // with a wide content card) so the two creation flows feel like one
    // system instead of two different universes. Width is roughly half
    // of the new-node wizard since the workflow wizard is a chat-style
    // single-column flow rather than a multi-step form.
    <Dialog open={open} onOpenChange={(v) => {
      if (!v) {
        // §9.4 — fire dismissed_without_action events for any audit
        // findings the user never engaged with.
        flushDismissedAuditFindings();
        testRunPollRef.current?.cancel();
        setTestRun(null);
        onClose();
      }
    }}>
      <DialogContent
        className="max-w-[900px] w-[900px] h-[680px] max-h-[92vh] flex flex-col"
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
      >
        <DialogHeader className="text-left flex-shrink-0">
          <div className="flex items-center justify-between gap-2">
            <DialogTitle className="text-left flex items-center gap-2">
              <WandSparkles className="h-5 w-5" aria-hidden="true" />
              {debugRunId != null ? `Debugging run #${debugRunId}` : wbNavLabel('wb-nav-wizard-flow', 'Workflow Wizard')}
            </DialogTitle>
            {/* Always visible so the user can blow away the current
                conversation and start fresh without closing the panel.
                The auto-reset-on-empty-reopen effect only fires on cold
                opens; this button covers the warm-reopen case. */}
            <button
              type="button"
              onClick={() => {
                // §9.4 — flush before we wipe the audit state so the
                // training signal lands.
                flushDismissedAuditFindings();
                setTurns([]);
                setInput("");
                setSessionId(null);
                setError(null);
                setRecovery(null);
                setAwaitingMissing(null);
                setTestRun(null);
                // Reset audit state too so a new conversation starts clean.
                setLatestAudit(null);
                setIgnoredFindingIds(new Set());
                setMode("chat");
              }}
              disabled={busy}
              className="text-xs text-gray-500 hover:text-gray-900 underline-offset-2 hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
              title="Start a new conversation (clears the current one)"
            >
              New conversation
            </button>
          </div>
          <DialogDescription className="text-left">
            Describe a workflow in plain English and the wizard will compose
            it from your existing nodes.
          </DialogDescription>
        </DialogHeader>

        {/* Mode tabs (canvas-aware-workflow-wizard §12). Hidden until
            the user has run at least one audit; otherwise the panel
            looks the same as today for the from-scratch / generate
            paths. Switching tabs preserves state on both sides. */}
        {latestAudit && (
          <div
            role="tablist"
            aria-label="Wizard panel mode"
            className="flex gap-1 border-b -mx-6 px-6 flex-shrink-0"
          >
            <button
              role="tab"
              type="button"
              aria-selected={mode === "chat"}
              onClick={() => setMode("chat")}
              className={
                "px-3 py-1.5 text-xs font-medium border-b-2 transition-colors " +
                (mode === "chat"
                  ? "border-indigo-600 text-indigo-700"
                  : "border-transparent text-gray-500 hover:text-gray-800")
              }
            >
              Chat
            </button>
            <button
              role="tab"
              type="button"
              aria-selected={mode === "audit"}
              onClick={() => setMode("audit")}
              className={
                "px-3 py-1.5 text-xs font-medium border-b-2 transition-colors " +
                (mode === "audit"
                  ? "border-indigo-600 text-indigo-700"
                  : "border-transparent text-gray-500 hover:text-gray-800")
              }
            >
              Audit
              {latestAudit.findings.filter(f => !ignoredFindingIds.has(f.id)).length > 0 && (
                <span className="ml-1.5 inline-flex items-center justify-center text-[10px] bg-rose-100 text-rose-800 rounded-full px-1.5 min-w-4">
                  {latestAudit.findings.filter(f => !ignoredFindingIds.has(f.id)).length}
                </span>
              )}
            </button>
          </div>
        )}

        {/* Audit-mode body (canvas-aware-workflow-wizard §12). */}
        {mode === "audit" && latestAudit && (
          <div
            role="tabpanel"
            aria-label="Audit findings"
            data-testid="audit-mode-panel"
            className="flex-1 overflow-y-auto -mx-6 px-6 py-3 space-y-3 bg-gray-50"
          >
            <p className="text-xs text-gray-700 italic">{latestAudit.summary}</p>
            {(() => {
              const visible = latestAudit.findings.filter(f => !ignoredFindingIds.has(f.id));
              if (visible.length === 0) {
                return (
                  <p className="text-xs text-gray-500 text-center py-4" data-testid="audit-empty">
                    No findings to review.
                  </p>
                );
              }
              // Group by severity: error → warning → info.
              const grouped: Record<string, typeof visible> = { error: [], warning: [], info: [] };
              for (const f of visible) {
                (grouped[f.severity] || grouped.info).push(f);
              }
              return (["error", "warning", "info"] as const).map(sev => {
                const list = grouped[sev];
                if (list.length === 0) return null;
                const sevColor = sev === "error" ? "rose" : sev === "warning" ? "amber" : "indigo";
                return (
                  <section key={sev} data-testid={`audit-section-${sev}`}>
                    <div
                      className={`text-[11px] font-semibold uppercase tracking-wide mb-1 text-${sevColor}-700`}
                    >
                      {sev} ({list.length})
                    </div>
                    <ul className="space-y-2">
                      {list.map(f => (
                        <li
                          key={f.id}
                          data-testid="audit-finding"
                          data-finding-id={f.id}
                          className="rounded-md border border-gray-200 bg-white p-2 text-xs"
                        >
                          <div className="font-medium text-gray-800 mb-1">{f.title}</div>
                          {f.detail && (
                            <div className="text-gray-600 mb-1">{f.detail}</div>
                          )}
                          {f.affected_node_ids.length > 0 && (
                            <div className="text-[10.5px] text-gray-500 mb-1.5">
                              Affects: {f.affected_node_ids.join(", ")}
                            </div>
                          )}
                          <div className="flex flex-wrap items-center gap-1.5">
                            {f.suggested_action === "fix_via_wizard" && f.fix_prompt && (
                              <button
                                type="button"
                                data-testid={`audit-action-fix-${f.id}`}
                                onClick={() => {
                                  // §9.3 — record the action signal
                                  // BEFORE the chat send so even a
                                  // failed send still lands the event.
                                  postAuditAction(f.id, f.severity, "fix_via_wizard");
                                  // Switch to Chat tab + send the fix
                                  // prompt directly. Pass it as an
                                  // override so we don't depend on
                                  // setInput state having committed.
                                  setMode("chat");
                                  void send(f.fix_prompt!);
                                }}
                                className="text-[11px] px-2 py-0.5 rounded border border-indigo-300 text-indigo-700 hover:bg-indigo-50"
                              >
                                Fix via wizard
                              </button>
                            )}
                            {f.suggested_action === "open_node" && onSelectCanvasNode && f.affected_node_ids[0] && (
                              <button
                                type="button"
                                data-testid={`audit-action-open-${f.id}`}
                                onClick={() => {
                                  postAuditAction(f.id, f.severity, "open_node");
                                  onSelectCanvasNode(f.affected_node_ids[0]);
                                  onClose();
                                }}
                                className="text-[11px] px-2 py-0.5 rounded border border-indigo-300 text-indigo-700 hover:bg-indigo-50"
                              >
                                Open node
                              </button>
                            )}
                            <button
                              type="button"
                              data-testid={`audit-action-ignore-${f.id}`}
                              onClick={() => {
                                postAuditAction(f.id, f.severity, "ignore");
                                setIgnoredFindingIds(prev => {
                                  const next = new Set(prev);
                                  next.add(f.id);
                                  return next;
                                });
                              }}
                              className="text-[11px] px-2 py-0.5 rounded text-gray-500 hover:text-gray-800 hover:underline"
                            >
                              Ignore
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </section>
                );
              });
            })()}
          </div>
        )}

        {mode === "chat" && (
        <>
        <div ref={messagesRef} className="flex-1 overflow-y-auto -mx-6 px-6 py-3 space-y-3 bg-gray-50">
        {turns.length === 0 && (
          <div className="text-sm text-gray-500 text-center py-8">
            <p className="mb-2"><strong>Describe the workflow you want to build.</strong></p>
            <p className="text-xs">
              E.g., "Resize the input image then upscale it with ESRGAN."
            </p>
            <p className="text-xs text-gray-400 mt-3">
              I'll only propose nodes from your library. Build missing nodes via
              Dashboard → Nodes → New Node first.
            </p>
          </div>
        )}

        {turns.map((t, i) => (
          <div key={i} className={
            t.role === "user"
              ? "rounded-lg bg-blue-50 border border-blue-100 px-3 py-2 text-sm text-blue-900"
              : "rounded-lg bg-white border border-gray-200 px-3 py-2 text-sm text-gray-800 shadow-sm"
          }>
            <div className="text-xs font-semibold text-gray-500 mb-1 flex items-center gap-2">
              <span>{t.role === "user" ? "You" : wbNavLabel('wb-nav-wizard-flow', 'Workflow Wizard')}</span>
              {/* The clickable inferred-intent chip was removed alongside the
                  §9 intent buttons — surfacing internal intent jargon
                  (fill_gap / audit_graph) and a "re-run with this intent"
                  affordance is exactly the kind of thought the zero-thought
                  wizard is meant to eliminate. The server still infers + uses
                  the intent; the user just never sees or steers it. */}
            </div>
            {/* Live thinking feed — visible while streaming, then
                collapsed behind a "Show thinking" toggle once the
                final event lands. Gives the user something to look at
                during the 10-60s Builder+Critic loop instead of a
                frozen panel. */}
            {t.role === "assistant" && t.thinkingLines && t.thinkingLines.length > 0 && (
              <div className="mb-2 text-xs text-gray-500">
                {t.streaming ? (
                  <div className="space-y-0.5">
                    {t.thinkingLines.map((line, k) => (
                      <div
                        key={k}
                        className="flex items-start gap-1.5 leading-snug"
                      >
                        <span className="text-gray-400 mt-[2px]">·</span>
                        <span>{line}</span>
                        {k === t.thinkingLines!.length - 1 && (
                          <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-500 mt-[6px] animate-pulse" />
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <details
                    className="group"
                    open={!!t.thinkingExpanded}
                    onToggle={(e) => {
                      const open = (e.target as HTMLDetailsElement).open;
                      setTurns(prev => {
                        if (i >= prev.length) return prev;
                        const next = [...prev];
                        next[i] = { ...next[i], thinkingExpanded: open };
                        return next;
                      });
                    }}
                  >
                    <summary className="cursor-pointer text-[11px] text-gray-400 hover:text-gray-600 select-none inline-block">
                      {t.thinkingExpanded ? "Hide thinking" : `Show thinking (${t.thinkingLines.length} steps)`}
                    </summary>
                    <div className="mt-1 space-y-0.5 pl-2 border-l border-gray-200">
                      {t.thinkingLines.map((line, k) => (
                        <div key={k} className="flex items-start gap-1.5 leading-snug">
                          <span className="text-gray-400 mt-[2px]">·</span>
                          <span>{line}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
            {t.content && <div className="whitespace-pre-wrap">{t.content}</div>}
            {/* GSEC R-022 — the wizard paused before dispatching its
                test run, waiting for the human to confirm. Inline card
                after the reply: workflow name + graph summary +
                Confirm & run / Not now. */}
            {t.role === "assistant" && t.needsConfirm && (
              <div
                role="group"
                aria-label={`Confirm workflow "${t.needsConfirm.workflowName}" before running`}
                data-testid="wizard-confirm-card"
                className="mt-3 rounded-md border-2 border-indigo-300 bg-indigo-50 p-3"
              >
                <div className="text-sm font-semibold text-indigo-900 mb-1">
                  Confirm before running
                </div>
                <p className="text-xs text-indigo-900/80 mb-2">
                  The wizard stopped before dispatching a test run of
                  <strong> {t.needsConfirm.workflowName}</strong> —{" "}
                  {t.needsConfirm.nodeCount} node{t.needsConfirm.nodeCount === 1 ? "" : "s"},{" "}
                  {t.needsConfirm.edgeCount} edge{t.needsConfirm.edgeCount === 1 ? "" : "s"}.
                </p>
                {t.needsConfirm.nodeNames.length > 0 && (
                  <ul className="mb-2 text-[11px] text-indigo-900/80 space-y-0.5" aria-label="Workflow nodes">
                    {t.needsConfirm.nodeNames.map((n, k) => (
                      <li key={k}>· {n}</li>
                    ))}
                  </ul>
                )}
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => void send(t.needsConfirm!.lastUserMessage, { confirmed: true })}
                    disabled={busy}
                    className="bg-indigo-600 hover:bg-indigo-700 text-white"
                  >
                    Confirm &amp; run
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      setTurns(prev => {
                        if (i >= prev.length) return prev;
                        const next = [...prev];
                        next[i] = { ...next[i], needsConfirm: null };
                        return next;
                      })
                    }
                    disabled={busy}
                    className="text-gray-700"
                  >
                    Not now
                  </Button>
                </div>
              </div>
            )}
            {/* When the wizard asks for a sample input to test against, give the
                user a big, obvious upload button right here in the reply — not a
                tiny icon they'll never find. Uploads into their library so the
                next "test" picks it up. */}
            {t.role === "assistant" && t.content &&
             /sample input/i.test(t.content) && /upload/i.test(t.content) && (
              <div className="mt-3">
                <Button
                  type="button"
                  onClick={uploadTestInput}
                  disabled={busy || inputUpload.state === "uploading"}
                  className="w-full justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white"
                >
                  <Upload className="h-4 w-4" />
                  {inputUpload.state === "uploading" ? "Uploading…" : "Upload a file to test with"}
                </Button>
                {inputUpload.state === "done" && (
                  <p className="text-xs text-green-600 mt-1.5">✓ “{inputUpload.name}” added — running the test now…</p>
                )}
                {inputUpload.state === "error" && (
                  <p className="text-xs text-red-600 mt-1.5">Upload failed{inputUpload.msg ? `: ${inputUpload.msg}` : ""}</p>
                )}
              </div>
            )}
            {/* workflow-wizard-missing-node-handoff — eager surface
                inline below the chat reply. One card per placeholder
                node in the proposal. "Build it now" opens the New Node
                Wizard pre-filled so the user doesn't have to re-enter
                the problem. */}
            {t.role === "assistant" && t.missingNodes && t.missingNodes.length > 0 && (
              <div className="mt-3 border-t border-gray-100 pt-2 space-y-2">
                <div className="text-xs font-medium text-gray-700">
                  This workflow needs {t.missingNodes.length === 1 ? "one new node" : `${t.missingNodes.length} new nodes`} you don't have yet:
                </div>
                {t.missingNodes.map((hint) => (
                  <div
                    key={hint.placeholder_id}
                    className="rounded-md border border-amber-200 bg-amber-50 p-2.5 space-y-1.5"
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <div className="text-sm font-semibold text-amber-900 truncate">
                        {hint.label}
                      </div>
                      <span className="text-[10px] uppercase tracking-wide text-amber-700 font-medium shrink-0">
                        {hint.suggested_source_type}
                      </span>
                    </div>
                    {hint.purpose && (
                      <div className="text-xs text-amber-800">{hint.purpose}</div>
                    )}
                    {hint.suggested_source && (
                      <div className="text-[11px] text-amber-700 font-mono truncate" title={hint.suggested_source}>
                        {hint.suggested_source}
                      </div>
                    )}
                    <div className="pt-1">
                      <Button
                        size="sm"
                        className="bg-amber-600 hover:bg-amber-700 text-white"
                        onClick={() => onBuildMissingNode?.(hint.label, hint)}
                      >
                        Build it now
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {/* workflow-agentic-execution-loop §G + §H — node-wizard
                handoff cards. Server emits one spec per missing node
                with a prefilled URL the user clicks → land in the node
                wizard with name/IO/process_type/base image already
                populated. Distinct from the legacy `missingNodes` cards
                above: those are LLM-generated placeholders from the
                wizard's own response envelope; these are server-inferred
                from validator findings. */}
            {t.role === "assistant" && (() => {
              // Dedupe against the LLM `missingNodes` cards above. The SAME
              // missing node is often surfaced by BOTH paths (LLM placeholder
              // envelope + server-inferred handoff), which rendered two cards
              // for one node ("build it 2ce"). Show a handoff card only for
              // nodes NOT already covered by a missingNodes card above.
              const _llmNames = new Set(
                (t.missingNodes || []).map((h) => (h.label || "").trim().toLowerCase())
              );
              const _handoffs = (t.nodeBuildHandoffs || []).filter(
                (s) => !_llmNames.has((s.node_name || "").trim().toLowerCase())
              );
              if (_handoffs.length === 0) return null;
              return (
              <div className="mt-3 border-t border-gray-100 pt-2 space-y-2">
                <div className="text-xs font-medium text-gray-700">
                  The wizard wants to use {_handoffs.length === 1 ? "a node" : `${_handoffs.length} nodes`} that {_handoffs.length === 1 ? "doesn't" : "don't"} exist yet — build {_handoffs.length === 1 ? "it" : "them"} first:
                </div>
                {_handoffs.map((spec) => (
                  <div
                    key={`handoff-${spec.node_name}`}
                    className="rounded-md border border-blue-200 bg-blue-50 p-2.5 space-y-1.5"
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <div className="text-sm font-semibold text-blue-900 truncate">
                        {spec.node_name}
                      </div>
                      <span className="text-[10px] uppercase tracking-wide text-blue-700 font-medium shrink-0">
                        {spec.process_type}{spec.input_type && spec.output_type ? ` · ${spec.input_type}→${spec.output_type}` : ""}
                      </span>
                    </div>
                    <div className="text-xs text-blue-800">{spec.description}</div>
                    {spec.suggested_wrap_base_image && (
                      <div className="text-[11px] text-blue-700 font-mono truncate" title={spec.suggested_wrap_base_image}>
                        base: {spec.suggested_wrap_base_image}
                      </div>
                    )}
                    <div className="pt-1">
                      <Button
                        size="sm"
                        className="bg-blue-600 hover:bg-blue-700 text-white"
                        onClick={() => {
                          // workflow-agentic-execution-loop §H — open
                          // the New Node Wizard modal directly via the
                          // existing handoff callback. The prefill_url
                          // is a fallback for non-WorkflowBuilder
                          // surfaces; in this panel the callback is
                          // always wired so we never need to navigate.
                          if (onBuildMissingNode) {
                            const hint: MissingNodeHint = {
                              placeholder_id: spec.node_name,
                              label: spec.node_name,
                              purpose: spec.description || spec.rationale,
                              suggested_source_type: "describe",
                              suggested_source: spec.description || spec.rationale || spec.node_name,
                              input_schema_hint: spec.input_type ? { data: spec.input_type } : {},
                              output_schema_hint: spec.output_type ? { data: spec.output_type } : {},
                            };
                            onBuildMissingNode(spec.node_name, hint);
                          } else if (spec.prefill_url) {
                            window.location.assign(spec.prefill_url);
                          }
                        }}
                      >
                        Build this missing node
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
              );
            })()}

            {/* workflow-agentic-execution-loop §7.7 — tested-lineage
                chip + best-effort sub-label. Surface only on agent-loop
                turns (legacy pipeline emits neither). */}
            {t.role === "assistant" && t.testedLineage && (t.testedLineage.test_run_ids ?? []).length > 0 && (
              <div className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-green-50 border border-green-200 px-2.5 py-0.5 text-[11px] font-medium text-green-800">
                <span aria-hidden="true">✓</span>
                <span>
                  Verified against {(t.testedLineage.test_run_ids ?? []).length === 1
                    ? "1 test run"
                    : `${(t.testedLineage.test_run_ids ?? []).length} test runs`} ({t.testedLineage.iteration_count ?? 0} {(t.testedLineage.iteration_count ?? 0) === 1 ? "iteration" : "iterations"})
                </span>
              </div>
            )}

            {t.role === "assistant" && t.bestEffort && (
              <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                <span className="font-semibold">Best-effort proposal</span> — the wizard couldn't fully verify this against a test input within the iteration budget. Review the graph before applying.
              </div>
            )}

            {/* WSIP 4.6a — original-vs-fixed run outputs, captured from the
                turn's fix_verification_succeeded SSE event (debug mode only;
                the panel only sets debugCompare when BOTH arrays validated). */}
            {t.role === "assistant" && t.debugCompare && (
              <div className="mt-2">
                <DebugOutputCompare
                  original={t.debugCompare.original}
                  candidate={t.debugCompare.candidate}
                  title="Original vs fixed output"
                />
              </div>
            )}

            {/* EXPR 5.7 — "Why this workflow?" rationale disclosure.
                Rendered below the proposal card; the component is
                collapsed by default and handles its own validation. */}
            {t.role === "assistant" && (
              <div className="mt-3">
                <WizardRationale
                  rationale={t.rationale}
                  onTryDifferent={(prompt) => void sendRef.current(prompt)}
                />
              </div>
            )}

            {t.proposal && (
              <div
                className={
                  "mt-3 border-t pt-2 " +
                  (t.bestEffort ? "border-amber-300 border-2 rounded-md p-2 bg-amber-50/30" : "border-gray-100")
                }
              >
                <div className="text-xs font-medium text-gray-700 mb-1">
                  {t.proposal.apply_mode === "merge" ? "Merge into canvas: " : "Proposed: "}{t.proposal.name}
                </div>
                {/* Canvas-aware-workflow-wizard §10/§11 — Added /
                    Removed / Anchors for merge proposals (or per-
                    component toggles when the proposal partitions
                    into 2+ disjoint changes); flat list for replace.
                    Anchor labels resolve via the live canvas. */}
                <WizardProposalDiff
                  proposal={t.proposal}
                  canvas={getCanvas ? getCanvas() : null}
                  acceptedComponentIds={acceptedComponentsByTurn[i]}
                  onToggleComponent={(cid) => toggleComponent(i, cid)}
                />
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    onClick={() => t.proposal && apply(t.proposal, i)}
                    disabled={busy ||
                      // Disable when ALL components have been toggled
                      // off — there's nothing to apply (§11).
                      (acceptedComponentsByTurn[i] !== undefined &&
                       acceptedComponentsByTurn[i].size === 0)}
                    className="bg-indigo-600 hover:bg-indigo-700 text-white"
                  >
                    {t.proposal.apply_mode === "merge" ? "Merge into canvas" : "Apply to canvas"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      // #2 / launch-day-followup-2 — Revise replaces
                      // Cancel. Pre-fills the chat input with a starter
                      // question so the user can describe what they
                      // wanted differently. The wizard's next turn sees
                      // the original proposal in chat history + the
                      // user's revision request, and the Builder+Critic
                      // loop produces a new proposal.
                      setInput(
                        "Please revise this workflow — what I'd like is: ",
                      );
                      // Focus the textarea on next tick (after state
                      // flush). Using a query selector keeps this
                      // independent of the input ref naming.
                      setTimeout(() => {
                        const el = document.querySelector<HTMLInputElement>(
                          "#wizard-chat-input",
                        );
                        if (el) {
                          el.focus();
                          // Place cursor at end so the user types after
                          // the prefilled prompt.
                          const len = el.value.length;
                          el.setSelectionRange(len, len);
                        }
                      }, 0);
                    }}
                    disabled={busy}
                    className="text-gray-700"
                  >
                    Revise
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={dispatchTestRun}
                    disabled={busy || !!testRun}
                    title="Run this workflow against a sample input to verify it works before applying"
                    className="text-green-700 border-green-300 hover:bg-green-50"
                  >
                    {testRun?.status === "dispatching" ? "Dispatching…"
                      : testRun?.status === "QUEUED" || testRun?.status === "RUNNING" ? "Testing…"
                      : "Test this workflow"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={uploadTestInput}
                    disabled={busy || inputUpload.state === "uploading"}
                    title="Upload a file to test this workflow against — it's added to your library, then click Test"
                    className="text-gray-700"
                  >
                    {inputUpload.state === "uploading" ? "Uploading…" : "Upload test input"}
                  </Button>
                </div>
                {inputUpload.state !== "idle" && (
                  <div className={
                    "mt-2 rounded-md px-2.5 py-1.5 text-xs " +
                    (inputUpload.state === "done" ? "bg-green-50 border border-green-200 text-green-800"
                      : inputUpload.state === "error" ? "bg-red-50 border border-red-200 text-red-800"
                      : "bg-blue-50 border border-blue-200 text-blue-800")
                  }>
                    {inputUpload.state === "uploading" && `Uploading “${inputUpload.name}” to your library…`}
                    {inputUpload.state === "done" && `✓ “${inputUpload.name}” added to your library — click “Test this workflow” to run against it.`}
                    {inputUpload.state === "error" && `Upload failed${inputUpload.msg ? `: ${inputUpload.msg}` : ""}`}
                  </div>
                )}
                {testRun && (
                  <div className={
                    "mt-2 rounded-md px-2.5 py-1.5 text-xs flex items-center gap-2 " +
                    (testRun.status === "COMPLETED"
                      ? "bg-green-50 border border-green-200 text-green-800"
                      : testRun.status === "FAILED" || testRun.status === "CANCELLED" || testRun.status === "TIMEOUT"
                      ? "bg-red-50 border border-red-200 text-red-800"
                      : "bg-blue-50 border border-blue-200 text-blue-800")
                  }>
                    {(testRun.status === "dispatching" || testRun.status === "QUEUED" || testRun.status === "RUNNING") && (
                      <span className="inline-block w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
                    )}
                    {testRun.status === "dispatching" && "Dispatching test run…"}
                    {testRun.status === "QUEUED" && "Test run queued — waiting for a worker…"}
                    {testRun.status === "RUNNING" && "Test run in progress…"}
                    {testRun.status === "COMPLETED" && (
                      <><span aria-hidden="true">✓</span> Test run passed — workflow works against a sample input</>
                    )}
                    {testRun.status === "FAILED" && (
                      <><span aria-hidden="true">✗</span> Test run failed{testRun.error ? `: ${testRun.error}` : ""}</>
                    )}
                    {testRun.status === "CANCELLED" && "Test run was cancelled"}
                    {testRun.status === "TIMEOUT" && "Test run timed out"}
                    {(testRun.status === "COMPLETED" || testRun.status === "FAILED" || testRun.status === "CANCELLED" || testRun.status === "TIMEOUT") && (
                      <button
                        type="button"
                        onClick={() => setTestRun(null)}
                        className="ml-auto text-[10px] underline-offset-2 hover:underline opacity-70 hover:opacity-100"
                      >
                        dismiss
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}

        {busy && (
          <div className="text-xs italic text-gray-500">Wizard is thinking…</div>
        )}
        {recovery && (
          <div className="rounded-md bg-amber-50 border border-amber-300 px-3 py-2 text-xs text-amber-900 space-y-2">
            <div className="font-semibold">
              Can't apply yet — {recovery.missing.length === 1 ? "this node" : "these nodes"} {recovery.missing.length === 1 ? "doesn't" : "don't"} exist in your library:
            </div>
            <ul className="space-y-1">
              {recovery.missing.map((name) => (
                <li key={name} className="flex items-center gap-2">
                  <code className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-900 font-mono">{name}</code>
                  {onBuildMissingNode && (
                    <button
                      type="button"
                      onClick={() => onBuildMissingNode(name)}
                      disabled={busy}
                      className="text-[10px] text-amber-800 bg-amber-100 hover:bg-amber-200 border border-amber-300 rounded px-1.5 py-0.5 disabled:opacity-50"
                      title="Open the Node Wizard with this name pre-filled. After it builds, click Try again to apply."
                    >
                      Build this node
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => navigator.clipboard?.writeText(name)}
                    className="text-[10px] text-amber-700 hover:text-amber-900 underline-offset-2 hover:underline"
                    title="Copy node name to clipboard"
                  >
                    copy
                  </button>
                </li>
              ))}
            </ul>
            <p className="text-[11px] leading-snug">
              Build {recovery.missing.length === 1 ? "it" : "them"} via <strong>Dashboard → Nodes → New Node</strong> (or
              the <strong>+ New Node</strong> button on the workflow canvas), then click <strong>Try again</strong>.
            </p>
            {recovery.retryStatus === "still-missing" && (
              <p className="text-[11px] text-amber-800 italic">
                Same nodes are still missing — finish building them first, then retry.
              </p>
            )}
            {awaitingMissing && awaitingMissing.length > 0 && (
              <p className="text-[11px] text-amber-800 italic flex items-center gap-1">
                <span className="inline-block w-2 h-2 rounded-full bg-amber-600 animate-pulse" />
                Auto-retry will fire as soon as every node lands in your library.
              </p>
            )}
            <div className="flex items-center gap-2 pt-1 flex-wrap">
              <Button
                size="sm"
                onClick={retryApply}
                disabled={busy}
                className="bg-amber-600 hover:bg-amber-700 text-white text-xs h-7"
              >
                {recovery.retryStatus === "retrying" ? "Retrying…" : "Try again"}
              </Button>
              {onAllMissingDispatched && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={dispatchAllMissing}
                  disabled={busy}
                  className="text-xs h-7 border-amber-300 text-amber-800 hover:bg-amber-100"
                  title="Dispatch a Containerizer task per missing name. The LLM generates a best-guess implementation; you review + edit each one in the Node Wizard before Confirm and Build. Quality varies — for complex nodes, prefer Build this node + manual editing."
                >
                  Build all missing nodes
                </Button>
              )}
              <button
                type="button"
                onClick={dismissRecovery}
                disabled={busy}
                className="text-[11px] text-amber-700 hover:text-amber-900 underline-offset-2 hover:underline disabled:opacity-50"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
        {error && !recovery && (
          <div role="alert" className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-800">
            {error}
          </div>
        )}
      </div>

        {/* Intent mode-buttons removed by design — "0 thoughts, all intent".
            The user just describes what they want; the server infers the
            intent (_infer_wizard_intent: empty canvas → from_scratch, verbs +
            selection → the right co-pilot mode, LLM fallback for ambiguity).
            No mode selection is surfaced to the user. */}

        <form
          className="pt-3 border-t bg-white flex gap-2 flex-shrink-0"
          onSubmit={(e) => { e.preventDefault(); send(); }}
        >
          <input
            id="wizard-chat-input"
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Describe the workflow you want…"
            className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
            disabled={busy}
            autoComplete="off"
          />
          <Button type="button" size="sm" variant="outline"
                  onClick={uploadTestInput}
                  disabled={busy || inputUpload.state === "uploading"}
                  title="Upload a file to test this workflow against — it's added to your library, then ask the wizard to test again"
                  className="text-gray-600 border-gray-300">
            <Upload className="h-4 w-4" />
          </Button>
          <Button type="submit" size="sm" aria-label="Send message" disabled={busy || !input.trim()}
                  className="bg-indigo-600 hover:bg-indigo-700 text-white">
            <Send className="h-4 w-4" />
          </Button>
        </form>
        {inputUpload.state !== "idle" && (
          <p className={"text-[11px] pt-1 flex-shrink-0 " +
            (inputUpload.state === "done" ? "text-green-600"
              : inputUpload.state === "error" ? "text-red-600" : "text-gray-500")}>
            {inputUpload.state === "uploading" && `Uploading “${inputUpload.name}” to your library…`}
            {inputUpload.state === "done" && `✓ “${inputUpload.name}” added — running the test now…`}
            {inputUpload.state === "error" && `Upload failed${inputUpload.msg ? `: ${inputUpload.msg}` : ""}`}
          </p>
        )}
        <p className="text-[11px] text-gray-400 pt-1 flex-shrink-0">
          First reply: 30–60s · Subsequent: 1–3s · Per-user node visibility
        </p>
        </>
        )}
      </DialogContent>
    </Dialog>
  );
};

export default WorkflowWizardPanel;
