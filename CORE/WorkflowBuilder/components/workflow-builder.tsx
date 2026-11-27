// File Browser - Minimal v1
"use client";
import { apiUrl } from "@/lib/runtime-config";

import { getApiUrl, getDashboardUrl } from "@/lib/urls";

import React, { useState, useEffect, useCallback, useRef } from "react";
import ReactFlow, { ReactFlowProvider, Background, Controls, MiniMap, Panel, Node, NodeMouseHandler } from "reactflow";
import "reactflow/dist/style.css";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Loader2 } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

// Custom Hooks
import { useWorkflowState } from "@/hooks/useWorkflowState";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";
import { useIdleWarning } from "@/hooks/use-idle-warning";
import { IdleWarningDialog } from "@/components/IdleWarningDialog";
import { useOpenWorkflowFromQuery } from "@/hooks/useOpenWorkflowFromQuery";
import { useWorkflowEvents } from "@/hooks/useWorkflowEvents";
import { useNodeStackBindings } from "@/hooks/useNodeStackBindings";
import { useConversationalDebugFlag } from "@/hooks/use-conversational-debug-flag";
import { useEntitlements } from "@/hooks/use-entitlements";
import { NodeStackBindingsProvider } from "@/lib/node-stack-bindings";
import { useBreakpoints, breakpointMenuLabel } from "@/lib/use-breakpoints";
import { authenticatedFetch } from "@/lib/api-client";
import { loadEntitlements } from "@/lib/entitlements";
import { dispatchWorkflowHotkey } from "@/lib/workflow-hotkeys";
import { copySelection, buildPaste, type ClipboardSnapshot } from "@/lib/workflow-clipboard";
import { startSignInAgain } from "@/lib/sign-in-again";
import {
  WB_CANVAS_LIVE_REGION_ID,
  describeRunStatusChange,
  describeNodeAdded,
  describeNodesDeleted,
  describeEdgeConnected,
  describeTerminalRunStatus,
  diffStatuses,
} from "@/lib/canvas-a11y";
import { toast } from "@/components/ui/use-toast";

// UI Components
import NodeLibrary from "./node-library";
import NewNodeWizard, { clearPersistedWizardState } from "./new-node-wizard";
import BuildQueuePanel from "./build-queue-panel";
import ConnectionLostBanner from "./connection-lost-banner";
import TrialBanner from "./TrialBanner";
import type { BuildQueueEntry } from "./build-queue-row";
import { applyTransitionToBuild } from "@/lib/build-progress";
import { parseWizardQueueHash } from "@/lib/wizard-queue";
import NodeConfigPanel from "./node-config-panel";
import { WorkflowToolbar } from "./WorkflowToolbar";
import NotificationsBell from "./notifications-bell";
import ThemeToggleButton from "./theme-toggle-button";
import AssistantLauncher from "./assistant-launcher";
import AssistantPanel from "./assistant-panel";
import {
  deriveBuildQueueUserId,
  buildQueueStorageKey,
  LEGACY_GLOBAL_BUILDS_KEY,
  OLD_SINGLE_BUILD_KEY,
} from "@/lib/build-queue-namespace";
import { WorkflowDialogs } from "./WorkflowDialogs";
import { WorkflowWizardPanel } from "./workflow-wizard-panel";
// CE: the Project Wizard is pulled from the builder in CE - no panel import ()
// CE: the Stack Wizard is pulled from the builder in CE () - no panel import
import { serialiseCanvasForWizard } from "@/lib/wizard-canvas";
import { Sparkles } from "lucide-react";
import { Command, CommandInput, CommandList, CommandEmpty, CommandItem } from "@/components/ui/command";
import { FileBrowser } from "./file-browser";
import { ControlledBanner, DataLabelChip, worstLabel } from "./data-label";
import { PriorityClassChip } from "./priority-class-chip";
import { DataMarkingBanner } from "./data-marking-banner";
import { ExtractionSuggestionsBanner } from "./extraction-suggestions-banner";
import { WorkflowQueueDialog } from "./WorkflowQueueDialog";
import { PendingApprovalsDialog } from "./PendingApprovalsDialog";
import FanoutModal from "./FanoutModal";
import NodeVersionsModal from "./NodeVersionsModal";
import { TriggersDialog } from "./TriggersDialog";
// CE: the store dialog is denied in CE () - no dialog import
import BlockedRunPanel from "./workflow/BlockedRunPanel";
import SealedAppForm from "./workflow/SealedAppForm";
import FrameInspectorPanel from "./workflow/FrameInspectorPanel";

// Theme map (OZ-E1-WB-TOTO: nav/titles read from product-names.js)
import { themeLabel, themeEnabled, wbNavLabel, PRODUCT_NAMES } from "@/lib/product-names";

// Node and Edge Types
import { InputNode } from "./nodes/input-node";
import { OutputNode } from "./nodes/output-node";
import { ProcessNode } from "./nodes/process-node";
import { ConditionalNode } from "./nodes/conditional-node";
import { CodeNode } from "./nodes/code-node";
import { IfElseNode } from "./nodes/if-else-node";
import { SwitchNode } from "./nodes/switch-node";
import { SplitNode } from "./nodes/split-node";
import CustomEdge from "./custom-edge";
import type { NodeDefinition, WorkflowNode } from "@/lib/types";

const nodeTypes = {
  input: InputNode, output: OutputNode, process: ProcessNode, conditional: ConditionalNode,
  code: CodeNode, "if-else": IfElseNode, switch: SwitchNode, split: SplitNode,
};
const edgeTypes = { custom: CustomEdge };

type NodeDefinitionUI = Partial<NodeDefinition> & { nodeType: string };

// Node-library sidebar resize bounds.
//   - 280px floor: long node names + the Functions dropdown truncate below this.
//   - 720px ceiling: keeps the canvas usable on a ~1280px laptop.
//   - default 420px matches the historical fixed width before the handle landed.
const SIDEBAR_MIN_WIDTH = 280;
const SIDEBAR_MAX_WIDTH = 720;
const SIDEBAR_DEFAULT_WIDTH = 420;
const SIDEBAR_STORAGE_KEY = "wb_node_library_width";

const localNodes: NodeDefinitionUI[] = [
    { id: 999, name: "If/Else", nodeType: "if-else", type: "Operators", description: "Branch based on a true/false condition." },
    { id: 998, name: "Switch", nodeType: "switch", type: "Operators", description: "Branch based on multiple cases." },
    { id: 997, name: "Split", nodeType: "split", type: "Operators", description: "Split execution into parallel paths." }
];

const WorkflowBuilderContent = () => {
  // SUPPORTCHAT-CE-4 — the whole support package ships in Community Edition
  // (owner 11-05 Q-SC1): the 2400UI125 / rc20-batch fix-s19 edition carves that
  // hid the Project Wizard and Workflow Wizard openers are REMOVED, so the
  // openers, panels and the artifact redirect behave identically on CE and
  // paid. The edition is still read here (fail-open, unresolved = paid
  // behaviour) for the other gates below; nothing carves the wizards off it.
  const { edition: entitlementsEdition } = useEntitlements();

  // Harbor Configuration Check
  const [harborStatus, setHarborStatus] = useState<{
    loading: boolean;
    configured: boolean;
    isPlatformAdmin: boolean;
    tenantId: number;
    // 1900Y1p3 — CE carve: CE ships no Harbor by design, so the
    // unconfigured-Harbor block screen must not render there. This flag
    // comes from the /platform/features entitlements.edition the UI already
    // reads (lib/entitlements loadEntitlements, same predicate as the
    // Dashboard's _isCeEdition); false on any failure = paid behaviour.
    ceEdition: boolean;
    error?: string;
  }>({ loading: true, configured: false, isPlatformAdmin: false, tenantId: 1, ceEdition: false });

  // Worker Availability Check
  //
  // Guard against false-positive "no workers" banners: the worker's
  // heartbeat lives on a short (seconds) cadence, so a single probe
  // can race — especially right after API restart or hard refresh.
  // We require TWO consecutive zero-worker results before we flip
  // hasWorkers to false; in the meantime the UI stays in its optimistic
  // "we have workers" state so the wizard remains usable.
  const [workerStatus, setWorkerStatus] = useState<{
    loading: boolean;
    checked: boolean;
    hasWorkers: boolean;
    onlineCount: number;
    licensedCount: number;
    totalCount: number;
    /** Workers visible to the tenant but marked worker_offline (stale heartbeat / disconnected) */
    offlineCount: number;
    error?: string;
    /** # of consecutive checks that returned zero workers. Used as
     *  a debounce so a single transient miss doesn't flash the banner. */
    consecutiveMisses: number;
  }>({
    loading: false,
    checked: false,
    hasWorkers: false,
    onlineCount: 0,
    licensedCount: 0,
    totalCount: 0,
    offlineCount: 0,
    consecutiveMisses: 0,
  });

  // Core State Management
  const {
    nodes, setNodes, onNodesChange, edges, setEdges, onEdgesChange, selectedNode, setSelectedNode,
    // Undo/redo history (see hooks/useWorkflowState.ts).
    commit, commitConfig, beginNodeDrag, endNodeDrag, undo, redo, canUndo, canRedo, resetHistory,
  } = useWorkflowState();
  const [fileStore, setFileStore] = useState<{ [nodeId: string]: File }>({});

  // R-038 — screen-reader announcements. The builder renders one visually-
  // hidden aria-live region (id WB_CANVAS_LIVE_REGION_ID) and pushes short
  // sentences into `canvasLiveMessage`; the previous sentence is kept in the
  // ref so a rapid double-push re-announces (aria-live ignores identical text).
  const [canvasLiveMessage, setCanvasLiveMessage] = useState("");
  const canvasLiveSeq = useRef(0);
  const announceCanvas = useCallback((message: string) => {
    if (!message) return;
    canvasLiveSeq.current += 1;
    setCanvasLiveMessage(`${message} ${canvasLiveSeq.current}`.trim());
  }, []);
  const [allNodeDefs, setAllNodeDefs] = useState<NodeDefinitionUI[]>([]);
  const [isFileBrowserOpen, setIsFileBrowserOpen] = useState(false);
  // GY.C7 — the open workflow's effective data-classification label
  // (derived server-side from its most recent run's input cogs) + the
  // labels of the assets the user has selected into nodes. Worst-of the
  // two drives the top "CONTROLLED: <LABEL> — handle per policy" banner.
  const [workflowLabel, setWorkflowLabel] = useState<string | null>(null);
  const [selectedAssetLabels, setSelectedAssetLabels] = useState<string[]>([]);
  const [fileBrowserContext, setFileBrowserContext] = useState<{
    nodeId: string | null;
    inputType?: string;
    allowMultiple?: boolean;
    isMetadata?: boolean;
  }>({ nodeId: null });
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [showBackConfirmation, setShowBackConfirmation] = useState(false);
  // LDF-3 §2.2 — confirm dialog for `+ New` when canvas has unsaved edits.
  const [showNewConfirmation, setShowNewConfirmation] = useState(false);
  // Tracks which run-blocked items (secret:<name> / file:<id>) have been
  // requested, so the dialog buttons can flip to "Requested ✓".
  const [requestedRunItems, setRequestedRunItems] = useState<Set<string>>(new Set());
  const [isQueueDialogOpen, setIsQueueDialogOpen] = useState(false);
  const [isApprovalsOpen, setIsApprovalsOpen] = useState(false);
  // The paused node on the canvas opens this dialog by event rather than by a
  // prop drilled through ReactFlow's node renderer, which does not take
  // arbitrary callbacks. Matches the codebase's existing cross-component
  // pattern (mod:queue:expand-row, mod:nodes:changed).
  useEffect(() => {
    const open = () => setIsApprovalsOpen(true);
    window.addEventListener('mod:approvals:open', open);
    return () => window.removeEventListener('mod:approvals:open', open);
  }, []);
  // Fan-out inspector — opened by the "FAN ×N" badge on a fanned edge
  // (custom-edge.tsx dispatches mod:open-fanout). Same event convention as
  // the approvals dialog above.
  const [fanoutNodeId, setFanoutNodeId] = useState<string | null>(null);
  // node-run-controls — DAG schedule pause toggle (workflow-level).
  const [schedulePaused, setSchedulePaused] = useState(false);
  // node-run-controls — "Output versions" modal target (keep + version).
  const [versionsNodeId, setVersionsNodeId] = useState<string | null>(null);
  useEffect(() => {
    const open = (e: Event) => {
      const detail = (e as CustomEvent<{ nodeId?: string }>).detail;
      if (detail?.nodeId) setFanoutNodeId(detail.nodeId);
    };
    window.addEventListener('mod:open-fanout', open);
    return () => window.removeEventListener('mod:open-fanout', open);
  }, []);
  const [isWizardPanelOpen, setIsWizardPanelOpen] = useState(false);
  // CE: the Project Wizard is pulled from the builder in CE - no panel open state ()
  // CE: the Stack Wizard is pulled from the builder in CE () - no panel state
  const [triggersOpen, setTriggersOpen] = useState(false);
  // CE: the store dialog is denied in CE () - no popup state
  // Marketplace is for tenant admins (parent_user_id == null) and platform
  // admins only — mirrors the old Dashboard nav gating. Non-admins never see
  // the functions-menu button.
  const [canMarketplace, setCanMarketplace] = useState(false);
  // FX.163 p3b4b — tenant-admin / platform-admin verdict for the data-marking
  // banner's "Clear" button (DELETE /v1/tenant/data-markings/... requires
  // manage_settings). Resolved from the same cached /me read as canMarketplace.
  const [canClearMarking, setCanClearMarking] = useState(false);
  // Stack Wizard authoring is gated on the platform-only `stack:author`
  // capability — the entire /persistent-stacks router requires it. Tenant
  // users without the grant used to see the menu entry and then hit a wall
  // of 403s ("can't get anything stack related to work"), so we probe the
  // capability and HIDE the entry when it's absent (mega-qa-hardening).
  const [canStackAuthor, setCanStackAuthor] = useState(false);
  const [isLegendExpanded, setIsLegendExpanded] = useState(true);
  const [recommendedNodes, setRecommendedNodes] = useState<NodeDefinitionUI[]>([]);
  const [isLoadingNodes, setIsLoadingNodes] = useState(true);

  // Sidebar width. Starts at the SSR-safe default; hydrates from
  // localStorage on mount so a returning user lands on their preferred
  // size without a render mismatch.
  const [sidebarWidth, setSidebarWidth] = useState<number>(SIDEBAR_DEFAULT_WIDTH);
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
      if (!stored) return;
      const parsed = parseInt(stored, 10);
      if (!Number.isFinite(parsed)) return;
      setSidebarWidth(Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, parsed)));
    } catch {
      // localStorage unavailable (private mode, disabled storage) — fall back to default.
    }
  }, []);
  const handleSidebarResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = sidebarWidth;
    let finalWidth = startWidth;
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(
        SIDEBAR_MAX_WIDTH,
        Math.max(SIDEBAR_MIN_WIDTH, startWidth + (ev.clientX - startX)),
      );
      finalWidth = next;
      setSidebarWidth(next);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      try {
        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(finalWidth));
      } catch {
        // Ignore — width still applies for this session.
      }
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [sidebarWidth]);

  // Check if nodes have been loaded before using cookie
  const hasLoadedNodesBefore = () => {
    if (typeof document === 'undefined') return false;
    return document.cookie.includes('workflowNodesLoaded=true');
  };

  const setNodesLoadedCookie = () => {
    if (typeof document === 'undefined') return;
    // Set cookie that expires in 30 days
    const expires = new Date();
    expires.setDate(expires.getDate() + 30);
    document.cookie = `workflowNodesLoaded=true; expires=${expires.toUTCString()}; path=/`;
  };

  // API and Side Effects Hook
  const api = useWorkflowApi(nodes, edges, setNodes, setEdges, fileStore, resetHistory);

  // WSIP 4.5b — RunRail's "Why did this happen?" explain button is a tenant
  // flag (GET /wsip/flags). Fail-closed: hidden until the flag says true.
  const debugEnabled = useConversationalDebugFlag(getApiUrl(), api.accessToken);

  // GSEC FX.54 p4b — SSO idle-warning dialog at deadline-2min (deadline = the
  // Keycloak refresh-token `exp`, the server-side idle timeout). The hook owns
  // the schedule; this state is just the countdown the component renders.
  const [idleOpen, setIdleOpen] = useState(false);
  const [idleSeconds, setIdleSeconds] = useState(0);
  const idleWarning = useIdleWarning(api.accessToken, {
    onOpen: (secondsLeft) => { setIdleSeconds(secondsLeft); setIdleOpen(true); },
    onTick: (secondsLeft) => setIdleSeconds(secondsLeft),
    onClose: () => setIdleOpen(false),
  });

  // GY.C7 — fetch the open workflow's effective data-classification label
  // (server derives it from the workflow's most recent run's input cogs).
  // One fetch per workflow load, no polling: the label only changes when a
  // new run is triggered with differently-labelled inputs. Fail-OPEN — a
  // fetch/parse hiccup clears the marking, never blocks the builder.
  const wfLabelKey = `${api.currentWorkflow?.id ?? ""}:${api.currentVersionNumber ?? ""}`;
  useEffect(() => {
    let cancelled = false;
    if (!api.accessToken || !api.currentWorkflow?.id) {
      setWorkflowLabel(null);
      return;
    }
    (async () => {
      try {
        const resp = await authenticatedFetch(
          `${apiUrl()}/road/${api.currentWorkflow?.id}`,
          { headers: { Authorization: `Bearer ${api.accessToken}`, Accept: "application/json" } },
        );
        if (!resp.ok) return;
        const wf = await resp.json();
        if (!cancelled) setWorkflowLabel(wf?.data_classification ?? null);
      } catch {
        if (!cancelled) setWorkflowLabel(null);
      }
    })();
    return () => { cancelled = true; };
  }, [wfLabelKey, api.accessToken]);

  // Dashboard Library "Open workflow" deep link — `?workflow=<id>` loads the
  // workflow's latest version straight onto the canvas (no Load dialog).
  useOpenWorkflowFromQuery({ accessToken: api.accessToken, onOpenWorkflow: api.openWorkflowById, setAccessDenied: api.setAccessDenied });

  // generalized-stack-wake D12 — node_kind -> managed-stack binding map for the
  // on-node overlay. Polls (via the shared backoff poller) only while a workflow
  // is open + authed; provided to the canvas nodes below.
  const stackBindings = useNodeStackBindings(!!(api.currentWorkflow?.id && api.accessToken));

  // sz-2.10d — canvas breakpoints (right-click Add/Remove + Inspect frame).
  const breakpoints = useBreakpoints({
    apiUrl: getApiUrl(),
    accessToken: api.accessToken,
    workflowId: api.currentWorkflow?.id,
    versionId: api.currentVersionId,
  });
  // The paused run's frame inspector (opened from the node context menu).
  const [inspectFrame, setInspectFrame] = useState<{ runId: number; nodeId: string } | null>(null);
  useEffect(() => {
    if (!breakpoints.error) return;
    toast({
      title: "Breakpoints",
      description: breakpoints.error,
      variant: "destructive",
    });
  }, [breakpoints.error]);

  // Marketplace is for tenant admins (parent_user_id == null) and platform
  // admins only — mirrors the old Dashboard nav gating. Non-admins never see
  // the functions-menu button. NOTE: must live below `const api` — it reads
  // api.accessToken, so declaring it earlier would touch `api` in its TDZ.
  useEffect(() => {
    if (!api.accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const u = await fetch(`${getApiUrl()}/me`, {
          headers: { Authorization: `Bearer ${api.accessToken}` },
        }).then((r) => r.json());
        const isAdmin = !!(u && (u.is_platform_admin || u.parent_user_id == null));
        if (!cancelled) {
          setCanMarketplace(isAdmin);
          // FX.163 p3b4b — same verdict gates the data-marking banner's
          // "Clear" button (the DELETE route needs manage_settings).
          setCanClearMarking(isAdmin);
        }
      } catch {
        if (!cancelled) setCanMarketplace(false);
      }
    })();
    return () => { cancelled = true; };
  }, [api.accessToken]);

  // Stack Wizard gate — probe the actual STACK_AUTHOR-gated router
  // (`GET /persistent-stacks/templates`) rather than guessing from a role
  // field. This can never drift from the backend rule: platform admins and
  // delegated `stack:author` holders get 2xx (button shown); everyone else
  // gets 403 (button hidden). A 401/network error also hides it — better a
  // missing entry than a dead one.
  useEffect(() => {
    if (!api.accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`${getApiUrl()}/stacks/templates`, {
          headers: { Authorization: `Bearer ${api.accessToken}` },
        });
        if (!cancelled) setCanStackAuthor(resp.ok);
      } catch {
        if (!cancelled) setCanStackAuthor(false);
      }
    })();
    return () => { cancelled = true; };
  }, [api.accessToken]);

  // Function to check Harbor configuration (extracted for reuse)
  const checkHarborStatus = useCallback(async () => {
    if (!api.accessToken) {
      return;
    }

    setHarborStatus(prev => ({ ...prev, loading: true }));

    try {
      const apiUrl = getApiUrl();

      // Check user profile to determine if platform admin.
      // authenticatedFetch handles the silent token-refresh on 401 so a
      // long-running session doesn't 401-storm the API after token expiry.
      const meResponse = await authenticatedFetch(`${apiUrl}/me`, {
        headers: { Accept: "application/json" },
      });

      if (!meResponse.ok) {
        throw new Error("Failed to fetch user profile");
      }

      const userProfile = await meResponse.json();
      const isPlatformAdmin = !!userProfile.is_platform_admin;
      const tenantId = userProfile.tenant_id || 1;

      // Check Harbor status
      const harborResponse = await authenticatedFetch(`${apiUrl}/harbor/status`, {
        headers: { Accept: "application/json" },
      });

      if (!harborResponse.ok) {
        throw new Error("Failed to check registry status");
      }

      const harborData = await harborResponse.json();

      const ent = await loadEntitlements();
      setHarborStatus({
        loading: false,
        configured: harborData.platform_configured,
        isPlatformAdmin,
        tenantId,
        ceEdition: !!ent && ent.edition === "ce" && !ent.error,
      });
    } catch (harborErr) {
      console.error("Error checking registry status:", harborErr);
      // Resolve entitlements independently of the Harbor fetch so that CE
      // never blocks on a missing /harbor/status (CE ships no Harbor router).
      // Paid trees keep HEAD behaviour: a paid tree that also hits this catch
      // path is blocked unless loadEntitlements confirms CE.
      let ceEditionVal = false;
      try {
        const ent = await loadEntitlements();
        ceEditionVal = !!ent && ent.edition === "ce" && !ent.error;
      } catch {
        // loadEntitlements never throws — it resolves null on error.
      }
      setHarborStatus({
        loading: false,
        configured: false,
        isPlatformAdmin: false,
        tenantId: 0,
        ceEdition: ceEditionVal,
        error: harborErr instanceof Error ? harborErr.message : "Unknown error",
      });
    }
  }, [api.accessToken]);

  // Check Harbor configuration on FIRST token available, not on every
  // token change. Silent token refresh (auth/refresh-token) fires every
  // ~5-15 min during a long session; if this effect reruns on each one,
  // `setHarborStatus({loading:true})` takes over the page with a full-
  // screen spinner every refresh — user sees this as "page refreshing
  // every 20-30 seconds". Track the last token value we checked with
  // via a ref and short-circuit re-entry.
  const harborCheckedForTokenRef = useRef<string | null>(null);
  useEffect(() => {
    if (!api.accessToken) return;
    if (harborCheckedForTokenRef.current === "ok") return;  // already OK
    harborCheckedForTokenRef.current = api.accessToken;
    checkHarborStatus().then(() => {
      // Mark "ok" so even if accessToken changes later (refresh), we
      // don't re-run. Harbor config doesn't change mid-session.
      harborCheckedForTokenRef.current = "ok";
    });
  }, [api.accessToken, checkHarborStatus]);

  /**
   * Workers that may run this tenant's workloads (pool + assigned + tenant-owned), same rule as queue routing.
   * Platform admins must use this too — do not use GET /platform/workers here, or admins would "have workers"
   * even when every worker is assigned away from their home tenant.
   */
  const checkTenantWorkerAvailability = useCallback(async (opts: { background?: boolean } = {}) => {
    if (!api.accessToken) {
      return;
    }

    // Only flip the global `loading` flag on the FIRST check. Background
    // re-polls every 30s update the counters in-place; flipping loading
    // true during those takes over the whole viewport with a full-screen
    // "Checking worker availability..." spinner — the user sees that as
    // the entire page flashing every 30s.
    if (!opts.background) {
      setWorkerStatus(prev => ({ ...prev, loading: true }));
    }

    try {
      const apiUrl = getApiUrl();
      const tenantId = harborStatus.tenantId; // Use tenant_id from user profile

      const response = await authenticatedFetch(`${apiUrl}/workers/available-for-tenant/${tenantId}`, {
        headers: { Accept: "application/json" },
      });

      if (!response.ok) {
        if (response.status === 403) {
          let detail = "You are not allowed to view worker assignments for this tenant.";
          try {
            const errBody = await response.json();
            if (errBody?.detail) {
              detail = typeof errBody.detail === "string" ? errBody.detail : JSON.stringify(errBody.detail);
            }
          } catch {
            /* ignore */
          }
          console.warn("Tenant workers endpoint returned 403:", detail);
          setWorkerStatus((prev) => ({
            loading: false,
            checked: true,
            // 403 is a HARD denial — flip immediately, no debounce.
            hasWorkers: false,
            onlineCount: 0,
            licensedCount: 0,
            totalCount: 0,
            offlineCount: 0,
            error: detail,
            consecutiveMisses: prev.consecutiveMisses + 1,
          }));
          return;
        }
        // 502/503/504 (or any 5xx) and 401 are a degraded/warming backend, not
        // a real "no workers" signal — surface a calm retrying message rather
        // than a raw status. The catch keeps hasWorkers optimistic for one more
        // poll so a transient blip doesn't boot the user out of the wizard.
        const degraded = response.status >= 500 || response.status === 401;
        throw new Error(
          degraded
            ? "Backend temporarily unavailable — retrying…"
            : `Failed to fetch tenant worker status: ${response.status}`,
        );
      }

      const workers = await response.json();
      const totalAvailable = workers.total_available || 0;
      const tenantOwned = workers.tenant_owned || [];
      const platformAssigned = workers.platform_assigned || [];
      const platformPool = workers.platform_pool || [];
      const allWorkers = [...tenantOwned, ...platformAssigned, ...platformPool];

      const norm = (s: string | undefined) => (s || "").toLowerCase();
      const licensedActive = allWorkers.filter((w: { status?: string }) => norm(w.status) === "licensed").length;
      const offlineWorkers = allWorkers.filter((w: { status?: string }) => norm(w.status) === "offline").length;

      setWorkerStatus((prev) => {
        const nowHasWorkers = totalAvailable > 0;
        const newMisses = nowHasWorkers ? 0 : prev.consecutiveMisses + 1;
        // Stay optimistic through the first miss — most zero-worker
        // reads right after page load are races with the server's
        // heartbeat-cleanup cycle. Only flip hasWorkers=false on the
        // SECOND consecutive miss.
        const hasWorkersSticky = nowHasWorkers ? true : newMisses < 2 && prev.hasWorkers;
        return {
          loading: false,
          checked: true,
          hasWorkers: hasWorkersSticky,
          onlineCount: licensedActive,
          licensedCount: licensedActive,
          totalCount: totalAvailable,
          offlineCount: offlineWorkers,
          consecutiveMisses: newMisses,
        };
      });
    } catch (error) {
      console.error("Error checking tenant worker availability:", error);
      setWorkerStatus((prev) => ({
        loading: false,
        checked: true,
        // Network error — keep prior hasWorkers true for one more
        // check before flipping the banner. One lost request shouldn't
        // boot the user out of the wizard.
        hasWorkers: prev.consecutiveMisses < 1 && prev.hasWorkers,
        onlineCount: 0,
        licensedCount: 0,
        totalCount: 0,
        offlineCount: 0,
        error: error instanceof Error ? error.message : "Unknown error",
        consecutiveMisses: prev.consecutiveMisses + 1,
      }));
    }
  }, [api.accessToken, harborStatus]);

  // Background re-poll worker status every 30 s. Pass background:true
  // so the poll updates counters in-place without flipping `loading`
  // — otherwise every poll would trigger the full-screen spinner
  // early-return and the entire page would flash every 30s.
  useEffect(() => {
    if (!harborStatus.configured || harborStatus.loading || !api.accessToken) return;
    const id = window.setInterval(() => {
      checkTenantWorkerAvailability({ background: true });
    }, 30_000);
    return () => window.clearInterval(id);
  }, [harborStatus.configured, harborStatus.loading, api.accessToken, checkTenantWorkerAvailability]);

  // Check worker availability after Harbor is confirmed configured
  useEffect(() => {
    if (harborStatus.configured && !harborStatus.loading && api.accessToken && !workerStatus.checked) {
      checkTenantWorkerAvailability();
    }
  }, [harborStatus.configured, harborStatus.loading, api.accessToken, workerStatus.checked, checkTenantWorkerAvailability]);

  // Auto-trigger node refresh on initial load (only once)
  const [hasTriggeredRefresh, setHasTriggeredRefresh] = useState(false);

  // ===== New Node wizard + build queue =====
  //
  // Both live at the stable parent (this component) so they aren't
  // wiped whenever NodeLibrary's key bumps (node refresh, delete,
  // reload). Build queue replaces the old single-slot floating
  // progress card: any number of builds can be in flight; the
  // sidebar's Wizard Queue button opens a paginated dialog showing
  // one memoized row per build, and each row polls independently
  // so a status change in one doesn't re-render the rest.
  const [wizardOpen, setWizardOpen] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  // Bell deep-link `#wizard-queue/<id>`: the run the Wizard Queue panel
  // should scroll to + flash once open. Set on load and on hashchange.
  const [wizardQueueFocusId, setWizardQueueFocusId] = useState<string | null>(null);
  // CE: no Project Wizard session state ()
  // node-wizard→workflow handoff: the description the Node Wizard hands off
  // when it decides a request is really a multi-step pipeline. Seeds the
  // Workflow Wizard, which auto-composes it. Cleared when that panel closes.
  const [workflowSeedPrompt, setWorkflowSeedPrompt] = useState<string | undefined>(undefined);
  // When a pending generation finishes and the user clicks Resume,
  // stash the taskId here so the wizard opens straight to Preview
  // and re-polls the already-done task. Cleared as soon as the wizard
  // sees it (the wizard's resume-effect consumes the prop once).
  const [resumeTaskId, setResumeTaskId] = useState<number | null>(null);
  // Carries the queued nodeName forward into the wizard on resume.
  // The defer flow wipes localStorage when the user picks
  // "Close & notify me", so without this the wizard remounts on
  // resume with no nodeName and Phase 2's queue row falls back to
  // the "Task #<id>" placeholder.
  const [resumeNodeName, setResumeNodeName] = useState<string | undefined>(undefined);
  // workflow-wizard-missing-node-handoff — seed payload from the
  // Workflow Wizard's structured hint (source type + source + purpose).
  // NewNodeWizard hydrates these onto its form on mount.
  const [resumeWizardSeed, setResumeWizardSeed] = useState<{
    sourceType?: "git" | "image" | "dockerfile" | "script" | "describe";
    source?: string;
    purpose?: string;
  } | null>(null);
  // Bumped on every wizard open. Used as the wizard's React `key` so
  // that each open forces a fresh mount — without this the wizard
  // stays mounted across opens and its in-memory useState values
  // survive, so a partially-filled wizard from a prior session would
  // reappear when the user clicked + New Node. Combined with
  // clearPersistedWizardState below, a key bump means the wizard
  // mounts with empty localStorage AND empty in-memory state.
  const [wizardOpenCount, setWizardOpenCount] = useState(0);

  // node-code-edit-rebuild — set when NodeLibrary's "Edit code" action
  // opens the wizard in "edit existing node code" mode instead of the
  // normal create-a-node flow. Cleared alongside the resume props on
  // wizard close.
  const [editNodeId, setEditNodeId] = useState<number | null>(null);
  const [editNodeName, setEditNodeName] = useState<string | undefined>(undefined);

  // WSIP 4.5 — the run the Workflow Wizard opens to debug (conversational
  // "explain this run"). Non-null = the wizard mounts in debug mode bound to
  // that run; every OTHER wizard open clears it first, so a normal open is
  // never stuck in debug mode.
  const [debugRunId, setDebugRunId] = useState<number | null>(null);
  // RunRail's "Explain this run" button hands the run id here: stash it and
  // open the wizard with the usual pattern (the count bump force-remounts
  // the panel so it mounts fresh with the debug binding applied).
  const handleExplainRun = (runId: number) => {
    setDebugRunId(runId);
    setWizardOpenCount((c) => c + 1);
    setWizardOpen(true);
  };

  // Cross-app navigation: when the Dashboard's notifications panel
  // routes a "Node artifacts ready" / "Node generation failed" click
  // here, the URL carries either:
  //   ?openQueue=1&taskId=<id>  — open the build-queue panel +
  //                               expand the row for that task.
  //                               Used by node_generation_*
  //                               notifications today.
  //   ?resume=<task_id>         — legacy path that reopens the
  //                               wizard at Preview. Still honoured
  //                               for backwards compat if some other
  //                               surface emits it; new notifications
  //                               use openQueue.
  // Consume on first mount, strip the query so a refresh doesn't
  // re-trigger.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const params = new URLSearchParams(window.location.search);
      const openQueue = params.get("openQueue");
      const queueTaskRaw = params.get("taskId");
      const resumeRaw = params.get("resume");

      let consumed = false;

      if (openQueue === "1") {
        setQueueOpen(true);
        const queueTaskId = parseInt(queueTaskRaw || "", 10);
        if (Number.isFinite(queueTaskId) && queueTaskId > 0) {
          // Let the build-queue row pick this up; expanding a row
          // before it has rendered is a no-op, so fire on the next
          // frame to give React time to mount the panel + rows.
          window.requestAnimationFrame(() => {
            window.dispatchEvent(
              new CustomEvent("mod:queue:expand-row", { detail: { taskId: queueTaskId } }),
            );
          });
        }
        consumed = true;
      } else if (resumeRaw) {
        const taskId = parseInt(resumeRaw, 10);
        if (Number.isFinite(taskId) && taskId > 0) {
          // Legacy resume path — open the wizard at Preview. New
          // notifications use openQueue= instead.
          clearPersistedWizardState();
          setResumeTaskId(taskId);
          setDebugRunId(null);
          setWizardOpenCount((c) => c + 1);
          setWizardOpen(true);
          consumed = true;
        }
      } else if (params.get("new") === "node" || params.get("newNode") === "1") {
        // Deep link to the New Node Wizard — used by the in-product assistant's
        // "open new node wizard" action (replaces the dead /builder/new route).
        // Open a fresh wizard (no resume task).
        setDebugRunId(null);
        setWizardOpenCount((c) => c + 1);
        setWizardOpen(true);
        consumed = true;
      }

      if (consumed) {
        const url = new URL(window.location.href);
        url.searchParams.delete("openQueue");
        url.searchParams.delete("taskId");
        url.searchParams.delete("resume");
        url.searchParams.delete("new");
        url.searchParams.delete("newNode");
        window.history.replaceState({}, "", url.toString());
      }
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // NOTE: we intentionally do NOT clear the persisted wizard draft on mount
  // or unmount. Doing so wiped a user's in-progress new-node draft whenever
  // they navigated away from the Workflow Builder and came back (the page-level
  // unmount/remount fired the purge before the wizard could rehydrate). The
  // draft now survives navigation; it is still cleared on a successful submit
  // (resetForm / handleCloseAndReset / handleCloseAndNotify) and auto-expires
  // after WIZARD_DRAFT_TTL_MS (loadPersisted), so a stale prior-session draft
  // doesn't resurface indefinitely on a much-later "+ New Node".
  // LDF-3 §5 — Build Queue is localStorage-backed; the original key was
  // global (`mod.buildQueue.v1`), which leaked across users on the same
  // browser when User A logged out and User B logged in. Server-side
  // queue endpoints (/platform/queue, /platform/workers/.../queue) are
  // already platform-admin-gated, so the only cross-user surface was
  // here. The helper derives a per-user suffix from the JWT `sub` so
  // each authenticated user gets their own queue.
  const buildsStorageUserId = (() => {
    if (typeof window === "undefined") return "anon";
    const tok = window.sessionStorage.getItem("workflow_access_token") ||
                window.sessionStorage.getItem("accessToken") || "";
    return deriveBuildQueueUserId(tok);
  })();
  const BUILDS_STORAGE_KEY = buildQueueStorageKey(buildsStorageUserId);

  const [builds, setBuildsRaw] = useState<BuildQueueEntry[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.localStorage.getItem(BUILDS_STORAGE_KEY);
      if (raw) return JSON.parse(raw) as BuildQueueEntry[];
      // LDF-3 §5 migration: if the legacy GLOBAL key still has a
      // payload AND we're a real authenticated user (not "anon"),
      // adopt it as ours and remove the global key so it can never
      // leak to a future user. anon stays on the global key (read-
      // only) until login.
      const legacyGlobal = window.localStorage.getItem(LEGACY_GLOBAL_BUILDS_KEY);
      if (legacyGlobal && buildsStorageUserId !== "anon") {
        try {
          const adopted = JSON.parse(legacyGlobal) as BuildQueueEntry[];
          window.localStorage.setItem(BUILDS_STORAGE_KEY, legacyGlobal);
          window.localStorage.removeItem(LEGACY_GLOBAL_BUILDS_KEY);
          return adopted;
        } catch { /* fall through */ }
      }
      // Older single-slot migration (pre-LDF-3) — unchanged.
      const legacy = window.localStorage.getItem(OLD_SINGLE_BUILD_KEY);
      if (legacy) {
        const one = JSON.parse(legacy) as { taskId: number; nodeName: string };
        window.localStorage.removeItem(OLD_SINGLE_BUILD_KEY);
        return [{ ...one, startedAt: Date.now() }];
      }
      return [];
    } catch {
      return [];
    }
  });

  const setBuilds = useCallback(
    (updater: BuildQueueEntry[] | ((prev: BuildQueueEntry[]) => BuildQueueEntry[])) => {
      setBuildsRaw((prev) => {
        const next = typeof updater === "function" ? updater(prev) : updater;
        if (typeof window !== "undefined") {
          try {
            if (next.length > 0)
              window.localStorage.setItem(BUILDS_STORAGE_KEY, JSON.stringify(next));
            else window.localStorage.removeItem(BUILDS_STORAGE_KEY);
          } catch { /* quota / security — tolerate */ }
        }
        return next;
      });
    },
    [],
  );

  // Cross-tab sync: when another tab adds/dismisses a build, reflect
  // it here so the count badge + dialog stay consistent.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (e: StorageEvent) => {
      if (e.key !== BUILDS_STORAGE_KEY) return;
      if (!e.newValue) setBuildsRaw([]);
      else {
        try { setBuildsRaw(JSON.parse(e.newValue) as BuildQueueEntry[]); }
        catch { /* ignore parse error */ }
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const addBuild = useCallback((entry: Omit<BuildQueueEntry, "startedAt"> & { startedAt?: number }) => {
    setBuilds((prev) => {
      // If a row for the same taskId already exists, don't duplicate.
      if (prev.some((b) => b.taskId === entry.taskId)) return prev;
      return [{ ...entry, startedAt: entry.startedAt ?? Date.now() }, ...prev];
    });
  }, [setBuilds]);

  // Phase 1 → Phase 2 transition: morph the existing generation-phase
  // row into the build-phase row in place rather than spawning a second
  // queue entry. See applyTransitionToBuild() for the why.
  const transitionToBuild = useCallback(
    (generationTaskId: number, newBuildTaskId: number, nodeName: string) => {
      setBuilds((prev) =>
        applyTransitionToBuild(prev, generationTaskId, newBuildTaskId, nodeName),
      );
    },
    [setBuilds],
  );

  const dismissBuild = useCallback((taskId: number) => {
    setBuilds((prev) => prev.filter((b) => b.taskId !== taskId));
  }, [setBuilds]);

  const markBuildFinished = useCallback(
    (taskId: number, outcome: "success" | "failed") => {
      setBuilds((prev) =>
        prev.map((b) => (b.taskId === taskId ? { ...b, outcome } : b)),
      );
      // On success, refresh the library so the new node appears.
      if (outcome === "success") {
        window.dispatchEvent(new Event("mod:nodes:changed"));
      }
    },
    [setBuilds],
  );

  const clearCompleted = useCallback(() => {
    setBuilds((prev) => prev.filter((b) => !b.outcome));
  }, [setBuilds]);

  const inProgressCount = builds.filter((b) => !b.outcome).length;

  useEffect(() => {
    // Only trigger refresh once when Harbor is configured and we have an access token
    if (harborStatus.configured && !harborStatus.loading && api.accessToken && !hasTriggeredRefresh) {
      console.log('🔄 Workflow Builder loaded with registry configured - triggering node refresh...');
      console.log('Access token available:', !!api.accessToken);
      console.log('API URL:', apiUrl());

      setHasTriggeredRefresh(true); // Mark as triggered
      setIsLoadingNodes(true); // Show loading indicator

      // Trigger node refresh to ensure latest nodes are available
      const refreshNodes = async () => {
        try {
          const apiUrl = getApiUrl();
          const url = `${apiUrl}/nodes/refresh`;
          console.log('📡 Calling:', url);

          const response = await fetch(url, {
            method: 'POST',
            headers: {
              'Authorization': `Bearer ${api.accessToken}`,
              'Accept': 'application/json',
              'Content-Type': 'application/json',
            },
          });

          console.log('📊 Response status:', response.status);
          const responseData = await response.json().catch(() => null);
          console.log('📋 Response data:', responseData);

          if (response.ok) {
            console.log('✅ Node refresh triggered — re-fetching library in-place');
            // Dispatch the event NodeLibrary listens for. NO key bump —
            // key bumps unmount the whole sidebar subtree and everything
            // rendered in it, which is what kept killing the wizard.
            window.dispatchEvent(new Event('mod:nodes:changed'));
          } else {
            console.warn('❌ Node refresh failed:', response.status, response.statusText, responseData);
            setIsLoadingNodes(false);
          }
        } catch (error) {
          console.error('💥 Error triggering node refresh:', error);
          setIsLoadingNodes(false);
        }
      };

      refreshNodes();
    }
  }, [harborStatus.configured, harborStatus.loading, api.accessToken, hasTriggeredRefresh, api]);

  // Callback from NodeLibrary when loading state changes
  const handleNodeLibraryLoadingChange = useCallback((loading: boolean) => {
    setIsLoadingNodes(loading);

    // When loading finishes, set cookie and refetch allNodeDefs for hinting menu
    if (!loading && api.accessToken) {
      const hadLoadedBefore = hasLoadedNodesBefore();

      const fetchAllNodeDefs = async () => {
        try {
          const apiUrl = getApiUrl();
          const response = await authenticatedFetch(`${apiUrl}/nodes/?limit=1000&offset=0`, {
            headers: { Accept: "application/json" },
          });
          if (!response.ok) throw new Error("Failed to fetch node definitions");
          const apiNodes: NodeDefinition[] = await response.json();
          const processedApiNodes = apiNodes.map(n => ({...n, nodeType: n.input_schema?.input_type === 'NONE' ? 'input' : (n.output_schema?.output_type === 'NONE' ? 'output' : (n.process_type === 'INPUT' ? 'input' : 'process')) }));
          setAllNodeDefs([...localNodes, ...processedApiNodes]);

          // Set cookie so future loads skip the stability check
          if (!hadLoadedBefore) {
            setNodesLoadedCookie();
            console.log("First load complete — hinting system initialized.");
          }
        } catch (error) {
          console.error("Error fetching all node definitions for hinting:", error);
          setAllNodeDefs(localNodes);
          // Even on error, set cookie if it was first load to avoid infinite refresh loops
          if (!hadLoadedBefore) {
            setNodesLoadedCookie();
          }
        }
      };
      fetchAllNodeDefs();
    }
  }, [api.accessToken]);

  // Reset unsaved changes flag when workflow is loaded
  useEffect(() => {
    if (api.currentWorkflow && api.currentVersionNumber) {
      setHasUnsavedChanges(false);
    }
  }, [api.currentWorkflow, api.currentVersionNumber]);

  // mod-assistant-agent §5/§6 — handle open-dialog actions emitted
  // by the assistant. The action runtime in assistant-panel.tsx
  // dispatches `mod:assistant-open-dialog` with the dialog id; we
  // route to the matching state setter here so the chat panel doesn't
  // have to know about every dialog's wiring. Ids must match the
  // entries under `dialogs:` in CORE/API/data/assistant_site_map.yaml.
  useEffect(() => {
    const handler = (e: Event) => {
      const id = (e as CustomEvent).detail?.id as string | undefined;
      if (!id) return;
      switch (id) {
        case "new-node-wizard":
          // Mirror the onOpenWizard prop NodeLibrary uses (line 1121
          // below) — clear stale resume state, wipe persisted wizard
          // state, bump the open counter so the dialog force-remounts
          // with empty in-memory useState.
          setResumeTaskId(null);
          setResumeNodeName(undefined);
          setEditNodeId(null);
          setEditNodeName(undefined);
          clearPersistedWizardState();
          setDebugRunId(null);
          setWizardOpenCount((c) => c + 1);
          setWizardOpen(true);
          break;
        case "workflow-wizard":
          setIsWizardPanelOpen(true);
          break;
        case "schedule-dialog":
          window.dispatchEvent(new CustomEvent("mod:open-schedule-dialog"));
          break;
        case "process-queue":
          setIsQueueDialogOpen(true);
          break;
        case "node-queue":
          setQueueOpen(true);
          break;
      }
    };
    window.addEventListener("mod:assistant-open-dialog", handler);
    return () => window.removeEventListener("mod:assistant-open-dialog", handler);
  }, []);

  // Bell deep-link `#wizard-queue/<run id>`: on load and on hashchange,
  // open the Wizard Queue panel and focus the named run. The focus id is
  // kept (not cleared) so the panel can reveal the row even after a close
  // → reopen, and a matching hash is a no-op on re-fire.
  useEffect(() => {
    const handle = () => {
      const id = parseWizardQueueHash(window.location.hash);
      if (!id) return;
      setWizardQueueFocusId(id);
      setQueueOpen(true);
    };
    handle();
    window.addEventListener("hashchange", handle);
    return () => window.removeEventListener("hashchange", handle);
  }, []);

  // Event Handling Hook
  const events = useWorkflowEvents(nodes, edges, setNodes, setEdges, allNodeDefs, commit);

  // Auto-focus context menu search input when menu appears
  useEffect(() => {
    if (events.menu) {
      // Use setTimeout to ensure the input is rendered before focusing
      setTimeout(() => {
        const input = document.querySelector('[cmdk-input]') as HTMLInputElement;
        if (input) {
          input.focus();
        }
      }, 0);
    }
  }, [events.menu]);

  // Listen for file browser open event
  useEffect(() => {
    const handleOpenFileBrowser = (e: Event) => {
      const customEvent = e as CustomEvent;
      const { nodeId, inputType, allowMultiple, isMetadata } = customEvent.detail;
      setFileBrowserContext({ nodeId, inputType, allowMultiple, isMetadata });
      setIsFileBrowserOpen(true);
    };

    window.addEventListener('openFileBrowser', handleOpenFileBrowser);
    return () => {
      window.removeEventListener('openFileBrowser', handleOpenFileBrowser);
    };
  }, []);

  // Copy / paste / duplicate of canvas nodes. The clipboard is an in-app ref
  // (not the system clipboard) so a paste keeps the FULL node data — config,
  // schema, selected files — which a text serialization would lose. Pure graph
  // logic (id remap, internal-edge rewiring) lives in lib/workflow-clipboard.
  const clipboardRef = useRef<ClipboardSnapshot | null>(null);

  const pasteSnapshot = useCallback((snapshot: ClipboardSnapshot | null) => {
    const existingIds = nodes.map((n) => n.id);
    const result = buildPaste(snapshot, existingIds);
    if (!result) return;
    // One undo entry covers the whole paste (new nodes + their edges).
    commit();
    // Carry any directly-uploaded File blobs (kept in fileStore, not node.data)
    // onto the pasted node ids so an uploaded input isn't silently lost.
    setFileStore((prev) => {
      const next = { ...prev };
      for (const [oldId, newId] of Object.entries(result.idMap)) {
        if (prev[oldId]) next[newId] = prev[oldId];
      }
      return next;
    });
    // Clear the previous selection so only the fresh paste is selected.
    setNodes((nds) => nds.map((n) => (n.selected ? { ...n, selected: false } : n)).concat(result.nodes));
    setEdges((eds) => eds.concat(result.edges));
    api.markDirty();
  }, [nodes, commit, setNodes, setEdges, setFileStore, api.markDirty]);

  const handleCopy = useCallback(() => {
    const snapshot = copySelection(nodes, edges);
    if (!snapshot) return; // nothing selected — leave native copy alone
    clipboardRef.current = snapshot;
  }, [nodes, edges]);

  const handlePaste = useCallback(() => {
    pasteSnapshot(clipboardRef.current);
  }, [pasteSnapshot]);

  // Duplicate = copy the current selection straight into a paste, without
  // touching the clipboard (so a prior copy survives). Used by Ctrl/Cmd+D and
  // the right-click menu.
  const handleDuplicate = useCallback((only?: Node) => {
    const snapshot = only
      ? copySelection([{ ...only, selected: true }], edges)
      : copySelection(nodes, edges);
    pasteSnapshot(snapshot);
  }, [nodes, edges, pasteSnapshot]);

  // Right-click "Duplicate" (bonus). A minimal node context menu positioned at
  // the cursor; duplicates the whole selection when the right-clicked node is
  // part of it, otherwise just that node.
  const [nodeCtxMenu, setNodeCtxMenu] = useState<{ x: number; y: number; node: Node } | null>(null);
  const onNodeContextMenu = useCallback((e: React.MouseEvent, node: Node) => {
    e.preventDefault();
    setNodeCtxMenu({ x: e.clientX, y: e.clientY, node });
  }, []);
  // R-038 follow-up — focus lands on the first menu item ("Duplicate") when
  // the menu opens (mouse or keyboard), so Tab walks the items in order.
  const nodeCtxMenuFirstItemRef = useRef<HTMLButtonElement | null>(null);
  // FX.68.p3 [U] — every menu item, so arrow keys can walk them, plus the
  // element focus came from, so Escape can put it back (WCAG 2.1.1: closing a
  // menu must not leave the keyboard lost on a detached item).
  const nodeCtxMenuItemsRef = useRef<HTMLButtonElement[]>([]);
  const nodeCtxMenuFocusFromRef = useRef<HTMLElement | null>(null);
  const nodeCtxMenuContainerRef = useRef<HTMLDivElement | null>(null);
  // rc20-golden — the menu is `fixed` at the raw right-click point, so a node
  // near the canvas edge pushes the deep run-control items (the block ends with
  // "Output versions…") past the bottom/right edge: the button renders, but
  // Playwright reports it "outside of the viewport" forever and the click never
  // lands. Measure the rendered menu and shift it back inside the viewport.
  // The shift is applied as a transform (not top/left) and is recomputed
  // against the UNSHIFTED box, so the second pass converges instead of
  // ping-ponging.
  const [nodeCtxMenuShift, setNodeCtxMenuShift] = useState({ dx: 0, dy: 0 });
  useEffect(() => {
    if (!nodeCtxMenu) {
      if (nodeCtxMenuShift.dx !== 0 || nodeCtxMenuShift.dy !== 0) {
        setNodeCtxMenuShift({ dx: 0, dy: 0 });
      }
      return;
    }
    // FX.68.p3 [U] — collect the menu's buttons so the canvas keydown handler
    // can walk them with arrow keys (queried from the live DOM so a re-render
    // with a different run/selection can never hand back stale buttons).
    const container = nodeCtxMenuContainerRef.current;
    nodeCtxMenuItemsRef.current = container
      ? Array.from(container.querySelectorAll("button")) as HTMLButtonElement[]
      : [];
    (nodeCtxMenuItemsRef.current[0] ?? nodeCtxMenuFirstItemRef.current)?.focus();
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const EDGE = 8;
    // Undo the shift already applied so the target is relative to the raw
    // anchor box (converges in one extra render).
    const bx = rect.left - nodeCtxMenuShift.dx;
    const by = rect.top - nodeCtxMenuShift.dy;
    const bw = rect.right - rect.left;
    const bh = rect.bottom - rect.top;
    let dx = 0;
    let dy = 0;
    if (bx + bw > vw - EDGE) dx = vw - EDGE - (bx + bw);
    if (bx < EDGE) dx = Math.max(dx, EDGE - bx);
    if (by + bh > vh - EDGE) dy = vh - EDGE - (by + bh);
    if (by < EDGE) dy = Math.max(dy, EDGE - by);
    if (dx !== nodeCtxMenuShift.dx || dy !== nodeCtxMenuShift.dy) {
      setNodeCtxMenuShift({ dx, dy });
    }
  }, [nodeCtxMenu, nodeCtxMenuShift]);

  // Canvas keyboard shortcuts. Node delete is bound natively by ReactFlow's
  // `deleteKeyCode` prop; Save/Undo/Redo/Copy/Paste/Duplicate have no native
  // binding so they are dispatched here. Undo/Redo restore snapshots from the
  // history stack owned by useWorkflowState (Ctrl/Cmd+Z, Ctrl/Cmd+Y,
  // Ctrl/Cmd+Shift+Z); Copy/Paste/Duplicate are Ctrl/Cmd+C / V / D.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      dispatchWorkflowHotkey(e, {
        onSave: () => { void api.handleSaveVersion(); },
        onUndo: () => { undo(); },
        onRedo: () => { redo(); },
        onCopy: handleCopy,
        onPaste: handlePaste,
        onDuplicate: () => handleDuplicate(),
      });
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [api.handleSaveVersion, undo, redo, handleCopy, handlePaste, handleDuplicate]);

  // Track unsaved changes when nodes or edges change
  useEffect(() => {
    if (nodes.length > 0 || edges.length > 0) {
      setHasUnsavedChanges(true);
    }
  }, [nodes, edges]);

  // R-038 — announce run lifecycle + per-node status changes into the canvas
  // live region. Both trackers reset when the watched run (or workflow)
  // changes so switching workflows never replays stale announcements.
  const lastRunStatusKey = useRef<string | null>(null);
  useEffect(() => {
    const runId = api.pollingRunId;
    if (runId == null) {
      lastRunStatusKey.current = null;
      return;
    }
    const status = api.activeRunInfo?.status ?? null;
    const key = `${api.currentWorkflow?.id ?? ""}:${runId}:${status}`;
    if (lastRunStatusKey.current === key) return;
    lastRunStatusKey.current = key;
    if (status) announceCanvas(describeRunStatusChange(status));
  }, [api.pollingRunId, api.activeRunInfo?.status, api.currentWorkflow?.id, announceCanvas]);

  const lastNodeStatuses = useRef<Map<string, string> | null>(null);
  const runAnnouncedForRef = useRef<string | null>(null);
  useEffect(() => {
    // Keyed on the run the CANVAS is bound to, not the live-poll alias: the
    // poller writes the terminal node statuses and clears `pollingRunId` in the
    // SAME tick (hooks/useWorkflowApi.ts:795 `setPollingRunId(null)` once every
    // NodeRun is terminal), so keying on the alias skipped the final transition
    // — a node reaching failure/cancelled announced nothing and the all-terminal
    // sentence never fired. `selectedRunId` stays bound while the settled run
    // stays on the canvas (hooks/useWorkflowApi.ts:152 null only deactivates),
    // and resetRunState (:826) clears it, so switching workflows still resets.
    const runId = api.selectedRunId;
    if (runId == null) {
      lastNodeStatuses.current = null;
      runAnnouncedForRef.current = null;
      return;
    }
    const snaps = nodes
      .filter((n) =>
        ["queued", "running", "success", "failure", "blocked", "cancelled", "canceled"].includes((n.data as any)?.status ?? "")
      )
      .map((n) => ({
        id: n.id,
        label: ((n.data as any)?.label as string) || n.id,
        status: (n.data as any).status as string,
      }));
    const changes = diffStatuses(lastNodeStatuses.current ?? undefined, snaps);
    lastNodeStatuses.current = new Map(snaps.map((s) => [s.id, s.status]));
    // R-038 — ONE announceCanvas per effect pass. Two back-to-back setCanvasLiveMessage
    // calls in the SAME tick batch into a single DOM mutation, so the live region (and
    // any screen reader / MutationObserver) only ever holds the LAST sentence — the node
    // transition sentence ("Return Assets Export: failure") was swallowed by the all-terminal
    // sentence fired right after it (rc20e e2e red: wb-node-catalogue-audit.spec.ts:373).
    // Join every sentence this pass produces into one region update.
    const terminalSentence = describeTerminalRunStatus(
      snaps.map((s) => s.status),
      api.activeRunInfo?.status,
    );
    const parts: string[] = [];
    if (changes.length) parts.push(changes.join(". "));
    if (terminalSentence) {
      if (runAnnouncedForRef.current !== String(runId)) {
        runAnnouncedForRef.current = String(runId);
        parts.push(terminalSentence);
      }
    } else {
      runAnnouncedForRef.current = null;
    }
    if (parts.length) announceCanvas(parts.join(". "));
  }, [nodes, api.selectedRunId, announceCanvas]);

  // R-038 — announce node add/delete (covers library drag-drop, wizard-apply
  // and paste alike — we diff the node id set, not the input path).
  const lastNodeIds = useRef<Set<string> | null>(null);
  useEffect(() => {
    const ids = new Set(nodes.map((n) => n.id));
    if (lastNodeIds.current) {
      const prev = lastNodeIds.current;
      const added = nodes.filter((n) => !prev.has(n.id));
      const removedCount = [...prev].filter((id) => !ids.has(id)).length;
      if (added.length) {
        announceCanvas(describeNodeAdded((added[0].data as any)?.label as string | undefined));
      }
      if (removedCount > 0) announceCanvas(describeNodesDeleted(removedCount));
    }
    lastNodeIds.current = ids;
  }, [nodes, announceCanvas]);

  // R-038 — announce connections appearing/disappearing (covers mouse drag,
  // keyboard connect and wizard-apply equally — we watch the edge set, not
  // the input path).
  const edgeKey = (e: { source: string; sourceHandle?: string | null; target: string; targetHandle?: string | null }) =>
    `${e.source}|${e.sourceHandle ?? ""}|${e.target}|${e.targetHandle ?? ""}`;
  const lastEdgeKeys = useRef<Set<string> | null>(null);
  useEffect(() => {
    const keys = new Set(edges.map(edgeKey));
    if (lastEdgeKeys.current) {
      const prev = lastEdgeKeys.current;
      const added = edges.filter((e) => !prev.has(edgeKey(e)));
      if (added.length) {
        const first = added[0];
        const src = nodes.find((n) => n.id === first.source);
        const tgt = nodes.find((n) => n.id === first.target);
        announceCanvas(describeEdgeConnected(
          (src?.data as any)?.label as string | undefined,
          (tgt?.data as any)?.label as string | undefined,
        ));
      }
    }
    lastEdgeKeys.current = keys;
  }, [edges, nodes, announceCanvas]);

  // R-038 — canvas keyboard path. React Flow's node wrapper (nodesFocusable)
  // is the focusable element: Tab reaches nodes, Enter/Space select (native),
  // Delete removes (native deleteKeyCode), arrows move a selected node
  // natively while a node is focused. This handler covers the gaps:
  //   * Enter on a focused node also opens its config panel (reuses the same
  //     setSelectedNode path as the existing click handler);
  //   * arrows move the selected node even when focus is on the pane.
  // Keys typed inside form controls or buttons are left alone.
  const handleCanvasKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement | null;
    if (!t) return;
    let nodeEl = t.tagName === "DIV" ? (t.closest("[data-id]") as HTMLElement | null) : null;
    // FX.68.p3 [U] — remember the element focus came from while a menu item is
    // focused, so Escape can hand it back.
    const menuContainer = nodeCtxMenuContainerRef.current;
    if (menuContainer && t.tagName === "BUTTON" && t.closest("button") === t
        && menuContainer.contains(t)) {
      nodeCtxMenuFocusFromRef.current = t;
    }
    // R-038 follow-up — Escape closes the node context menu. Handled before
    // the form-control guard below so it works while a menu item is focused.
    if (e.key === "Escape" && nodeCtxMenu) {
      e.preventDefault();
      setNodeCtxMenu(null);
      // FX.68.p3 [U] — and focus returns to where it was (the node wrapper, or
      // the menu item the user was on), never lost on a removed button.
      const back = nodeCtxMenuFocusFromRef.current;
      nodeCtxMenuFocusFromRef.current = null;
      if (back) back.focus();
      return;
    }
    // R-038 follow-up — Shift+F10 / ContextMenu key on a focused (or
    // selected) node opens the same context menu the right-click opens,
    // positioned at the node's on-screen rect.
    if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      let node: Node | undefined;
      const focusedNodeEl = nodeEl;
      if (focusedNodeEl) node = nodes.find((n) => n.id === focusedNodeEl.getAttribute("data-id"));
      if (!node) {
        const sel = nodes.find((n) => n.selected);
        if (sel) {
          node = sel;
          nodeEl = (document.querySelector(`[data-id="${CSS.escape(sel.id)}"]`) as HTMLElement | null) ?? null;
        }
      }
      if (node && nodeEl) {
        e.preventDefault();
        const rect = nodeEl.getBoundingClientRect();
        setNodeCtxMenu({ x: rect.right, y: rect.bottom, node });
      }
      return;
    }
    // FX.68.p3 [U] — arrow-key navigation INSIDE the node context menu: the
    // items are native buttons (Tab walks them already), so arrows walk them
    // too, with wrap-around. Handled before the form-control guard below.
    if (nodeCtxMenu && /^Arrow(Down|Up)$/.test(e.key)
      && menuContainer && menuContainer.contains(t) && t.tagName === "BUTTON") {
      const items = nodeCtxMenuItemsRef.current.filter((b) => menuContainer.contains(b));
      const at = items.indexOf(t as HTMLButtonElement);
      if (at !== -1) {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        const next = items[(at + step + items.length) % items.length];
        if (next) next.focus();
        return;
      }
    }
    if (/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName) || t.isContentEditable) return;
    if (e.key === "Enter") {
      if (nodeEl) {
        const node = nodes.find((n) => n.id === nodeEl.getAttribute("data-id"));
        if (node) {
          e.preventDefault();
          setSelectedNode(node);
        }
      }
      return;
    }
    if (/^Arrow(Up|Down|Left|Right)$/.test(e.key)) {
      // A focused node/edge wrapper owns arrow keys (React Flow's native
      // keyboard movement) — moving here too would double the step.
      if (nodeEl) return;
      const sel = nodes.find((n) => n.selected);
      if (!sel) return;
      e.preventDefault();
      const step = e.shiftKey ? 50 : 10;
      const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
      const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
      setNodes((nds) => nds.map((n) => (n.id === sel.id ? { ...n, position: { x: n.position.x + dx, y: n.position.y + dy } } : n)));
    }
  }, [nodes, setSelectedNode, setNodes, nodeCtxMenu]);

  // R-038 — keyboard connect. For a node, list the other canvas nodes it can
  // be wired to, reusing the SAME validity gate the drag path uses
  // (events.isValidConnection → normalizeConnection + type-compatibility).
  // Returns targets where `fromSource` says whether `nodeId` is the output
  // (feed) side or the input side of the new wire.
  const computeConnectTargets = useCallback((nodeId: string) => {
    const out: { id: string; label: string; fromSource: boolean }[] = [];
    for (const other of nodes) {
      if (other.id === nodeId) continue;
      const asSource = events.isValidConnection({
        source: nodeId, sourceHandle: null, target: other.id, targetHandle: null,
      } as any);
      if (asSource) {
        out.push({ id: other.id, label: ((other.data as any)?.label as string) || other.id, fromSource: true });
        continue;
      }
      const asTarget = events.isValidConnection({
        source: other.id, sourceHandle: null, target: nodeId, targetHandle: null,
      } as any);
      if (asTarget) {
        out.push({ id: other.id, label: ((other.data as any)?.label as string) || other.id, fromSource: false });
      }
    }
    return out;
  }, [nodes, events.isValidConnection]);

  // Fire a keyboard connection. Runs through events.onConnect so the real
  // normalize/type-check/addEdge path (and its error toasts) is reused.
  const connectKeyboard = useCallback((nodeId: string, targetId: string, fromSource: boolean) => {
    events.onConnect({
      source: fromSource ? nodeId : targetId, sourceHandle: null,
      target: fromSource ? targetId : nodeId, targetHandle: null,
    } as any);
  }, [events.onConnect]);

  // Update recommended nodes when menu opens or allNodeDefs changes
  useEffect(() => {
    if (events.menu) {
      const recommended = events.getRecommendedNodes();
      setRecommendedNodes(recommended);
    }
  }, [events.menu, allNodeDefs]);


  const onNodeClick: NodeMouseHandler = useCallback((_, node) => {
    setSelectedNode(node);
  }, [setSelectedNode]);

  // The critical update function
  // `opts.commit === false` skips the undo/redo history entry — used for
  // programmatic, non-user updates (e.g. the config panel syncing a node's
  // schema/version from the live catalog on open) that shouldn't be undoable.
  const updateNodeData = useCallback((nodeId: string, data: any, opts?: { commit?: boolean }) => {
      if (data.file) {
          setFileStore((prev) => ({ ...prev, [nodeId]: data.file }));
          delete data.file;
      }
      api.markDirty();
      if (opts?.commit !== false) commitConfig(nodeId);
      setNodes((nds) =>
          nds.map((node) => {
              if (node.id === nodeId) {
                  const updatedNode = { ...node, data: { ...node.data, ...data } };
                  if (selectedNode && selectedNode.id === nodeId) {
                      setSelectedNode(updatedNode);
                  }
                  return updatedNode;
              }
              return node;
          })
      );
  }, [setNodes, selectedNode, setSelectedNode, api.markDirty, commitConfig]);

  // Handle file selection from file browser
  const handleFileSelect = useCallback((files: any[]) => {
    if (fileBrowserContext.nodeId) {
      const dataKey = fileBrowserContext.isMetadata ? 'selectedMetadata' : 'selectedFiles';
      updateNodeData(fileBrowserContext.nodeId, { [dataKey]: files });
      // GY.C7 — remember the data-classification labels of the assets
      // selected into this node (drives the top controlled banner when
      // any selected asset is CUI/ITAR/EAR/etc.).
      setSelectedAssetLabels(
        (files || [])
          .map((f: any) => f?.data_classification)
          .filter((l: any): l is string => typeof l === "string" && l.length > 0),
      );
      setFileBrowserContext({ nodeId: null });
    }
  }, [fileBrowserContext.nodeId, fileBrowserContext.isMetadata, updateNodeData]);

  // Handle back button click
  const handleBackClick = () => {
    const dashboardUrl = getDashboardUrl();
    if (hasUnsavedChanges) {
      setShowBackConfirmation(true);
    } else {
      window.location.href = `${dashboardUrl}/#home`;
    }
  };

  // Confirm discard and go back
  const handleConfirmBack = () => {
    setShowBackConfirmation(false);
    const dashboardUrl = getDashboardUrl();
    window.location.href = `${dashboardUrl}/#home`;
  };

  // mod-assistant-agent — render the launcher + panel regardless of
  // the block-screen gates below. Without this, users who land on
  // the WB without configured Harbor / Workers can't use the
  // assistant at all (so a cross-app open-dialog chain initiated on
  // Dashboard would silently no-op on the block screen). The panel
  // and launcher are both position:fixed + Portal-rendered so they
  // overlay any of the screens without disturbing layout.
  const assistantChrome = (
    <>
      <AssistantLauncher
        accessToken={api.accessToken}
        apiUrl={apiUrl()}
      />
      <AssistantPanel
        accessToken={api.accessToken}
        apiUrl={apiUrl()}
      />
    </>
  );

  // Shared access-denied dialog — rendered in every early-return gate so the
  // user always sees the denial message even when the main canvas never
  // renders (Q9=B: the dialog must be reachable from a token-less cold link).
  const accessDeniedDialog = (
    <>
      {/* No-access to workflow -> offer to request access from the owner */}
      <AlertDialog open={api.accessDenied !== null} onOpenChange={(open) => { if (!open) api.setAccessDenied(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>You don&apos;t have access to this workflow</AlertDialogTitle>
          <AlertDialogDescription>
            You don&apos;t have permission to use <strong>{api.accessDenied?.workflowName}</strong>. You can ask the workflow owner for access — they&apos;ll be notified to approve your request.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          {api.accessToken == null ? (
            /* Q41 (Q9=B follow-up): no valid login in this tab — "Request
               access" cannot work (requestWorkflowAccess returns false with
               no token), so offer "Sign in again", which starts the SAME
               login redirect the Q9 cold-link flow uses (startSilentSso)
               with the original workflow link as the return target. */
            <AlertDialogAction
              onClick={async () => {
                const wfId = api.accessDenied?.workflowId;
                api.setAccessDenied(null);
                if (wfId != null) await startSignInAgain(wfId);
              }}
            >
              Sign in again
            </AlertDialogAction>
          ) : (
            <AlertDialogAction
              onClick={async () => {
                const wfId = api.accessDenied?.workflowId;
                api.setAccessDenied(null);
                if (wfId != null) await api.requestWorkflowAccess(wfId);
              }}
            >
              Request access
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
      </AlertDialog>
    </>
  );

  // Block access if Harbor is not configured and user is not platform admin
  if (harborStatus.loading) {
    return (
      <>
        {assistantChrome}
        {accessDeniedDialog}
        <div className="flex h-screen items-center justify-center">
          <div className="flex flex-col items-center">
            <Loader2 className="h-16 w-16 animate-spin text-blue-500 mb-4" />
            <p className="text-lg font-semibold text-gray-700">Checking configuration...</p>
          </div>
        </div>
      </>
    );
  }

  // Block everyone if Harbor is not configured (including platform admins)
  // — except on CE (1900Y1p3): CE ships no Harbor, so unconfigured is
  // expected there and must not block the canvas. Paid trees unchanged.
  if (!harborStatus.configured && !harborStatus.ceEdition) {
    return (
      <>
      {assistantChrome}
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="max-w-2xl mx-auto p-8 bg-white rounded-lg shadow-lg border border-gray-200">
          <div className="flex flex-col items-center text-center">
            <div className="w-16 h-16 bg-orange-100 rounded-full flex items-center justify-center mb-6">
              <svg className="w-8 h-8 text-orange-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <h1 className="text-2xl font-bold text-gray-900 mb-3">
              Workflow Builder Not Available
            </h1>
            <p className="text-gray-600 mb-4 text-lg">
              The workflow builder requires platform configuration to be completed.
            </p>
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6 text-left">
              <p className="text-sm text-blue-900 font-medium mb-2">
                {harborStatus.isPlatformAdmin ? 'To enable the workflow builder:' : 'Platform setup in progress'}
              </p>
              {harborStatus.isPlatformAdmin ? (
                <p className="text-sm text-blue-800">
                  Please configure the container registry credentials in the <strong>Secrets</strong> tab of the Dashboard.
                </p>
              ) : (
                <p className="text-sm text-blue-800">
                  The platform is being configured by the system administrator. You can explore the interface, but some features may be unavailable until setup is complete.
                </p>
              )}
            </div>
            <div className="flex gap-3">
              <Button
                onClick={checkHarborStatus}
                variant="outline"
                className="mt-4"
                disabled={harborStatus.loading}
              >
                {harborStatus.loading ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <svg className="h-4 w-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                )}
                Check Again
              </Button>
              <Button
                onClick={() => {
                  const dashboardUrl = getDashboardUrl();
                  if (harborStatus.isPlatformAdmin) {
                    // Redirect platform admin directly to secrets tab
                    window.location.href = `${dashboardUrl}/#secrets`;
                  } else {
                    // Redirect regular users to home
                    window.location.href = `${dashboardUrl}/#home`;
                  }
                }}
                variant="default"
                className="mt-4"
              >
                <ArrowLeft className="h-4 w-4 mr-2" />
                {harborStatus.isPlatformAdmin ? "Go to Secrets Settings" : "Return to Dashboard"}
              </Button>
            </div>
          </div>
        </div>
      </div>
      {accessDeniedDialog}
      </>
    );
  }

  // Check if workers are being loaded
  if (workerStatus.loading || (!workerStatus.checked && harborStatus.configured)) {
    return (
      <>
      {assistantChrome}
      {accessDeniedDialog}
      <div className="flex h-screen items-center justify-center">
        <div className="flex flex-col items-center">
          <Loader2 className="h-16 w-16 animate-spin text-blue-500 mb-4" />
          <p className="text-lg font-semibold text-gray-700">Checking worker availability...</p>
        </div>
      </div>
      </>
    );
  }

  // Block access if this tenant has no workers assigned in the routing model (pool / assigned / owned).
  if (workerStatus.checked && !workerStatus.hasWorkers && workerStatus.onlineCount !== -1) {
    return (
      <>
      {assistantChrome}
      {accessDeniedDialog}
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="max-w-2xl mx-auto p-8 bg-white rounded-lg shadow-lg border border-gray-200">
          <div className="flex flex-col items-center text-center">
            <div className="w-16 h-16 bg-red-100 rounded-full flex items-center justify-center mb-6">
              <svg className="w-8 h-8 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
            </div>
            <h1 className="text-2xl font-bold text-gray-900 mb-3">
              No Workers Available
            </h1>
            <p className="text-gray-600 mb-4 text-lg">
              The workflow builder is unavailable until at least one worker is approved and routed to your tenant (platform pool, platform-assigned, or tenant-owned).
            </p>
            {workerStatus.error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4 text-left w-full">
                <p className="text-sm text-red-800 font-medium">Details</p>
                <p className="text-sm text-red-700 mt-1">{workerStatus.error}</p>
              </div>
            )}
            <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 mb-6 text-left w-full">
              <p className="text-sm text-amber-900 font-medium mb-2">
                Current Worker Status:
              </p>
              <ul className="text-sm text-amber-800 space-y-1">
                <li>• Workers routed to you (visible here): <strong>{workerStatus.totalCount}</strong></li>
                <li>• Active / connected: <strong>{workerStatus.licensedCount}</strong></li>
                <li>• Marked offline (stale): <strong>{workerStatus.offlineCount}</strong></li>
              </ul>
              {workerStatus.totalCount === 0 && (
                <p className="text-sm text-amber-800 mt-3">
                  No workers are routed to this tenant yet. A platform admin should approve a worker and assign it to this tenant (or use the platform pool). Ensure the mod-worker process is running and able to reach the API. On a single-box install the embedded worker (mod-worker-embedded) registers with the platform after it starts: refresh the page, or run docker compose ps mod-worker.
                </p>
              )}
              {workerStatus.totalCount > 0 && workerStatus.licensedCount === 0 && workerStatus.offlineCount > 0 && (
                <p className="text-sm text-amber-800 mt-3">
                  Workers are assigned but marked offline (no recent heartbeat). Restart the worker or check its connection to the API — schedules will not run until it reconnects.
                </p>
              )}
              {workerStatus.totalCount > 0 && workerStatus.licensedCount === 0 && workerStatus.offlineCount === 0 && (
                <p className="text-sm text-amber-800 mt-3">
                  No active workers matched the routing rules. Confirm approval and owner type (platform pool / assigned / tenant-owned) in Worker Management.
                </p>
              )}
            </div>
            <div className="flex gap-3">
              <Button
                // checkTenantWorkerAvailability accepts an optional opts
                // argument; wrap so the MouseEventHandler signature
                // (event-only) doesn't accidentally pass the click event
                // through as `opts`.
                onClick={() => checkTenantWorkerAvailability()}
                variant="outline"
                className="mt-4"
                disabled={workerStatus.loading}
              >
                {workerStatus.loading ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <svg className="h-4 w-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                )}
                Check Again
              </Button>
              <Button
                onClick={() => {
                  const dashboardUrl = getDashboardUrl();
                  // Redirect to workers tab within System page (analytics)
                  window.location.href = `${dashboardUrl}/?tab=workers#analytics`;
                }}
                variant="default"
                className="mt-4"
              >
                <svg className="h-4 w-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                </svg>
                Go to Worker Management
              </Button>
            </div>
          </div>
        </div>
      </div>
      </>
    );
  }

  // GY.C7 — top controlled banner: shown when the open workflow OR any
  // selected asset carries a controlled label (CUI/ITAR/EAR/etc.).
  const controlledLabel = worstLabel(
    [workflowLabel, ...selectedAssetLabels].filter((l): l is string => !!l),
  );
  return (
    <>
    {/* GSEC FX.54 p4b — SSO idle warning: opens at deadline-2min, Esc/Stay
        refreshes and re-arms, Sign out logs out, and the deadline itself logs
        out if nobody acts. */}
    <IdleWarningDialog
      open={idleOpen}
      secondsLeft={idleSeconds}
      onStay={() => idleWarning.stay()}
      onSignOut={() => idleWarning.signOut()}
    />
    {/* FX.79 p3a — CUI/ITAR data-marking banner on the open workflow
        (wording from services/marking_stamps.py); renders nothing when
        the workflow carries no live mark or the fetch fails.
        FX.163 p3b4b — `canClear` is the same tenant-admin / platform-admin
        verdict the /me gate above already resolved (is_platform_admin ||
        parent_user_id == null), so only admins get the per-label "Clear"
        button the DELETE route requires (manage_settings). */}
    <DataMarkingBanner
      resourceType="workflow"
      resourceId={api.currentWorkflow?.id ?? null}
      position="top"
      accessToken={api.accessToken}
      canClear={canClearMarking}
    />
    {/* expr 3.6 — extraction-suggestion info bar (read-only): repeated node
        groups in this workflow that also appear in other same-tenant
        workflows. Renders nothing when there are no suggestions, the
        endpoint is unmounted/errors, or the bar was dismissed this session. */}
    <ExtractionSuggestionsBanner
      workflowId={api.currentWorkflow?.id ?? null}
      accessToken={api.accessToken}
    />
    {/* GY.C7 — CUI-banner-convention marking; full-width, above the canvas. */}
    <ControlledBanner label={controlledLabel} />
    {/* Canvas height: 100dvh minus a 24px bottom buffer.
        - `dvh` (dynamic viewport) instead of `vh` so mobile address bars
          resizing don't push controls offscreen.
        - 1.5rem buffer keeps the ReactFlow Controls / MiniMap / bottom
          panels visible instead of flush against the viewport edge
          (where browser/OS chrome can clip them). */}
    <div className="flex relative flex-col" style={{ height: 'calc(100dvh - 1.5rem)' }}>
      {/* R-038 — screen-reader announcements for the canvas. Visually hidden
          but announced by aria-live. A trailing counter is appended by
          announceCanvas so consecutive identical sentences still re-announce
          (aria-live skips repeated identical text). */}
      <div
        id={WB_CANVAS_LIVE_REGION_ID}
        aria-live="polite"
        aria-atomic="true"
        role="status"
        className="sr-only"
      >
        {canvasLiveMessage}
      </div>
      {workerStatus.offlineCount > 0 && workerStatus.licensedCount === 0 && (
        <div className="w-full bg-amber-50 border-b border-amber-200 px-4 py-2 text-sm text-amber-900 shrink-0 z-[60]">
          <strong>Worker offline:</strong> This tenant has{' '}
          {workerStatus.offlineCount === 1 ? 'one worker' : `${workerStatus.offlineCount} workers`} assigned but
          none are currently active. Start or reconnect the worker service before running workflows.
        </div>
      )}
      <div className="flex flex-1 min-h-0 relative">
      {/* Blocking canvas overlay — only on first load. Refresh
          loads (which happen after every node creation + periodic
          sync) don't block the canvas; a subtle inline spinner in
          the sidebar's library header signals the refresh instead.
          Users who just finished the New Node wizard expect to see
          their canvas, not "Checking for updates…" */}
      {isLoadingNodes && !hasLoadedNodesBefore() && (
        <div className="absolute inset-0 bg-white/90 flex flex-col items-center justify-center z-50">
          <Loader2 className="h-16 w-16 animate-spin text-blue-500 mb-4" />
          <p className="text-lg font-semibold text-gray-700">Loading node library…</p>
          <p className="text-sm text-gray-600 mt-2">
            Fetching available workflow nodes from your tenant.
          </p>
        </div>
      )}
      {isLoadingNodes && hasLoadedNodesBefore() && (
        <div className="absolute top-2 right-2 z-50 bg-white/95 border border-gray-200 rounded-md px-3 py-1.5 flex items-center gap-2 text-xs text-gray-700 shadow-sm">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-500" />
          <span>Refreshing node library…</span>
        </div>
      )}
      {/* MOD-branded sidebar (issue #5). The canvas stays light for
          graph readability; the sidebar mirrors example.com's
          dark nav. The brand logo sits at the top, replacing the
          previous bare "Node Library" heading. Background, text,
          and hover surfaces flow from the --sidebar-* tokens
          defined in globals.css so a single token swap rebrands
          the whole strip. */}
      {/* Node library sidebar.
           - Width comes from the `sidebarWidth` state, driven by the
             drag handle to the right. flex-shrink-0 holds the assigned
             width under canvas pressure; without it the flex parent
             could compress the sidebar back below SIDEBAR_MIN_WIDTH.
           - min-h-0 lets the inner ScrollArea bound itself correctly
             (without it, a child with flex-grow can blow past the
             parent in some flex layouts, which clips the last few
             rows at the sidebar bottom).
           - pb-6 on the inner scroll (see node-library.tsx) gives
             the last visible row breathing room. */}
      <nav
        role="navigation"
        aria-label="Workflow builder node library"
        style={{ width: sidebarWidth }}
        className="flex-shrink-0 border-r p-4 bg-sidebar text-sidebar-foreground flex flex-col overflow-hidden min-h-0"
      >
        <div className="flex-shrink-0">
            {/* Brand logo, theme-aware: navy on the light sidebar, white when
                bg-sidebar darkens in dark mode. Both PNGs are recolored from the
                single white source asset; the dotted-O tile is the favicon. */}
            <div className="mb-4 flex items-center justify-center px-2" aria-label="MOD">
              <img
                src="/branding/logos/mod-logo-navy.png"
                alt="MOD"
                className="h-16 w-auto wb-wordmark block dark:hidden"
              />
              <img
                src="/branding/logos/mod-logo-white.png"
                alt="MOD"
                className="h-16 w-auto wb-wordmark hidden dark:block"
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              className="mb-4 w-full bg-transparent border-sidebar-border text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              onClick={handleBackClick}
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              {wbNavLabel("wb-nav-dashboard", "Back to Home", undefined)}
            </Button>
            <h2 className="text-lg font-semibold mb-4" data-theme-key="wb-nav-node-library">
              {wbNavLabel("wb-nav-node-library", "Node Library", undefined)}
            </h2>
        </div>
        {/* FX.68.p3 [U] — keyboard palette add: Enter on a focused palette row
            drops that node on the canvas centre through the hook's addNodeAtCenter
            (same createNode as the drag's onDrop). */}
        <NodeLibrary
          accessToken={api.accessToken}
          onLoadingChange={handleNodeLibraryLoadingChange}
          hasLoadedBefore={hasLoadedNodesBefore()}
          onKeyboardAddNode={(node) => events.addNodeAtCenter(node)}
          onOpenWizard={() => {
            // The notification-resume path leaves resumeTaskId set in
            // state. Without this clear, "+ New Node" reopens at the
            // previous wizard's Preview step instead of a fresh form.
            // Also wipe the wizard's localStorage-persisted state AND
            // bump wizardOpenCount so the wizard force-remounts with
            // empty in-memory useState (without the bump, the dialog
            // just toggles `open` and stale state survives).
            setResumeTaskId(null);
            setResumeNodeName(undefined);
            setEditNodeId(null);
            setEditNodeName(undefined);
            clearPersistedWizardState();
            setDebugRunId(null);
            setWizardOpenCount((c) => c + 1);
            setWizardOpen(true);
          }}
          onOpenQueue={() => setQueueOpen(true)}
          onOpenProcessQueue={() => setIsQueueDialogOpen(true)}
          onOpenApprovals={() => setIsApprovalsOpen(true)}
          onOpenTriggers={() => setTriggersOpen(true)}
          // CE: the store dialog is denied in CE () - no functions-menu entry
          onOpenWorkflowBuilder={() => setIsWizardPanelOpen(true)}
          // CE: no Project Wizard functions-menu entry ()
          // CE: the Stack Wizard entry is pulled from the builder in CE () - no functions-menu entry
          onEditNodeCode={(nodeId, nodeName) => {
            // Same fresh-mount story as onOpenWizard: clear any stale
            // resume state + localStorage draft and bump wizardOpenCount
            // so the wizard remounts, then set editNodeId/editNodeName
            // so it opens straight into "edit existing node code" mode
            // instead of the normal create-a-node flow.
            setResumeTaskId(null);
            setResumeNodeName(undefined);
            setResumeWizardSeed(null);
            clearPersistedWizardState();
            setEditNodeId(nodeId);
            setEditNodeName(nodeName);
            setDebugRunId(null);
            setWizardOpenCount((c) => c + 1);
            setWizardOpen(true);
          }}
          queueCount={inProgressCount}
        />
      </nav>

      {/* Sidebar resize handle. 4px wide divider with a wider invisible
          hit area is overkill here — 4px is comfortable to grab and
          keeps the visual seam tight. Mouse events go to document
          (not the handle), so the drag survives even when the cursor
          briefly leaves the strip. */}
      <div
        onMouseDown={handleSidebarResizeStart}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize node library"
        className="w-1 flex-shrink-0 cursor-col-resize bg-transparent hover:bg-indigo-300 active:bg-indigo-400 transition-colors"
      />

      <div
        className="relative flex-1 flex flex-col min-w-0"
        ref={events.reactFlowWrapper}
        onKeyDown={handleCanvasKeyDown}
      >
        {api.sealedWorkflowId ? (
          <SealedAppForm
            workflowId={api.sealedWorkflowId}
            apiUrl={apiUrl()}
            accessToken={api.accessToken}
            isAdmin={canMarketplace}
          />
        ) : (<>
        <NodeStackBindingsProvider value={stackBindings.bindings} stacks={stackBindings.stacks}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          // History commit points: a completed drag (snapshot taken at drag
          // start, recorded on stop only if anything moved) and deletions of
          // nodes/edges. Position spam during a drag flows through
          // onNodesChange and is deliberately NOT snapshotted.
          onNodeDragStart={beginNodeDrag}
          onNodeDragStop={endNodeDrag}
          onNodesDelete={() => commit()}
          onEdgesDelete={() => commit()}
          onPaneClick={() => { setSelectedNode(null); events.onPaneClick(); setNodeCtxMenu(null); }}
          onNodeContextMenu={onNodeContextMenu}
          // Pull only the ReactFlow-recognized handlers off `events`
          // (it also returns `reactFlowWrapper`, `menu`, etc. which
          // aren't ReactFlow props and would fail the JSX type-check).
          onConnect={events.onConnect}
          isValidConnection={events.isValidConnection}
          onConnectStart={events.onConnectStart}
          onConnectEnd={events.onConnectEnd}
          onDrop={events.onDrop}
          onDragOver={events.onDragOver}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          // R-038 — make node wrappers keyboard-focusable (tabIndex=0) so Tab
          // reaches each node; React Flow then handles Enter/Space (select),
          // Delete (deleteKeyCode above) and arrow-key movement natively.
          nodesFocusable
          deleteKeyCode={['Backspace', 'Delete']}
        >
          <Background />
          <Controls />
          <MiniMap style={{ bottom: 10, right: 10 }} />
          <Panel position="bottom-right" style={{ marginBottom: '140px', marginRight: '10px' }}>
            {/* Legend surface uses Tailwind utility classes (bg-white,
                bg-gray-50, border-gray-200) instead of inline styles so
                the dark-mode override layer in app/globals.css can remap
                them to card / muted / border tokens. Swatch dots stay
                inline — their colors are semantic. */}
            <div
              className="bg-white border border-gray-200"
              style={{
                borderRadius: '4px',
                boxShadow: '0 1px 2px 0 rgba(0, 0, 0, 0.05)',
                overflow: 'hidden'
              }}
            >
              <button
                onClick={() => setIsLegendExpanded(!isLegendExpanded)}
                className={`bg-gray-50 ${isLegendExpanded ? 'border-b border-gray-200' : ''}`}
                style={{
                  width: '100%',
                  padding: '6px 10px',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: 600,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between'
                }}
              >
                <span>Legend</span>
                <span>{isLegendExpanded ? '▼' : '▲'}</span>
              </button>

              {isLegendExpanded && (
                <div style={{ padding: '10px', fontSize: '11px', maxWidth: '220px' }}>
                  {/* Handle Shapes Section */}
                  <div style={{ marginBottom: '12px' }}>
                    <div className="text-gray-600" style={{ fontWeight: 600, marginBottom: '6px' }}>Handle Shapes</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'rgb(107, 114, 128)' }}></div>
                        <span>Standard handle</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'transparent', border: '2px solid rgb(107, 114, 128)' }}></div>
                        <span>Single file</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '2px', backgroundColor: 'rgb(107, 114, 128)' }}></div>
                        <span>Multiple Files / Sequence</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '3px', backgroundColor: 'rgb(107, 114, 128)' }}></div>
                        <span>Both single & multiple</span>
                      </div>
                    </div>
                  </div>

                  {/* Data Type Colors Section */}
                  <div style={{ marginBottom: '12px' }}>
                    <div className="text-gray-600" style={{ fontWeight: 600, marginBottom: '6px' }}>Data Types</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#3b82f6' }}></div>
                        <span>IMAGE</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#22c55e' }}></div>
                        <span>MESH</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#a855f7' }}></div>
                        <span>TEXT/STRING</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#f97316' }}></div>
                        <span>VIDEO</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#ec4899' }}></div>
                        <span>AUDIO</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#14b8a6' }}></div>
                        <span>NUMBER</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#6366f1' }}></div>
                        <span>JSON/OBJECT</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: '#d946ef' }}></div>
                        <span>ANY</span>
                      </div>
                    </div>
                  </div>

                  {/* Node Status Section */}
                  <div>
                    <div className="text-gray-600" style={{ fontWeight: 600, marginBottom: '6px' }}>Execution Status</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'rgb(168, 85, 247)' }}></div>
                        <span>Queued (waiting)</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'rgb(74, 222, 128)', animation: 'pulse 2s infinite' }}></div>
                        <span>Processing</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'rgb(34, 197, 94)' }}></div>
                        <span>Completed</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ width: '12px', height: '12px', borderRadius: '50%', backgroundColor: 'rgb(239, 68, 68)' }}></div>
                        <span>Failed</span>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </Panel>
          {/* Workflow-name pill — pinned to the canvas top-left so it
              sits visually adjacent to the node library panel rather
              than crowding the right-side action toolbar. Hidden when
              no workflow is loaded. */}
          {api.workflowName && (
            <div
              style={{ position: 'absolute', top: 10, left: 10, zIndex: 5 }}
              className="text-sm font-semibold px-3 py-1.5 bg-white border border-gray-200 rounded-md shadow-sm"
            >
              <span className="inline-flex items-center gap-1.5">
                {api.workflowName}
                {api.currentVersionNumber ? ` (v${api.currentVersionNumber})` : null}
                {/* GY.C7 — workflow-level data-label chip (controlled labels
                    render loudly; public/internal subtly). */}
                <DataLabelChip label={workflowLabel} subtleDefault={false} />
                {/* UI-PRIORITY-CLASS-BADGE rework — READ-ONLY priority-class
                    badge (GET /v1/road/{id}/priority-class via useWorkflowApi).
                    Hidden for 'normal'/unknown: only interactive/high/low show.
                    Classes order capacity-queued runs and route interactive
                    tasks through the lighter placement; they never make an
                    ordinary run start faster. No setter here (read-only). */}
                <PriorityClassChip priorityClass={api.priorityClass} />
              </span>
            </div>
          )}
           {events.menu && (
              <div style={{ top: events.menu.top, left: events.menu.left, position: "fixed", zIndex: 10 }} className="w-64">
                <Command className="rounded-lg border shadow-md">
                  <CommandInput placeholder="Search for a node..." autoFocus />
                  <CommandList>
                    <CommandEmpty>No compatible nodes found.</CommandEmpty>
                    {recommendedNodes.map((nodeDef) => (
                      <CommandItem key={nodeDef.id} onSelect={() => events.addRecommendedNode(nodeDef)}>
                        <span>{nodeDef.name}</span>
                      </CommandItem>
                    ))}
                  </CommandList>
                </Command>
              </div>
            )}
        </ReactFlow>
        </NodeStackBindingsProvider>
          <div style={{ position: 'absolute', top: 10, right: 10, zIndex: 5 }} className="flex items-center gap-2">
            {/* Standalone Workflow Builder (formerly "Wizard") button
                removed — entry point is now the Functions menu on the
                node-library bar, second-from-top. The panel itself
                (<WorkflowWizardPanel />) still renders below; only the
                trigger surface moved. */}
            <WorkflowToolbar
                onNewWorkflow={() => {
                    // LDF-3 §2.2 — same unsaved-changes guard the
                    // back-button uses; on confirm the dialog calls
                    // api.startNewWorkflow() (see handleConfirmBack
                    // alongside).
                    if (hasUnsavedChanges) {
                        setShowNewConfirmation(true);
                    } else {
                        api.startNewWorkflow();
                    }
                }}
                onUndo={undo}
                onRedo={redo}
                canUndo={canUndo}
                canRedo={canRedo}
                onSave={api.handleSaveVersion}
                onSaveAs={() => api.handleSaveInitiation('saveAs')}
                onLoad={api.handleLoadWorkflowList}
                onEdit={api.handleOpenEditDialog}
                onExecute={api.executeWorkflow}
                activeRunId={api.pollingRunId}
                onCancelRun={api.cancelActiveRun}
                selectedRunId={api.selectedRunId}
                onSelectRun={api.selectRun}
                onExplainRun={debugEnabled ? handleExplainRun : undefined}
                freshRun={api.freshRun}
                onFreshRunChange={api.setFreshRun}
                schedulePaused={schedulePaused}
                onSchedulePauseChange={(p: boolean) => { setSchedulePaused(p); api.pauseSchedule(p); }}
                onPausedNodesChange={api.setPausedNodes}
                onQueue={() => setIsQueueDialogOpen(true)}
                onExport={api.handleExportWorkflow}
                onImportFile={api.handleImportFile}
                onSchedule={async (cron: string) => {
                    if (!api.currentWorkflow?.id) {
                        alert("Save the workflow first before scheduling.");
                        return;
                    }
                    try {
                        const apiUrl = getApiUrl();
                        const resp = await authenticatedFetch(`${apiUrl}/road/triggers/`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                workflow_id: api.currentWorkflow.id,
                                trigger_type: "schedule",
                                configuration: { cron, timezone: "UTC" },
                                is_active: true,
                            }),
                        });
                        if (!resp.ok) {
                            const err = await resp.json();
                            alert(err.detail || "Failed to create schedule");
                            return;
                        }
                        alert(`Scheduled! Cron: ${cron} (UTC)`);
                    } catch (e) {
                        alert("Failed to create schedule trigger");
                    }
                }}
                isWorkflowLoaded={!!api.currentWorkflow}
                workflowName={api.workflowName}
                versionNumber={api.currentVersionNumber || undefined}
                versionId={api.currentVersionId ?? null}
                workflowId={api.currentWorkflow?.id}
                nodes={nodes}
                edges={edges}
                accessToken={api.accessToken}
                fileCount={1}
            />
            {/* mod-assistant-agent §7 — robot launcher, left of the bell. */}
            <AssistantLauncher
              accessToken={api.accessToken}
              apiUrl={apiUrl()}
            />
            {/* mod-assistant-agent §8 — slide-in chat panel. Hooks
                into the `assistant-panel-open` body class the launcher
                toggles; position:fixed so its tree location doesn't
                affect layout. */}
            <AssistantPanel
              accessToken={api.accessToken}
              apiUrl={apiUrl()}
            />
            {/* Light/dark toggle — left of the bell, right of the assistant
                launcher. Syncs across surfaces via the mod_theme cookie. */}
            <ThemeToggleButton />
            <NotificationsBell
              accessToken={api.accessToken}
              apiUrl={apiUrl()}
              onOpenQueue={() => setQueueOpen(true)}
              onResumeGeneration={(taskId) => {
                clearPersistedWizardState();
                setResumeTaskId(taskId);
                const queued = builds.find(
                  (b) => b.taskId === taskId || b.generationTaskId === taskId,
                );
                setResumeNodeName(queued?.nodeName);
                setEditNodeId(null);
                setEditNodeName(undefined);
                setDebugRunId(null);
                setWizardOpenCount((c) => c + 1);
                setWizardOpen(true);
              }}
            />
          </div>
        </>
        )}
        {nodeCtxMenu && (
          <>
            {/* click-away layer */}
            <div className="fixed inset-0 z-40" onClick={() => setNodeCtxMenu(null)} onContextMenu={(e) => { e.preventDefault(); setNodeCtxMenu(null); }} />
            {/* FX.68.p3 [U] — role=menu so a screen reader announces the
                keyboard-opened context menu as a menu, not a floating div. */}
            <div
              ref={nodeCtxMenuContainerRef}
              role="menu"
              className="fixed z-50 min-w-[140px] rounded-md border border-gray-200 bg-white py-1 shadow-lg text-sm"
              style={{
                top: nodeCtxMenu.y,
                left: nodeCtxMenu.x,
                transform: `translate(${nodeCtxMenuShift.dx}px, ${nodeCtxMenuShift.dy}px)`,
              }}
            >
              <button
                type="button"
                ref={nodeCtxMenuFirstItemRef}
                className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                onClick={() => {
                  const target = nodeCtxMenu.node;
                  // Duplicate the whole selection when the clicked node is part
                  // of it; otherwise just the clicked node.
                  handleDuplicate(target.selected ? undefined : target);
                  setNodeCtxMenu(null);
                }}
              >
                Duplicate
              </button>
              {/* sz-2.10d — breakpoint toggle. Needs a saved version (the API
                  is keyed by workflow + version ids); unsaved workflows get a
                  disabled item that explains why. */}
              {api.currentWorkflow?.id && api.currentVersionId ? (
                <button
                  type="button"
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  title={breakpoints.isActive(nodeCtxMenu.node.id)
                    ? "Remove this node's breakpoint — the run no longer stops here."
                    : "Stop runs at this node so you can inspect its inputs/outputs before it executes."}
                  onClick={() => {
                    void breakpoints.toggle(nodeCtxMenu.node.id);
                    setNodeCtxMenu(null);
                  }}
                >
                  {breakpointMenuLabel(breakpoints.isActive(nodeCtxMenu.node.id))}
                </button>
              ) : (
                <button
                  type="button"
                  title="Save the workflow to set breakpoints"
                  className="block w-full px-3 py-1.5 text-left text-gray-400 hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  onClick={() => setNodeCtxMenu(null)}
                >
                  Add breakpoint
                </button>
              )}
              {api.selectedRunId != null && api.pausedNodes.includes(nodeCtxMenu.node.id) && (
                <button
                  type="button"
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  title="Inspect this paused node's frame — the exact inputs/outputs it stopped with."
                  onClick={() => {
                    setInspectFrame({ runId: api.selectedRunId!, nodeId: nodeCtxMenu.node.id });
                    setNodeCtxMenu(null);
                  }}
                >
                  Inspect frame
                </button>
              )}
              {/* R-038 — keyboard connect. Same validity gate + onConnect as
                  the drag path; lists every compatible node as a button so
                  composing a wire needs no mouse. */}
              {(() => {
                const targets = computeConnectTargets(nodeCtxMenu.node.id);
                if (targets.length === 0) return null;
                return (
                  <>
                    <div className="my-1 border-t border-gray-100" />
                    <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400">
                      Connect this node to…
                    </div>
                    {targets.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                        title={t.fromSource ? "Wire this node's output into that node's input" : "Wire that node's output into this node's input"}
                        onClick={() => {
                          connectKeyboard(nodeCtxMenu.node.id, t.id, t.fromSource);
                          setNodeCtxMenu(null);
                        }}
                      >
                        {t.fromSource ? "→" : "←"} {t.label}
                      </button>
                    ))}
                  </>
                );
              })()}
              {api.selectedRunId != null && (
                <button
                  type="button"
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  title="Start a new run that reuses unchanged upstream results and re-runs this node + everything downstream (picks up edits you made here)."
                  onClick={() => {
                    api.restartFromNode(nodeCtxMenu.node.id);
                    setNodeCtxMenu(null);
                  }}
                >
                  Restart from here
                </button>
              )}
              {api.selectedRunId != null && (
                api.pausedNodes.includes(nodeCtxMenu.node.id) ? (
                  <button
                    type="button"
                    className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                    title="Clear this pause point — the run continues past this node."
                    onClick={() => { api.resumeNode(nodeCtxMenu.node.id); setNodeCtxMenu(null); }}
                  >
                    Resume here
                  </button>
                ) : (
                  <button
                    type="button"
                    className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                    title="Pause the run when it reaches this node — it stops and waits here until you resume."
                    onClick={() => { api.pauseNode(nodeCtxMenu.node.id); setNodeCtxMenu(null); }}
                  >
                    Pause here
                  </button>
                )
              )}
              {/* node-run-controls — Airflow-style in-run actions (need a selected run) */}
              {api.selectedRunId != null && (() => {
                const nid = nodeCtxMenu.node.id;
                const close = () => setNodeCtxMenu(null);
                const act = (a: string) => { api.nodeAction(nid, a); close(); };
                const confirmAct = (a: string, msg: string) => { if (window.confirm(msg)) api.nodeAction(nid, a); close(); };
                const Item = ({ label, onClick, danger, title }: any) => (
                  <button type="button" title={title}
                    className={`block w-full px-3 py-1.5 text-left hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${danger ? "text-red-600" : ""}`}
                    onClick={onClick}>{label}</button>
                );
                return (
                  <>
                    <div className="my-1 border-t border-gray-100" />
                    <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400">Flow</div>
                    <Item label="Skip (mark success)" title="Bypass this node so the run continues past it." onClick={() => act("skip")} />
                    <Item label="Force-fail" danger title="Fail this node and block everything downstream." onClick={() => act("force_fail")} />
                    <Item label="Cancel downstream — now" danger title="Cancel queued/running tasks after this node immediately."
                      onClick={() => confirmAct("cancel_downstream_now", "Cancel everything downstream of this node now?")} />
                    <Item label="Cancel downstream — after current" title="Let the current node finish, then stop downstream."
                      onClick={() => act("cancel_downstream_drain")} />
                    <div className="my-1 border-t border-gray-100" />
                    <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400">Recompute</div>
                    <Item label="Re-run (same data)" title="Re-run this node with identical inputs (new attempt, prior kept)." onClick={() => act("rerun_same_data")} />
                    <Item label="Re-run + downstream" title="Re-run this node and everything downstream." onClick={() => act("rerun_with_downstream")} />
                    <div className="my-1 border-t border-gray-100" />
                    <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400">Data</div>
                    <Item label="Clear cache" title="Invalidate this node's cached output so it recomputes on next reuse (no files deleted)." onClick={() => act("clear_cache")} />
                    <Item label="Output versions…" title="View every attempt's outputs (kept, never overwritten) and download any to see what went wrong."
                      onClick={() => { setVersionsNodeId(nid); close(); }} />
                  </>
                );
              })()}
            </div>
          </>
        )}
      </div>

      {inspectFrame && (
        <FrameInspectorPanel
          apiUrl={getApiUrl()}
          accessToken={api.accessToken ?? ""}
          runId={inspectFrame.runId}
          nodeId={inspectFrame.nodeId}
          onClose={() => setInspectFrame(null)}
        />
      )}

      {/* CE: the Project Wizard panel is pulled from the builder in CE () - no panel mount */}
      {/* CE: the Stack Wizard panel is pulled from the builder in CE () - no panel mount */}
      <WorkflowWizardPanel
        open={isWizardPanelOpen}
        onClose={() => { setIsWizardPanelOpen(false); setWorkflowSeedPrompt(undefined); setDebugRunId(null); }}
        debugRunId={debugRunId}
        accessToken={api.accessToken}
        initialPrompt={workflowSeedPrompt}
        // Canvas-aware-workflow-wizard §8: serialise the live canvas
        // on every wizard send so the server can route to suggest_next /
        // fill_gap / refactor_selection / audit_graph instead of from-
        // scratch. We pass a getter, not props, so the panel pulls
        // FRESH state on each turn (props would stale-close).
        getCanvas={() => serialiseCanvasForWizard(nodes, edges)}
        currentWorkflowId={api.currentWorkflow?.id ?? null}
        currentVersionId={api.currentVersionId ?? null}
        onApplied={async (workflowId, versionId, name, applyMode) => {
          // LDF-3 §1.4-1.5: load the freshly-applied workflow into the
          // canvas state (not just nodes/edges — also currentWorkflow,
          // workflowName, currentVersionNumber, versions list) so the
          // user lands on the same place a regular Load would put them,
          // with every pasted node selected for grouped move.
          //
          // Replace closes the panel (today's behavior). Merge keeps
          // the panel open so the user can issue follow-up wizard
          // turns against the freshly-merged graph without re-opening
          // (canvas-aware-workflow-wizard §10.3 / D5).
          await api.handleWizardApplied(workflowId, versionId, name);
          if (applyMode !== "merge") {
            setIsWizardPanelOpen(false);
          }
        }}
        onBuildMissingNode={(missingName, hint) => {
          // Issue #20 polish + workflow-wizard-missing-node-handoff:
          // clicking Build closes the Workflow Wizard and opens the
          // New Node Wizard pre-filled. When called from the eager
          // handoff panel, `hint` carries the LLM's structured guess
          // (label / purpose / suggested_source_type /
          // suggested_source / schema hints) which the New Node
          // Wizard hydrates onto its form so the user clicks Build
          // instead of re-entering the problem. When called from the
          // legacy 422 missing_nodes recovery path, hint is undefined
          // and the wizard opens with just the name pre-filled.
          setIsWizardPanelOpen(false);
          clearPersistedWizardState();
          setResumeTaskId(null);
          setResumeNodeName(missingName);
          setResumeWizardSeed(hint ? {
            sourceType: (hint.suggested_source_type as
              "git" | "image" | "dockerfile" | "script" | "describe" | undefined) ?? undefined,
            source: hint.suggested_source || "",
            purpose: hint.purpose || "",
          } : null);
          setEditNodeId(null);
          setEditNodeName(undefined);
          setDebugRunId(null);
          setWizardOpenCount((c) => c + 1);
          setWizardOpen(true);
        }}
        onAllMissingDispatched={(tasks) => {
          // Issue #20 / task 8.1 — the Workflow Wizard auto-queued
          // a Containerizer task per missing node. Drop a Phase 1
          // (generation) row into the queue per dispatch so the
          // user can watch progress, then open the Build Queue
          // panel so the rows are immediately visible.
          for (const t of tasks) {
            if (typeof t.task_id !== "number" || !t.name) continue;
            addBuild({
              taskId: t.task_id,
              nodeName: t.name,
              phase: "generation",
              startedAt: Date.now(),
            });
          }
          setQueueOpen(true);
        }}
      />

      {selectedNode && (
        <div className="w-80 border-l p-4 bg-gray-50">
          <NodeConfigPanel
            node={selectedNode as WorkflowNode}
            updateNodeData={updateNodeData}
            onClose={() => setSelectedNode(null)}
            accessToken={api.accessToken}
            pollingRunId={api.pollingRunId}
            selectedRunId={api.selectedRunId}
            isPaused={api.pausedNodes.includes((selectedNode as WorkflowNode).id)}
            onRestartFromNode={api.restartFromNode}
            onPauseNode={api.pauseNode}
            onResumeNode={api.resumeNode}
          />
        </div>
      )}

      <WorkflowDialogs {...api} />

      {/* Fan-out inspector — per-file input→output pairs for a fanned node */}
      <FanoutModal
        open={fanoutNodeId !== null}
        onClose={() => setFanoutNodeId(null)}
        runId={api.selectedRunId}
        nodeId={fanoutNodeId}
        nodeName={
          fanoutNodeId
            ? (nodes.find((n) => n.id === fanoutNodeId)?.data?.label as string) ||
              (nodes.find((n) => n.id === fanoutNodeId)?.data?.name as string) ||
              fanoutNodeId
            : null
        }
        apiUrl={apiUrl()}
        accessToken={api.accessToken}
      />

      {/* node-run-controls — Output versions (keep + version) */}
      <NodeVersionsModal
        open={versionsNodeId !== null}
        onClose={() => setVersionsNodeId(null)}
        runId={api.selectedRunId}
        nodeId={versionsNodeId}
        nodeName={
          versionsNodeId
            ? (nodes.find((n) => n.id === versionsNodeId)?.data?.label as string) ||
              (nodes.find((n) => n.id === versionsNodeId)?.data?.name as string) ||
              versionsNodeId
            : null
        }
        apiUrl={apiUrl()}
        accessToken={api.accessToken}
      />

      {/* Missing file input dialog */}
      <AlertDialog open={api.isMissingFileDialogOpen} onOpenChange={api.setMissingFileDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>No Input Selected</AlertDialogTitle>
            <AlertDialogDescription>
              The input node <strong>{api.missingFileNodeName}</strong> has no files selected. Please click the node and select files before running the workflow.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction>OK</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {accessDeniedDialog}

      {/* Run blocked — access denied (secrets/input files) or purchase
          required (GG.UI.6b entitlement pre-flight). The purchase case shows
          the BlockedRunPanel's Buy/Try CTAs instead of request-access rows. */}
      <AlertDialog open={api.runBlocked !== null} onOpenChange={(open) => { if (!open) { api.setRunBlocked(null); setRequestedRunItems(new Set()); } }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {api.runBlocked?.purchaseReasons ? "Can't run — items need to be purchased" : "Can't run — access needed"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {api.runBlocked?.purchaseReasons
                ? (api.runBlocked?.message || "This run needs items you don't own yet.")
                : (api.runBlocked?.message + " Request access to the items below; you'll be able to run once they're granted.")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {api.runBlocked?.purchaseReasons ? (
            <div className="py-1">
              <BlockedRunPanel reasons={api.runBlocked.purchaseReasons} isAdmin={canMarketplace} />
            </div>
          ) : (
          <div className="space-y-2 max-h-64 overflow-y-auto py-1">
            {(api.runBlocked?.secrets || []).map((name) => {
              const key = `secret:${name}`;
              const done = requestedRunItems.has(key);
              return (
                <div key={key} className="flex items-center justify-between gap-2 text-sm border rounded px-2 py-1">
                  <span className="truncate">🔒 Secret <span className="font-mono">{name}</span></span>
                  {done ? <span className="text-xs font-medium text-amber-700 whitespace-nowrap">Requested ✓</span> : (
                    <button type="button" className="text-xs font-medium text-primary hover:underline whitespace-nowrap"
                      onClick={async () => { await api.requestRunSecretAccess(name); setRequestedRunItems(prev => new Set(prev).add(key)); }}>
                      Request access
                    </button>
                  )}
                </div>
              );
            })}
            {(api.runBlocked?.cogIds || []).map((cid) => {
              const key = `file:${cid}`;
              const done = requestedRunItems.has(key);
              return (
                <div key={key} className="flex items-center justify-between gap-2 text-sm border rounded px-2 py-1">
                  <span className="truncate">📄 Input file <span className="font-mono">#{cid}</span></span>
                  {done ? <span className="text-xs font-medium text-amber-700 whitespace-nowrap">Requested ✓</span> : (
                    <button type="button" className="text-xs font-medium text-primary hover:underline whitespace-nowrap"
                      onClick={async () => { await api.requestRunFileAccess(cid); setRequestedRunItems(prev => new Set(prev).add(key)); }}>
                      Request access
                    </button>
                  )}
                </div>
              );
            })}
          </div>
          )}
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => { api.setRunBlocked(null); setRequestedRunItems(new Set()); }}>
              {api.runBlocked?.purchaseReasons ? "Go to the store" : "Close"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Save-required-before-run dialog — the workflow has never been
          saved, so there's nothing to run yet. Prompt to save first
          instead of a silent dead Execute button. */}
      <AlertDialog open={api.saveRequiredBeforeRunOpen} onOpenChange={api.setSaveRequiredBeforeRunOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Save Before Running</AlertDialogTitle>
            <AlertDialogDescription>
              This workflow hasn&apos;t been saved yet. Save it first, then run it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={api.confirmSaveRequiredBeforeRun}>
              Save workflow
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Save before run confirmation dialog */}
      <AlertDialog open={api.saveBeforeRunOpen} onOpenChange={api.setSaveBeforeRunOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unsaved Changes</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved changes. Would you like to save before running?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={api.confirmSaveAndRun}>
              Save & Run
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* LDF-3 §2.2 — `+ New` confirm dialog. Mirrors the back-button
          flow: dirty canvas → confirm before discarding. */}
      <AlertDialog open={showNewConfirmation} onOpenChange={setShowNewConfirmation}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unsaved Changes</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved changes. Starting a new workflow will discard them. Continue?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => {
              setShowNewConfirmation(false);
              api.startNewWorkflow();
            }}>
              Discard & New
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Back button confirmation dialog */}
      <AlertDialog open={showBackConfirmation} onOpenChange={setShowBackConfirmation}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unsaved Changes</AlertDialogTitle>
            <AlertDialogDescription>
              You have unsaved changes in your workflow. If you go back now, you will lose all your work. Are you sure you want to continue?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmBack}>
              Discard Changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <FileBrowser
        isOpen={isFileBrowserOpen}
        onClose={() => {
          setIsFileBrowserOpen(false);
          setFileBrowserContext({ nodeId: null });
        }}
        accessToken={api.accessToken}
        onFileSelect={handleFileSelect}
        inputType={fileBrowserContext.inputType}
        allowMultiple={fileBrowserContext.allowMultiple}
        nodeId={fileBrowserContext.nodeId || undefined}
        isMetadata={fileBrowserContext.isMetadata}
      />

      <PendingApprovalsDialog
        isOpen={isApprovalsOpen}
        onClose={() => setIsApprovalsOpen(false)}
        accessToken={api.accessToken}
        onOpenWorkflow={(workflowId, versionId, runId) => {
          setIsApprovalsOpen(false);
          api.watchRun(workflowId, versionId, runId);
        }}
        onDecided={() => {
          // Deliberately nothing. The canvas already polls the watched run, so
          // it picks the new state up on its own. This previously called
          // handleLoadWorkflowList to "refresh" — that function ends with
          // setDialogView("workflows"), so every approve and reject threw the
          // Load Workflow dialog in the user's face.
        }}
      />

      <WorkflowQueueDialog
        isOpen={isQueueDialogOpen}
        onClose={() => setIsQueueDialogOpen(false)}
        accessToken={api.accessToken}
        onWatch={(workflowId, versionId, runId) => {
          setIsQueueDialogOpen(false);
          api.watchRun(workflowId, versionId, runId);
        }}
      />

      <TriggersDialog
        isOpen={triggersOpen}
        onClose={() => setTriggersOpen(false)}
        accessToken={api.accessToken}
        currentWorkflowId={api.currentWorkflow?.id ?? null}
        currentWorkflowName={api.workflowName}
      />

      {/* CE: the store dialog is denied in CE () - no store popup mount */}

      {/* Cancel and Restart Confirmation Dialog */}
      <AlertDialog open={api.isConfirmCancelRestartDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>This workflow already has a run in progress</AlertDialogTitle>
            <AlertDialogDescription>
              There is a current run (status: {api.activeRunInfo?.status}).
              {api.activeRunInfo?.started_at && (
                <span className="block mt-1 text-sm">
                  Started: {new Date(api.activeRunInfo.started_at).toLocaleString()}
                </span>
              )}
              <span className="block mt-2">
                Start another run alongside it, or stop the current one?
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={api.dismissCancelRestartDialog}>
              Never mind
            </AlertDialogCancel>
            <Button
              variant="outline"
              onClick={api.stopCurrentRunFromDialog}
              className="border-destructive text-destructive hover:bg-destructive/10"
            >
              Stop current run
            </Button>
            <AlertDialogAction onClick={api.executeNewRunAnyway}>
              Execute new run
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Wizard + Wizard Queue panel at the stable parent level — do
          NOT move these back inside NodeLibrary. That component's
          `key` is bumped on node refresh, which would unmount the
          wizard (and wipe its step state) whenever anything refreshed
          the library. Keep them mounted here across all library
          re-keys. */}
      <NewNodeWizard
        key={`wizard-${wizardOpenCount}`}
        open={wizardOpen}
        onClose={() => {
          setWizardOpen(false);
          setDebugRunId(null);
          setResumeTaskId(null);
          setResumeNodeName(undefined);
          setResumeWizardSeed(null);
          setEditNodeId(null);
          setEditNodeName(undefined);
        }}
        resumeTaskId={resumeTaskId}
        resumeNodeName={resumeNodeName}
        resumeWizardSeed={resumeWizardSeed}
        editNodeId={editNodeId}
        editNodeName={editNodeName}
        onSwitchToWorkflow={(prompt) => {
          // node-wizard→workflow handoff: the Node Wizard decided this
          // request is really a multi-step pipeline. Close the Node Wizard,
          // seed the Workflow Wizard with the same description, and open it —
          // it auto-composes the workflow.
          setWizardOpen(false);
          setResumeTaskId(null);
          setResumeNodeName(undefined);
          setResumeWizardSeed(null);
          setWorkflowSeedPrompt(prompt);
          setIsWizardPanelOpen(true);
        }}
        onDeferGeneration={(info) => {
          // Pop the Close-&-notify task into the Wizard Queue as a
          // generation-phase row. Server-side also wrote a DB
          // Notification so the bell will mirror this state.
          addBuild({ ...info, phase: "generation" });
          setQueueOpen(true);
        }}
        accessToken={api.accessToken}
        onNodeCreated={(info) => {
          // One queue row per Create-Node click. The wizard submits a
          // single workflow_run that auto-chains generate → ingest →
          // variant builds → register on the server side, so we just
          // add ONE phase:"build" row pointing at the workflow_run_id.
          // The row polls /build-run-progress/{id} for status across
          // all nodes in the run.
          //
          // The legacy two-phase shape (info.singleStep,
          // info.generationTaskId, transitionToBuild) is gone — the
          // server-side workflow handles the hand-off the client used
          // to stitch together.
          if (info) {
            addBuild({
              taskId: info.taskId,
              nodeName: info.nodeName,
              phase: "build",
              startedAt: Date.now(),
            });
          }
          setQueueOpen(true);
          window.dispatchEvent(new Event("mod:nodes:changed"));
        }}
      />

      <BuildQueuePanel
        open={queueOpen}
        onClose={() => setQueueOpen(false)}
        builds={builds}
        accessToken={api.accessToken}
        apiUrl={apiUrl()}
        onFinished={markBuildFinished}
        onDismiss={dismissBuild}
        onClearCompleted={clearCompleted}
        onResumeGeneration={(taskId) => {
          // Same fresh-mount story as the NodeLibrary resume path —
          // clear localStorage + bump the open counter so the wizard
          // remounts and the resume effect repopulates from the
          // server, instead of overlaying onto whatever the
          // previously-open wizard had cached. Also pass through the
          // queued nodeName so the wizard hydrates the user-chosen
          // name even though defer wiped localStorage. Match by
          // either taskId OR generationTaskId so a morphed Phase 2
          // row still resolves the original nodeName (otherwise the
          // new Phase 2 row created downstream falls back to
          // "Task #<id>").
          clearPersistedWizardState();
          setResumeTaskId(taskId);
          const queued = builds.find(
            (b) => b.taskId === taskId || b.generationTaskId === taskId,
          );
          setResumeNodeName(queued?.nodeName);
          setEditNodeId(null);
          setEditNodeName(undefined);
          setDebugRunId(null);
          setWizardOpenCount((c) => c + 1);
          setWizardOpen(true);
          setQueueOpen(false);
        }}
        onRetryWithCorrections={async (failedTaskId) => {
          // Issue #2: dispatch a corrected Containerizer retry via
          // the new /retry-with-corrections endpoint. On success,
          // dismiss the failed row and add a new generation-phase
          // row for the retried task. On 409 (cap reached), surface
          // the manual-fix message via toast.
          if (!api.accessToken) return;
          const apiBase = apiUrl();
          const failedRow = builds.find((b) => b.taskId === failedTaskId);
          try {
            const resp = await authenticatedFetch(
              `${apiBase}/woodsman/retry-with-corrections`,
              {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  Authorization: `Bearer ${api.accessToken}`,
                },
                body: JSON.stringify({ task_id: failedTaskId }),
              },
            );
            if (resp.status === 409) {
              const body = await resp.json().catch(() => ({}));
              const detail = body?.detail;
              if (detail && detail.error === "retry_cap_reached") {
                toast({
                  title: "Manual fix required",
                  description: detail.message ||
                    "Already retried this task twice. Open the Preview step's Dockerfile editor and edit by hand.",
                  variant: "destructive",
                });
                return;
              }
            }
            if (!resp.ok) {
              const text = await resp.text().catch(() => "");
              throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
            }
            const data = await resp.json();
            // Replace the failed row with a fresh generation row for
            // the new task. The user watches Phase 1 again, then
            // resumes into Confirm and Build as usual.
            dismissBuild(failedTaskId);
            addBuild({
              taskId: data.task_id,
              nodeName: failedRow?.nodeName || `Task #${data.task_id}`,
              phase: "generation",
              startedAt: Date.now(),
            });
            toast({
              title: "Retrying with corrections",
              description: `New Containerizer task #${data.task_id} dispatched (${data.failures_replayed || 0} failures replayed).`,
            });
          } catch (err: any) {
            toast({
              title: "Retry failed",
              description: err?.message || String(err),
              variant: "destructive",
            });
          }
        }}
        focusRunId={wizardQueueFocusId}
        onOpenArtifact={(refType, refId) => {
          // Only ref types with a real in-app / existing destination are
          // wired (wizardArtifactTarget already hid the button for the
          // rest). task → the Dashboard System → Workers tab, where
          // ApiTaskQueue rows + their logs live.
          // SUPPORTCHAT-CE-4 — the 2400UI125 CE no-op is removed: the artifact
          // redirect behaves identically on CE and paid.
          // CE: no project artifact redirect (the queue row hides Open for project refs) ()
          if (refType === "task") {
            window.location.href = `${getDashboardUrl()}/?tab=workers#analytics`;
          }
        }}
      />
      </div>
    </div>
    {/* FX.79 p3a — bottom half of the persistent data-marking banner
        pair (top one renders above the canvas). */}
    <DataMarkingBanner
      resourceType="workflow"
      resourceId={api.currentWorkflow?.id ?? null}
      position="bottom"
      accessToken={api.accessToken}
      canClear={canClearMarking}
    />
    </>
  );
};

export default function WorkflowBuilder() {
  return (
    <ReactFlowProvider>
      {/* Soft-auth banner: renders nothing until authenticatedFetch
          dispatches AUTH_CONNECTION_LOST_EVENT (typically a 401 on a
          background poll after silent refresh failed). See
          ConnectionLostBanner for the rationale — previously we hard-
          redirected here and wizard state got wiped on every token
          blip. */}
      <ConnectionLostBanner />
      {/* GG.UI.9 — trial countdown banner; fetch-once-on-mount, no polling. */}
      {false && <TrialBanner />}
      <WorkflowBuilderContent />
    </ReactFlowProvider>
  );
}