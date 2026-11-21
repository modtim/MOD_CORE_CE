"use client";
import { apiUrl } from "@/lib/runtime-config";

import React, { useState, useEffect, useRef } from "react";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Database, FileOutput, Settings, Loader2, GitMerge, Shuffle, GitCommitHorizontal, ChevronRight, ChevronDown, Plus, Sparkles, WandSparkles, Activity, Hammer, Zap, Store, CheckCircle2, Server, Pencil, Copy, RefreshCw } from "lucide-react";
import { authenticatedFetch, parseWorkflowIdParam } from "@/lib/api-client";
import { startSignInAgain } from "@/lib/sign-in-again";
import { ToastAction } from "@/components/ui/toast";
import { PaidLock } from "./PaidLock";
import { useEntitlements } from "@/hooks/use-entitlements";
import { EnvironmentNeeds } from "./EnvironmentNeeds";
// NewNodeWizard + BuildProgressCard are rendered by workflow-builder.tsx
// (the stable parent) so they survive NodeLibrary key-bump remounts.

// Credits disabled by default. When re-enabling, fetch from /platform/features.
const CREDITS_UI_ENABLED = false;
import type { NodeDefinition } from "@/lib/types";
import { isPlatformNode } from "@/lib/platform-node";
import { NodeChips, VariantStatusBadges, CeBadge, LifecycleBadge, paletteLifecycleState, ExternalAiBadge } from "./node-chips";
import CostBasisBadge from "./CostBasisBadge";
import { EgressStateBadge } from "@/components/egress-badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ScanDetailsDialog } from "./scan-details-dialog";
import { isLocalNodeId } from "../lib/local-node";
import { NodeHoverPanel } from "@/components/ui/node-hover-panel";
import { themeLabel, wbNavLabel } from "@/lib/product-names";


/** tcp 8.1 — the node payload also carries three-tier pricing and the
 *  server's cost basis; both are optional (old nodes have neither), so
 *  they extend the local UI type rather than lib/types NodeDefinition. */
type NodeDefinitionUI = Partial<NodeDefinition> & {
  nodeType: string;
  cost_credits_by_tier?: { small?: number; medium?: number; large?: number } | null;
  cost_basis?: string | null;
};

const getNodeIcon = (node: NodeDefinitionUI) => {
  switch (node.nodeType) {
    case "input":
      return <Database className="h-4 w-4 text-blue-500" />;
    case "output":
      return <FileOutput className="h-4 w-4 text-green-500" />;
    case "process":
      return <Settings className="h-4 w-4 text-purple-500" />;
    case "if-else":
        return <GitMerge className="h-4 w-4 text-yellow-500" />;
    case "switch":
        return <Shuffle className="h-4 w-4 text-orange-500" />;
    case "split":
        return <GitCommitHorizontal className="h-4 w-4 text-cyan-500" />;
    default:
        return <Settings className="h-4 w-4 text-gray-500" />;
  }
};


const localNodes: NodeDefinitionUI[] = [
    {
      id: 999,
      name: "If/Else",
      nodeType: "if-else",
      type: "Operators",
      description: "Branch based on a true/false condition.",
    },
    {
      id: 998,
      name: "Switch",
      nodeType: "switch",
      type: "Operators",
      description: "Branch based on multiple cases.",
    },
    {
      id: 997,
      name: "Split",
      nodeType: "split",
      type: "Operators",
      description: "Split execution into parallel paths.",
    },
    {
      // Gate 5. Listed here rather than fetched, for the same reason the
      // operators above are: `get_node_definitions` hides system rows
      // (tenant_id NULL) unless they are marketplace-curated AND the tenant
      // holds an entitlement. A control-flow primitive is not a marketplace
      // download, so a seeded NodeDefinition alone left the node invisible in
      // every tenant and the feature unreachable from the canvas.
      //
      // `id` matches the seeded row so `data.nodeDefinitionId` resolves for
      // anything that looks the definition up. `node_kind` is what actually
      // drives the behaviour — the orchestrator branches on it before
      // dispatch. `nodeType: "process"` gets the ordinary in/out handles so it
      // drops between any two nodes; there is nothing to branch, so the
      // if-else/switch shapes would be wrong.
      id: 332,
      name: "Human Approval",
      nodeType: "process",
      type: "Operators",
      node_kind: "human_approval",
      description: "Pauses the run until a person approves or rejects. No worker is held while it waits.",
      process_type: "CPU",
      cost_in_credits: 0,
      input_schema: { input_type: "ANY", input_variables: "NONE" },
      output_schema: { output_type: "ANY", file_mode: "both", output_variables: "NONE" },
    }
]

interface NodeLibraryProps {
  accessToken: string | null;
  onLoadingChange?: (isLoading: boolean) => void;
  hasLoadedBefore?: boolean;
  /** Opens the New Node wizard. The wizard lives in a stable parent
   *  (workflow-builder.tsx) so its state survives key-bumped remounts
   *  of this component. */
  onOpenWizard?: () => void;
  /** Opens the Node Queue dialog (build progress for 1..N in-flight
   *  node builds). Counter beside the label shows in-progress count. */
  onOpenQueue?: () => void;
  /** Opens the Process Queue dialog (in-flight workflow runs). */
  onOpenProcessQueue?: () => void;
  /** Opens Pending Approvals (runs paused on a human_approval gate). */
  onOpenApprovals?: () => void;
  /** Opens the Triggers popup (schedule/event automation for workflows). */
  onOpenTriggers?: () => void;
  /** Opens the Marketplace popup (browse/download + curate nodes). */
  onOpenMarketplace?: () => void;
  /** Opens the Workflow Builder side-rail (chat-style whole-workflow
   *  composer; previously labeled "Workflow Wizard"). Anchored second
   *  in the Functions menu, directly beneath New node. */
  onOpenWorkflowBuilder?: () => void;
  /** project-agent — opens the Project Wizard, one rung above the
   *  Workflow Wizard. The Workflow Wizard composes ONE graph; the
   *  Project Wizard holds a whole need across many workflows and nodes
   *  and dispatches the wizards below it. Anchored directly beneath
   *  Workflow Wizard so the escalation reads node → workflow → project. */
  onOpenProjectWizard?: () => void;
  /** Opens the Service (Stack) Wizard — stands up a persistent service
   *  stack via the /persistent-stacks API. Anchored directly beneath
   *  the Project Wizard so the escalation reads node → workflow →
   *  project → service. */
  onOpenStackWizard?: () => void;
  /** node-code-edit-rebuild — opens the Node Wizard in "edit existing
   *  node code" mode for the given registered node. Rendered as a card
   *  action for any registered, non-local node; permission is enforced
   *  server-side (RBAC capability + entitlement), not by a feature flag. */
  onEditNodeCode?: (nodeId: number, nodeName: string) => void;
  /** FX.68.p3 [U] — keyboard alternative to the palette drag. Enter/Space on a
   *  focused palette row calls this with the node def; the parent drops it on
   *  the canvas centre through the same createNode the drag's onDrop uses. */
  onKeyboardAddNode?: (node: NodeDefinitionUI) => void;
  queueCount?: number;
}

export default function NodeLibrary({
  accessToken,
  onLoadingChange,
  hasLoadedBefore = false,
  onOpenWizard,
  onOpenQueue,
  onOpenProcessQueue,
  onOpenApprovals,
  onOpenTriggers,
  onOpenMarketplace,
  onOpenWorkflowBuilder,
  onOpenProjectWizard,
  onOpenStackWizard,
  onEditNodeCode,
  onKeyboardAddNode,
  queueCount = 0,
}: NodeLibraryProps) {
  // SUPPORTCHAT-CE-4 — the 2400UI125 CE carve is removed: the Project Wizard
  // entry point mounts on CE and paid alike (the whole support package ships
  // in Community Edition), so there is no edition read here anymore.
  // STACK-WIZARD-UNLOCK (owner Q4 B, 11-10) is the one exception: the Stack
  // Wizard row reads the edition so it can bypass <PaidLock cap="stack_wizard">
  // on a POSITIVE "ce" edition (single-org persistent stacks are free; only
  // cross-site mesh federation is paid, and that lives on the manifest-denied
  // managers router, not here). Fail-open as everywhere: an unresolved edition
  // keeps the paid lock+badge.
  const { edition: stackWizardEdition, resolved, edition } = useEntitlements();

  // Functions accordion (consolidates the common actions — New node,
  // Workflow Wizard, Process queue, Node queue — into a single
  // collapsible group on the node library bar). Issue #2 / #9.
  // Default-open so the actions are immediately discoverable; user
  // clicks the trigger row to collapse and reclaim sidebar space.
  const [showFunctionsMenu, setShowFunctionsMenu] = useState(true);
  const [groupedNodes, setGroupedNodes] = useState<{ [key: string]: NodeDefinitionUI[] }>({});
  const [isLoading, setIsLoading] = useState(true);
  // Seed the search term from ?node_search=<name> on first mount so the
  // Dashboard's "Take me there" links land the user on the filtered node.
  const [searchTerm, setSearchTerm] = useState(() => {
    if (typeof window === "undefined") return "";
    try {
      return new URLSearchParams(window.location.search).get("node_search") || "";
    } catch {
      return "";
    }
  });
  const [nodeFilter, setNodeFilter] = useState<'all' | 'mine' | 'platform'>('all');
  // FX.103 p4 — retired nodes are HIDDEN from the palette by default (they
  // cannot run: the API's run guard refuses them). This toggle reveals them
  // greyed out, badged "Retired" and NOT draggable, so a user can see what
  // the retirement sweep took away without being able to add it back.
  const [showRetired, setShowRetired] = useState(false);
  // Fix 32 — when the API is degraded (a storm of 502/401/5xx on
  // /nodes/), surface a calm "backend unavailable, retrying" state
  // instead of letting the failure cascade. Set on a bad response / network
  // error, cleared the moment a fetch succeeds.
  const [apiDegraded, setApiDegraded] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [previousCount, setPreviousCount] = useState(0);
  const [stableCount, setStableCount] = useState(0);
  // Manual "Refresh nodes" control. `manualRefreshing` drives the
  // spinner on the button while an explicit refetch is in flight;
  // `justUpdated` briefly flips it to a green check so the user gets a
  // visible "updated" affordance. Both are cosmetic — the actual
  // refetch is the same retryCount-bump the mod:nodes:changed listener
  // and rescan handler already use, so no new fetch path is introduced.
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const [justUpdated, setJustUpdated] = useState(false);
  // Wizard + build-progress card are rendered by the stable parent
  // (workflow-builder.tsx). NodeLibrary only opens the wizard via the
  // onOpenWizard callback — storing wizardOpen here used to mean that
  // every refresh of the library (setNodeLibraryKey bump) unmounted
  // the wizard mid-use.

  // Notify parent when loading state changes
  useEffect(() => {
    onLoadingChange?.(isLoading);
  }, [isLoading, onLoadingChange]);

  // Reset state when component remounts (e.g., when refresh button is clicked)
  useEffect(() => {
    setRetryCount(0);
    setPreviousCount(0);
    setStableCount(0);
    setIsLoading(true);
  }, []);

  useEffect(() => {
    const processNodes = (nodes: NodeDefinition[]) => {
        // A backend row that is also a static local entry (Human Approval,
        // now entitled via the MOD Core starter package) would render twice.
        //
        // The id match alone is not enough. The seeded row's id is a DB
        // BIGSERIAL, while the palette entry pins id 332 only so
        // `data.nodeDefinitionId` resolves for canvas lookups — the two ids
        // never agree, so the row slipped through the filter and the palette
        // showed the gate twice (CE sweep 17, the wb-ce-batch24 row). Match on
        // the stable key (`node_kind`) as well; entries without one (If/Else,
        // Switch, Split) keep the id-only rule.
        //
        // But only for the SYSTEM row. Tenant copies ARE the palette: a tenant
        // (or user-private) copy of the gate that keeps `node_kind:
        // "human_approval"` under a different `name` is the tenant's own node
        // and must still render — `get_node_definitions` dedups by `name`, so
        // it survives the listing. The seeded row is system-scoped
        // (`seed_human_approval_node.py` `tenant_id=None`), same convention as
        // `lib/platform-node.ts`. Kind-blind hiding would drop it (CE sweep 17
        // review ISSUE-1).
        // B24-PRODFIX (CE launch) — the system-scope arm above is inert in CE.
        // node_scanner.py:1098 (WS2.7 CE exception) registers keep-listed
        // system nodes as TENANT-scoped rows (tenant_id = default_tenant_id()
        // = 1), so the seeded twin's tenant_id is NOT null and its id is a
        // BIGSERIAL — neither arm hides it, and the palette renders the gate
        // twice next to the pinned local entry (node-library.tsx:99-107).
        //
        // The row carries the platform's own seed marker: `ce_included` —
        // stamped TRUE on CREATE by node_scanner.py:1165 for every keep-listed
        // name, and returned on the palette payload by
        // routers/node_definitions_domains/node_authoring.py:2587
        // (services/ce_inclusion.py:75 falls back to the keep-list by name on
        // an un-migrated DB). Client type: lib/types.ts:165.
        //
        // So hide a row only when it is BOTH platform-seeded (ce_included) AND
        // the same `node_kind` as a local entry. Name alone is never a key: a
        // renamed copy ("Acme Review Gate") is not keep-listed, so it carries
        // no marker and still renders; a user node that merely shares a name
        // but is a different kind still renders.
        const localIds = new Set(localNodes.map(n => n.id));
        const localKinds = new Set(localNodes.map(n => n.node_kind).filter(Boolean));
        const isLocalTwin = (n: NodeDefinition) =>
            localIds.has(n.id) ||
            (!!n.node_kind && localKinds.has(n.node_kind) && n.tenant_id == null) ||
            (!!n.node_kind && localKinds.has(n.node_kind) && n.ce_included === true);
        // wb-ce-batch14 (CE launch) — one palette row per catalogue node NAME.
        // The CE catalogue ships the same converter twice: seed_converters.py:118
        // writes the row with one `node_kind` ("hdr_converter", :123) and the
        // metadata scan registers the manifest row under another
        // (CORE/Metadata/system/HdrConverter_IMAGE_PROCESS_Node.py:5
        // "hdr_convert_process"), both tenant-shared, both live. crud's name
        // dedup only hides LESS-specific twins, and the router's WB14-PRODFIX
        // read-time collapse keys name AND node_kind, so neither collapses the
        // pair and the palette renders two rows for one name — the strict-mode
        // violation the sweep hit (2 buttons matching `^HDR Converter`,
        // `data-node-definition-id="1154"` + the variant row).
        // Collapse the pair CLIENT-side. The key is (scope, casefolded name)
        // GATED on the platform seed marker (`ce_included === true`, user_id
        // null): the keep-list is keyed on NAME (open-core/node-seed-keeplist.yaml,
        // services/ce_inclusion.py:102, node_scanner.py:416 wide self-heal), so
        // two marker-carrying rows for one name ARE one catalogue entry even
        // when the two seeders disagree on `node_kind` — which is exactly why
        // node_kind is NOT in the key here. A row without the marker (a user's
        // own node that happens to share a catalogue name, or a tenant copy the
        // keep-list never stamped) is never collapsed, so no user data hides.
        // Lowest id wins, same convention as crud/node_definition_cud.py:347.
        const rowKey = (n: NodeDefinition) => {
          const name = (n.name || "").trim().toLowerCase();
          if (n.ce_included !== true || n.user_id != null || !name) return null;
          return `${n.tenant_id ?? "sys"}:${name}`;
        };
        const winnerId: { [key: string]: number } = {};
        for (const n of nodes) {
          const key = rowKey(n);
          if (key === null) continue;
          const rid = n.id ?? 0;
          if (winnerId[key] === undefined || rid < winnerId[key]) winnerId[key] = rid;
        }
        const dedupedServerNodes = nodes.filter(n => {
          if (isLocalTwin(n)) return false;
          const key = rowKey(n);
          return key === null || winnerId[key] === (n.id ?? 0);
        });
        const allNodes: NodeDefinitionUI[] = [...localNodes, ...dedupedServerNodes.map(n => ({...n, nodeType: n.input_schema?.input_type === 'NONE' ? 'input' : (n.output_schema?.output_type === 'NONE' ? 'output' : (n.process_type === 'INPUT' ? 'input' : 'process')) }))];

        const groups = allNodes.reduce((acc, node) => {
            const groupName = node.type || "General";
            if (!acc[groupName]) {
                acc[groupName] = [];
            }
            acc[groupName].push(node);
            return acc;
        }, {} as { [key: string]: NodeDefinitionUI[] });
        setGroupedNodes(groups);
    }

    if (!accessToken) {
      processNodes([]);
      setIsLoading(false);
      return;
    }

    const fetchNodeDefinitions = async () => {
      // AbortController + 15s timeout on the GET. Tim playtest
      // 2026-06-04: the WB hung indefinitely on "Loading node
      // library…" after a 202 from /nodes/refresh —
      // the subsequent GET had no upper bound and never resolved,
      // so the catch block never fired and the spinner was stuck.
      // 15s is well above a healthy round-trip (~200-500ms for
      // up to 1000 nodes) and below a "user gives up and refreshes"
      // threshold. On AbortError the existing catch path retries
      // up to 3 times, then falls back to local nodes + clears the
      // spinner via processNodes([]) + setIsLoading(false).
      const ctrl = new AbortController();
      const timeoutId = window.setTimeout(() => ctrl.abort(), 15_000);
      try {
        const API_URL = apiUrl();
        const response = await fetch(`${API_URL}/nodes/?limit=1000&offset=0`, {
          headers: {
            Authorization: `Bearer ${accessToken}`,
            Accept: "application/json",
          },
          signal: ctrl.signal,
        });
        window.clearTimeout(timeoutId);

        if (!response.ok) {
          // node-builder audit F5 (AUDIT-CE-JOURNEY-NODE-BUILDER-FIRST-USE-FRESH-ADMIN):
          // a 401/403 on /nodes/ is NOT a degraded backend — it is an EXPIRED
          // LOGIN (a saved login went stale; the fresh-admin first-use journey
          // hit exactly this). Retrying 3 times and toasting "Backend
          // unavailable" named the wrong cause and offered no fix. Branch on
          // the status: 401/403 gets its own toast carrying the existing
          // "Sign in again" action (lib/sign-in-again.ts — the same
          // startSilentSso round trip the WB's denied screen uses, Q41), and
          // the retry budget is NOT consumed (a stale token would only fail
          // again). Every other status keeps the existing degraded/retry
          // behaviour exactly.
          const status = response.status;
          if (status === 401 || status === 403) {
            // Not a degraded backend: clear any prior degraded flag so the
            // sidebar never claims "backend unavailable, retrying" for an
            // auth failure.
            setApiDegraded(false);
            processNodes([]); // local nodes only
            setIsLoading(false);
            toast({
              title: "Your session expired - sign in again",
              description: "The node library needs a live login. Sign in again, then reload the page — the API loads the nodes at start-up.",
              action: <ToastAction altText="Sign in again">Sign in again</ToastAction>,
            });
            // The existing action: same-origin cold-link login round trip.
            // Only started when the tab actually carries a validated
            // ?workflow=<id> (the Q41/Q9 case); on the bare WB page the
            // toast alone is the affordance — never a blind navigation.
            const wfId = parseWorkflowIdParam(window.location.search);
            if (wfId !== null) await startSignInAgain(wfId);
            return;
          }
          // 5xx/502/503 (backend degraded) stay transient: flag the degraded
          // state so the UI shows "backend unavailable, retrying" and fall
          // through to the existing retry/backoff path — never an uncaught
          // error cascade.
          setApiDegraded(true);
          throw new Error(`Failed to fetch node definitions (HTTP ${response.status})`);
        }
        // Success — clear any prior degraded state.
        setApiDegraded(false);
        const data: NodeDefinition[] = await response.json();

        // If zero nodes returned (startup scan may not have run yet), trigger refresh and retry once
        if (data.length === 0 && retryCount === 0) {
          console.log('No nodes found — triggering refresh and retrying...');
          try {
            // Inner refresh POST also bounded — pre-fix this had no
            // timeout either, so a hung POST would silently swallow
            // the retry budget.
            const innerCtrl = new AbortController();
            const innerTimeoutId = window.setTimeout(() => innerCtrl.abort(), 10_000);
            await fetch(`${API_URL}/nodes/refresh`, {
              method: 'POST',
              headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json', 'Content-Type': 'application/json' },
              signal: innerCtrl.signal,
            });
            window.clearTimeout(innerTimeoutId);
          } catch {}
          setTimeout(() => setRetryCount(prev => prev + 1), 1000);
          return;
        }

        processNodes(data);
        setIsLoading(false);

      } catch (error) {
        window.clearTimeout(timeoutId);
        const timedOut = (error as any)?.name === 'AbortError';
        if (timedOut) {
          console.warn(
            'NodeLibrary: /nodes/ did not respond within 15s ' +
            '(attempt ' + (retryCount + 1) + '/4). Falling through to retry/backoff.',
          );
        } else {
          console.error("Error fetching node definitions:", error);
        }
        // Network error / timeout is also a degraded-backend signal.
        setApiDegraded(true);
        if (retryCount < 3) {
          // Retry on network error / timeout (3 attempts max).
          setTimeout(() => setRetryCount(prev => prev + 1), 1000);
        } else {
          processNodes([]); // Fallback to local nodes on error
          toast({
            title: "Backend unavailable",
            description: "Could not load the node library. Showing local nodes only — it will refresh automatically once the backend recovers.",
            variant: "destructive",
          });
          setIsLoading(false);
        }
      }
    };

    fetchNodeDefinitions();
  }, [accessToken, retryCount, previousCount, stableCount, hasLoadedBefore]);

  // Listen for external "nodes changed" events. Any caller that wants
  // the library to refresh its data should dispatch this event — NOT
  // bump a parent-level key. Bumping a key remounts the whole sidebar
  // (and everything rendered inside it used to), which was the cause
  // of the wizard / progress card blinking out of existence on every
  // node-change. We now re-fetch data in-place: the component stays
  // mounted, the groupedNodes state swaps under the hood, anything
  // the user had open stays open.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onNodesChanged = () => {
      // Nudge the existing fetch effect — this runs fetchNodeDefinitions
      // again without any component remount.
      setRetryCount((c) => c + 1);
    };
    window.addEventListener("mod:nodes:changed", onNodesChanged);
    return () => window.removeEventListener("mod:nodes:changed", onNodesChanged);
  }, []);

  // Explicit "Refresh nodes" button handler. Re-fetches the library
  // in place (no page reload) by bumping retryCount — the exact same
  // mechanism the mod:nodes:changed listener uses. Flips isLoading so
  // the existing inline "Looking for newly-built nodes…" indicator
  // shows; that transition back to false is what the effect below
  // watches to render the brief "updated" check.
  const handleManualRefresh = () => {
    if (!accessToken) return;
    if (manualRefreshing) return;
    setJustUpdated(false);
    setManualRefreshing(true);
    setIsLoading(true);
    setRetryCount((c) => c + 1);
  };

  // Watch for a manual refresh completing (isLoading true -> false)
  // and show the "updated" check for ~1.5s. Kept separate from the
  // fetch effect so we don't touch its retry/degraded logic.
  const prevLoadingRef = useRef(isLoading);
  useEffect(() => {
    if (prevLoadingRef.current && !isLoading && manualRefreshing) {
      setManualRefreshing(false);
      setJustUpdated(true);
      const t = window.setTimeout(() => setJustUpdated(false), 1500);
      prevLoadingRef.current = isLoading;
      return () => window.clearTimeout(t);
    }
    prevLoadingRef.current = isLoading;
  }, [isLoading, manualRefreshing]);

  // Lightweight focus auto-refresh — the "I changed something server-side,
  // then came back to the tab" case. Re-fetches in place when the tab
  // regains focus, throttled to at most once per 5s so rapid focus
  // flips (alt-tab spam) don't hammer the API. Silent: no spinner, no
  // isLoading flip — groupedNodes just swaps under the hood. Skipped
  // while the backend is degraded so we don't pile onto a retry storm.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!accessToken) return;
    let lastFocusRefresh = 0;
    const onFocus = () => {
      if (document.visibilityState === "hidden") return;
      if (apiDegraded) return;
      const now = Date.now();
      if (now - lastFocusRefresh < 5000) return;
      lastFocusRefresh = now;
      setRetryCount((c) => c + 1);
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [accessToken, apiDegraded]);

  const onDragStart = (event: React.DragEvent<HTMLButtonElement>, nodeDef: NodeDefinitionUI) => {
    const dataToTransfer = JSON.stringify({ ...nodeDef, type: nodeDef.nodeType });
    event.dataTransfer.setData("application/reactflow", dataToTransfer);
    event.dataTransfer.effectAllowed = "move";
  };

  // Track which node's rescan button is mid-click so we can disable
  // it + show a spinner without globally disabling the library.
  const [rescanningIds, setRescanningIds] = useState<Set<number>>(new Set());

  // node-code-edit-rebuild — this is an everyone-facing affordance, not an
  // admin feature flag: the "Edit code"/"Copy as template" actions render
  // for any registered, non-local node and the backend (RBAC "view"/"edit"
  // capability on the node + the custom_nodes entitlement for rebuild) is
  // the real gate. A user without permission still sees the button but gets
  // a clear inline error when the wizard's GET .../source 403s, the same
  // reactive-error pattern used for the custom_nodes 402 on rebuild. There
  // used to be a client-side probe of the wizard_code_edit_enabled admin
  // flag here that hid these buttons entirely — removed. That flag's field
  // is still defined server-side (platform_feature_config.py) but nothing
  // reads it anywhere now; it's a candidate for full retirement.

  // "Copy as template" — POSTs /nodes/{id}/copy to create a
  // tenant-owned editable draft from a (possibly system/platform)
  // node, then refetches the library via the same mod:nodes:changed
  // event the rest of the app uses for node-list refresh.
  const [copyingIds, setCopyingIds] = useState<Set<number>>(new Set());
  const handleCopyAsTemplate = async (nodeId: number) => {
    if (!accessToken) return;
    if (copyingIds.has(nodeId)) return;
    setCopyingIds(prev => new Set(prev).add(nodeId));
    try {
      const API_URL = apiUrl();
      const resp = await authenticatedFetch(`${API_URL}/nodes/${nodeId}/copy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        const raw = await resp.text().catch(() => '');
        throw new Error(raw || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      toast({
        title: 'Copied',
        description: `Created "${data.name}" as an editable copy.`,
      });
      window.dispatchEvent(new Event('mod:nodes:changed'));
    } catch (err: any) {
      toast({
        title: 'Copy failed',
        description: err?.message || 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setCopyingIds(prev => {
        const next = new Set(prev);
        next.delete(nodeId);
        return next;
      });
    }
  };

  // Scan-details modal — big popup with full vuln breakdown, guidance
  // per status, rescan button. Opens from "View Details" in the hover
  // card. Kept at the library level (not per-node) so clicking a new
  // card replaces the current details without remount churn.
  const [detailsNode, setDetailsNode] = useState<NodeDefinitionUI | null>(null);

  // Auto-open the Scan Details dialog when we arrived via the Dashboard's
  // "Take me there" link (?node_search=<name>). Fires once the library
  // has loaded and the named node is present. Ref prevents re-open on
  // subsequent renders or if the user closes the dialog manually.
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (autoOpenedRef.current) return;
    if (typeof window === "undefined") return;
    if (!groupedNodes || Object.keys(groupedNodes).length === 0) return;

    let targetName: string | null = null;
    try {
      targetName = new URLSearchParams(window.location.search).get("node_search");
    } catch {
      return;
    }
    if (!targetName) return;

    // Find the exact-name match across all groups. Case-insensitive so
    // the Dashboard doesn't need to worry about capitalisation.
    const needle = targetName.toLowerCase();
    let match: NodeDefinitionUI | null = null;
    for (const nodes of Object.values(groupedNodes)) {
      const hit = nodes.find((n) => (n.name || "").toLowerCase() === needle);
      if (hit) { match = hit; break; }
    }
    if (!match) return;

    autoOpenedRef.current = true;
    setDetailsNode(match);

    // Strip the query param so a refresh doesn't re-open the dialog.
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("node_search");
      window.history.replaceState({}, "", url.toString());
    } catch {}
  }, [groupedNodes]);

  const handleScanNow = async (nodeId: number) => {
    if (!accessToken) return;
    if (rescanningIds.has(nodeId)) return;
    setRescanningIds(prev => new Set(prev).add(nodeId));
    try {
      const API_URL = apiUrl();
      const resp = await fetch(`${API_URL}/nodes/${nodeId}/rescan`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      });
      if (!resp.ok) {
        const body = await resp.text().catch(() => '');
        throw new Error(body || `HTTP ${resp.status}`);
      }
      toast({
        title: 'Scan queued',
        description: 'The worker will pick it up on its next poll; scan status should update within a minute or two.',
      });
      // Trigger a library refetch so the pending_scan badge flips
      // promptly. Safe to do even if the status hasn't moved yet —
      // the fetcher will just re-render with the current state.
      setRetryCount(prev => prev + 1);
    } catch (err: any) {
      toast({
        title: 'Could not queue scan',
        description: err?.message || 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setRescanningIds(prev => {
        const next = new Set(prev);
        next.delete(nodeId);
        return next;
      });
    }
  };

  const filteredGroupedNodes = Object.entries(groupedNodes).reduce((acc, [groupName, nodes]) => {
    const filteredNodes = nodes.filter(node => {
      const matchesSearch = !searchTerm ||
        node.name?.toLowerCase().includes(searchTerm.toLowerCase()) ||
        node.description?.toLowerCase().includes(searchTerm.toLowerCase());
      const platform = isPlatformNode(node);
      const matchesFilter = nodeFilter === 'all' ||
        (nodeFilter === 'platform' && platform) ||
        (nodeFilter === 'mine' && !platform);
      // FX.103 p4 — retired nodes are hidden unless the "Show retired"
      // toggle is on. Deprecated nodes stay (they are still runnable for
      // pinned workflows; the badge + tooltip tell the user the date).
      const isRetiredNode = paletteLifecycleState(node) === 'retired';
      return matchesSearch && matchesFilter && (showRetired || !isRetiredNode);
    });

    if (filteredNodes.length > 0) {
      acc[groupName] = filteredNodes;
    }

    return acc;
  }, {} as { [key: string]: NodeDefinitionUI[] });


  // Wizard + BuildProgressCard are rendered by the stable parent
  // (workflow-builder.tsx) so they aren't wiped whenever the library
  // bumps its key. The empty overlay fragment stays to preserve the
  // existing `{persistentOverlays}` insertion points below.
  const persistentOverlays = null;

  // Only show the full-pane "Loading nodes..." state when we have
  // literally nothing to display. Once the library has loaded once,
  // keep showing the existing nodes during refreshes — a user whose
  // build just kicked off doesn't want the sidebar to go blank every
  // time we poll for the new node to appear. Subtle inline indicator
  // instead.
  const hasAnyNodes = Object.keys(groupedNodes).length > 0;
  if (isLoading && !hasAnyNodes) {
    return (
      <>
        {persistentOverlays}
        <div className="flex flex-col justify-center items-center h-full px-4 text-center">
          <Loader2 className="h-8 w-8 animate-spin text-indigo-600 mb-3" />
          {apiDegraded ? (
            <>
              <p className="text-sm text-amber-700 font-medium">Backend unavailable — retrying…</p>
              <p className="text-xs text-gray-500 mt-1">The node library will load automatically once the backend recovers.</p>
            </>
          ) : (
            <p className="text-sm text-gray-600">Loading nodes...</p>
          )}
        </div>
      </>
    );
  }

  return (
    <div className="h-full flex flex-col">
       {persistentOverlays}
       {apiDegraded && (
         <div className="mb-2 flex items-center gap-2 text-[10px] text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1">
           <Loader2 className="h-3 w-3 animate-spin" />
           <span>Backend unavailable — retrying. Showing available nodes; the list refreshes automatically once it recovers.</span>
         </div>
       )}
       {isLoading && hasAnyNodes && (
         <div className="mb-2 flex items-center gap-2 text-[10px] text-indigo-700 bg-indigo-50 border border-indigo-100 rounded px-2 py-1">
           <Loader2 className="h-3 w-3 animate-spin" />
           <span>Looking for newly-built nodes…</span>
         </div>
       )}
       <div className="mb-3 flex-shrink-0 space-y-2">
        <div className="flex items-center gap-2">
          <Input
            placeholder="Search nodes..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="flex-1"
          />
          {/* Refresh nodes — re-fetches /nodes/ and updates
              the palette in place (no page reload). Spinner while a
              manual refresh is in flight; a brief green check on
              completion. Same in-place refetch used by marketplace-add
              and rescan, so newly registered / edited nodes are pulled
              in without a hard browser refresh. */}
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9 flex-shrink-0"
            onClick={handleManualRefresh}
            disabled={manualRefreshing || !accessToken}
            title="Refresh node library"
            aria-label="Refresh node library"
          >
            {manualRefreshing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : justUpdated ? (
              <CheckCircle2 className="h-4 w-4 text-green-600" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
          </Button>
        </div>
        <div className="flex gap-1">
          {(['all', 'mine', 'platform'] as const).map(f => (
            <button key={f} onClick={() => setNodeFilter(f)}
              className={`px-2 py-0.5 text-[10px] rounded-full border transition-colors ${
                nodeFilter === f ? 'bg-indigo-600 text-white border-indigo-600' : 'text-gray-500 border-gray-300 hover:border-gray-400'
              }`}>
              {f === 'all' ? 'All' : f === 'mine' ? 'My Nodes' : 'Platform'}
            </button>
          ))}
        </div>
        {/* FX.103 p4 — "Show retired" toggle. Retired nodes cannot run
            (the API run guard refuses them), so they are hidden by default;
            this reveals them greyed out + badged + not draggable. */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowRetired(v => !v)}
            aria-pressed={showRetired}
            className={`px-2 py-0.5 text-[10px] rounded-full border transition-colors ${
              showRetired ? 'bg-gray-700 text-white border-gray-700' : 'text-gray-500 border-gray-300 hover:border-gray-400'
            }`}
          >
            {showRetired ? 'Showing retired' : 'Show retired'}
          </button>
        </div>
       </div>
      {/* Hairline divider between the controls header (search /
          filters) and the scroll region below — the Functions section
          + the node accordions now share ONE scroll viewport, so the
          two regions read as distinct sections rather than one
          continuous column. */}
      <div className="h-px bg-gray-200 mb-3 flex-shrink-0" />
      {/* pb-6: bottom breathing room so the last node row isn't flush
           against the sidebar's bottom edge. The wrapping sidebar (in
           workflow-builder.tsx) has overflow-hidden, so a flush row
           reads as "cut off". */}
      <ScrollArea className="flex-grow pr-2 pb-6">
        {/* Functions dropdown — consolidates the common actions (Node
            Wizard, Workflow Wizard, Stack Wizard, Triggers, queues,
            Marketplace) into one collapsible section. It now lives as
            the FIRST child of the ScrollArea — inside the same scroll
            viewport as the node accordions — so opening it can no
            longer push the node list out of the overflow-hidden
            sidebar (owner 11-07: nodes beneath were unreachable while
            the menu was open). Issue #2 / #9. */}
        <Collapsible key="functions-menu" defaultOpen={true}>
          <CollapsibleTrigger
            className="w-full text-xs h-8 flex items-center justify-between"
            onClick={() => setShowFunctionsMenu(v => !v)}
            title="Workflow functions: new node, queues, schedule"
            aria-expanded={showFunctionsMenu}
          >
            <span className="flex items-center">
              <Plus className="w-3.5 h-3.5 mr-1.5" />
              Functions
            </span>
            <span className="flex items-center gap-1">
              {queueCount > 0 && (
                <span className="inline-flex items-center justify-center text-[10px] font-semibold rounded-full bg-blue-600 text-white px-1.5 min-w-[18px] h-[18px]">
                  {queueCount}
                </span>
              )}
              {showFunctionsMenu
                ? <ChevronDown className="w-3.5 h-3.5" />
                : <ChevronRight className="w-3.5 h-3.5" />}
            </span>
          </CollapsibleTrigger>
          <CollapsibleContent className="w-full bg-white border border-gray-200 border-t-0 rounded-b-md mt-px py-1 max-h-[280px] overflow-y-auto">
            {/* GT.D11.s2 — GY.C0 declared install environment + what it
                needs (entitlements.compliance off /platform/features).
                CE shows the declaration + the needs list + the upgrade
                link here instead of letting users click into a 402.
                Renders nothing when the payload carries no compliance
                sub-block; never invents a needs list on the CE carve.
                owner 11-17 (Gate 7 sitting 3): this must be the FIRST entry
                in the functions list — moved above the Node Wizard; text
                unchanged. */}
            <EnvironmentNeeds />
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenWizard?.(); }}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 flex items-center"
            >
              <Sparkles className="w-3.5 h-3.5 mr-2" />
              {wbNavLabel('wb-nav-wizard-nodes', 'Node Wizard')}
            </button>
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenWorkflowBuilder?.(); }}
              disabled={!onOpenWorkflowBuilder}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center"
              title="Compose a whole workflow from a plain-English description"
            >
              <WandSparkles className="w-3.5 h-3.5 mr-2" />
              {wbNavLabel('wb-nav-wizard-flow', 'Workflow Wizard')}
            </button>
            {/* Stack Wizard — placed directly beneath the Workflow
                Wizard for quick access while testing. Stands up a
                persistent service stack (e.g. a Gitea + Postgres pair)
                rather than a workflow graph. Only rendered when the
                caller supplies onOpenStackWizard — the parent gates
                that on the platform-only `stack:author` capability so
                tenant users without it never see an entry that would
                only 403. STACK-WIZARD-UNLOCK (owner Q4 B, 11-10): on a
                positive "ce" edition the row renders WITHOUT the
                <PaidLock cap="stack_wizard"> wrapper — single-org
                persistent stacks are free in Community Edition, and the
                paid half of the capability (cross-site mesh FEDERATION:
                enroll_with / enrollment-tokens / {id}/approve on the
                manifest-denied managers router) never ships here at all.
                Off CE the lock+badge is exactly as before. */}
            {/* CE: the Stack Wizard button is pulled from the builder in CE () */}
            {/* CE: the Project Wizard button is pulled from the builder in CE () */}
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenProcessQueue?.(); }}
              disabled={!onOpenProcessQueue}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center"
              title="In-flight workflow runs"
            >
              <Activity className="w-3.5 h-3.5 mr-2" />
              {wbNavLabel('wb-nav-process-queue', 'Process Queue')}
            </button>
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenApprovals?.(); }}
              disabled={!onOpenApprovals}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center"
              title="Runs paused waiting on a human decision"
            >
              <CheckCircle2 className="w-3.5 h-3.5 mr-2" />
              {wbNavLabel('wb-nav-approvals', 'Pending Approvals')}
            </button>
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenQueue?.(); }}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 flex items-center"
              title="In-flight node builds"
            >
              <Hammer className="w-3.5 h-3.5 mr-2" />
              <span className="flex-1">{wbNavLabel('wb-nav-wizard-queue', 'Wizard Queue')}</span>
              {queueCount > 0 && (
                <span className="ml-1 inline-flex items-center justify-center text-[10px] font-semibold rounded-full bg-blue-600 text-white px-1.5 min-w-[18px] h-[18px]">
                  {queueCount}
                </span>
              )}
            </button>
            {/* CE: the Marketplace button is denied with the dialog () */}
            {/* owner 11-17: Triggers is the LAST entry in the functions list. */}
            <button
              type="button"
              onClick={() => { setShowFunctionsMenu(false); onOpenTriggers?.(); }}
              disabled={!onOpenTriggers}
              className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed flex items-center"
              title="Schedule & event triggers for workflows"
            >
              <Zap className="w-3.5 h-3.5 mr-2" />
              {wbNavLabel('wb-nav-triggers', 'Triggers')}
            </button>
          </CollapsibleContent>
        </Collapsible>
        {Object.keys(filteredGroupedNodes).length > 0 ? (
          <div className="flex flex-col gap-2">
              {Object.entries(filteredGroupedNodes).sort(([a], [b]) => a.localeCompare(b)).map(([groupName, nodes]) => (
                // defaultOpen only applies on mount, so the key remounts every
                // group when a search starts or clears: matches show expanded.
                <Collapsible key={`${groupName}:${searchTerm ? 'search' : 'browse'}`} defaultOpen={!!searchTerm}>
                    <CollapsibleTrigger className="flex items-center justify-between w-full text-sm font-semibold text-gray-600 hover:text-gray-900 [&[data-state=open]>svg]:rotate-90">
                        <span>{groupName}</span>
                        <ChevronRight className="h-4 w-4 transition-transform duration-200" />
                    </CollapsibleTrigger>
                    <CollapsibleContent className="py-2 pl-2 flex flex-col gap-2 border-l">
                       {nodes.sort((a, b) => (a.name || '').localeCompare(b.name || '')).map((node) => {
                          const icon = getNodeIcon(node);
                          // Local + operator nodes (the sentinel ids in
                          // localNodes above, matched by membership) don't have scan_status and
                          // are always usable. Everything else must be
                          // `approved` before it can be dragged into a
                          // workflow — pending_scan / scanning / rejected /
                          // failed nodes are rendered disabled so the user
                          // sees why they can't add them.
                          const isLocalNode = isLocalNodeId(node.id, localNodes);
                          // Container-security policy: warn, don't block.
                          // Earlier the palette disabled (un-draggable) any
                          // node whose scan_status wasn't in a hard-coded
                          // allowlist — that was for the full-SaaS model
                          // we've since walked back from. Tenant now owns
                          // their own risk. The badge below still tells
                          // the user WHAT was found so the decision is
                          // informed; we just don't gatekeep.
                          const _scan = node.scan_status;
                          const isScanDisabled = false;
                          const isPending = _scan === 'pending_scan' || _scan === 'scanning';
                          const isHighVulnsWarn = _scan === 'high_vulns';
                          const isCriticalWarn = _scan === 'critical_vulns';
                          const isScanFailedWarn = _scan === 'scan_failed' || _scan === 'failed';
                          const isRejectedWarn = _scan === 'rejected';
                          // FX.103 p4 — deprecation/retirement lifecycle.
                          // Reads the palette payload's lifecycle_state (part-2
                          // alias of NodeDefinition.status) + retire_at
                          // (= retire_after). Deprecated = amber badge, still
                          // draggable; retired = hidden by the filter above
                          // unless "Show retired" is on, and then greyed +
                          // NOT draggable (the API run guard refuses them).
                          const lifecycleState = paletteLifecycleState(node);
                          const isRetiredNode = lifecycleState === 'retired';
                          const isLifecycleDisabled = isRetiredNode;
                          return (
                            <NodeHoverPanel
                              key={node.id}
                              openDelay={250}
                              closeDelay={80}
                              sideOffset={32}
                              trigger={
                                <Button
                                  variant="outline"
                                  className={`justify-start text-left h-auto w-full ${isScanDisabled || isLifecycleDisabled ? 'opacity-50 cursor-not-allowed' : ''}`}
                                  draggable={!isScanDisabled && !isLifecycleDisabled}
                                  data-node-definition-id={node.id}
                                  onDragStart={(e) => {
                                    if (isScanDisabled || isLifecycleDisabled) {
                                      e.preventDefault();
                                      return;
                                    }
                                    onDragStart(e, node);
                                  }}
                                  // FX.68.p3 [U] — keyboard add. The row is a
                                  // native <button> so Tab reaches it and its
                                  // own focus-visible ring (buttonVariants) is
                                  // the focus cue. Enter/Space while focused
                                  // drops the node on the canvas centre through
                                  // the parent's onKeyboardAddNode — the same
                                  // createNode the drag above feeds to onDrop.
                                  // Disabled rows (retired) refuse, matching the
                                  // drag guard.
                                  onKeyDown={(e) => {
                                    if (e.key !== "Enter" && e.key !== " ") return;
                                    e.preventDefault();
                                    if (isScanDisabled || isLifecycleDisabled) return;
                                    onKeyboardAddNode?.(node);
                                  }}
                                >
                                  <div className="mr-2 flex-shrink-0">{icon}</div>
                                  <div className="flex flex-col items-start overflow-hidden">
                                    <span className="truncate font-semibold flex items-center gap-1.5">
                                      {node.name}
                                      {/* Warn-only badges. Every node is draggable
                                          regardless of scan_status; the dot tells
                                          the user WHY they might want to look at
                                          the scan details before adding it. */}
                                      {isRejectedWarn && <span className="w-2 h-2 rounded-full bg-red-500 shrink-0" title="Admin marked this node rejected — usable, but flagged" />}
                                      {isCriticalWarn && <span className="w-2 h-2 rounded-full bg-red-500 shrink-0" title="Critical vulnerabilities found — usable, view details" />}
                                      {isScanFailedWarn && <span className="w-2 h-2 rounded-full bg-orange-500 shrink-0" title="Scan failed — usable, consider rescanning" />}
                                      {isHighVulnsWarn && <span className="w-2 h-2 rounded-full bg-amber-500 shrink-0" title="High-severity findings — usable, view details" />}
                                      {isPending ? <span className="w-2 h-2 rounded-full bg-yellow-400 shrink-0" title="Security scan in progress — usable, results coming" /> : null}
                                      {node.calibration_status === 'calibrated' && <span className="w-2 h-2 rounded-full bg-blue-500 shrink-0" />}
                                      {/* FX.73 p4 — licence badge. Reads the optional
                                          `licence` field on the node payload
                                          ({spdx, commercial_use: bool|null}) sourced
                                          from models.LicenceRecord. Absent => no badge;
                                          unknown commercial_use never claims "allowed". */}
                                      {node.licence?.spdx ? (
                                        <span className={`inline-flex items-center text-[10px] rounded px-1 ${node.licence?.commercial_use === false ? 'bg-red-100 text-red-700' : 'bg-muted-2 text-muted-foreground'}`}>{node.licence.spdx}</span>
                                      ) : null}
                                      {node.licence?.commercial_use === false ? (
                                        <span className="text-[10px] text-red-700 font-semibold" title="Non-commercial licence — commercial use is not permitted">non-commercial</span>
                                      ) : null}
                                      {/* FX.73 p4c — CE-edition badge. Reads the
                                          `ce_included` flag on the node payload,
                                          the single source of truth for CE
                                          inclusion (node_definitions.ce_included,
                                          stamped at CE seed time from the
                                          node-seed keep-list). Absent/false =>
                                          no badge; never claims CE. */}
                                      <CeBadge ceIncluded={node.ce_included} />
                                      {/* FX.103 p4 — deprecation/retirement badge.
                                          Reads lifecycle_state (part-2 alias of
                                          NodeDefinition.status) + retire_at
                                          (= retire_after) off the palette
                                          payload. 'active'/absent => no badge;
                                          'deprecated' => amber + "Retires on
                                          <date>" tooltip; 'retired' => grey
                                          (only visible under "Show retired"). */}
                                      <LifecycleBadge lifecycleState={lifecycleState} retireAt={node.retire_at ?? node.retire_after} />
                                      {/* FX.91 p2b — declared outbound network + this tenant's approval. Reads the palette payload's `egress_domains` (p2a) and `egress_approval` (p2c state, batched server-side). Absent declaration => no badge (the badge never claims "No network" for a node whose declaration it cannot see); empty declaration => neutral "No network" (fail-closed default-deny); amber = a tenant admin still has to approve; red = the gov profile blocks it regardless of approval. */}
                                      <EgressStateBadge node={node} />
                                      {/* FX.149 p3 — palette badge NAMING THE PROVIDER on external-AI nodes. Reads the `external-ai` soft tag on custom_metadata.tags (services/external_ai_node_gate.py EXTERNAL_AI_TAG, seeded by migrations/20261006v_fx149_external_ai_node_tags.py). Absent tag => no badge. Provider name comes from custom_metadata.external_ai_provider when carried, else from node_kind (the seeded nim_*_cloud kinds = NVIDIA NIM); never invents a provider it cannot see. The badge only names the provider — it does not claim the run is allowed (the run-start gate decides). */}
                                      <ExternalAiBadge node={node} />
                                    </span>
                                    {isPending ? (
                                      <span className="text-xs text-yellow-700 italic truncate">Security scan in progress — usable now</span>
                                    ) : isRejectedWarn ? (
                                      <span className="text-xs text-red-600 italic truncate">Admin flagged — usable, see details</span>
                                    ) : isScanFailedWarn ? (
                                      <span className="text-xs text-orange-600 italic truncate">Scan failed — usable, consider rescan</span>
                                    ) : isCriticalWarn ? (
                                      <span className="text-xs text-red-600 italic truncate">Critical CVEs — usable, view details</span>
                                    ) : isHighVulnsWarn ? (
                                      <span className="text-xs text-amber-700 italic truncate">High-severity findings — usable, view details</span>
                                    ) : (
                                      <span className="text-xs text-muted-foreground truncate">
                                        {node.description}
                                      </span>
                                    )}
                                  </div>
                                </Button>
                              }
                            >
                              <div className="flex items-start gap-4">
                                <div className="mt-1 flex-shrink-0">{icon}</div>
                                <div className="space-y-1">
                                  <h4 className="font-semibold">{node.name}</h4>
                                  {isPending && (
                                    <div className="text-xs bg-yellow-50 border border-yellow-200 rounded p-2 my-1">
                                      <div className="font-medium text-yellow-800">
                                        Waiting for security scan
                                      </div>
                                      <p className="text-yellow-700 mt-0.5">
                                        A Trivy CVE scan must complete before this node can be used in a workflow. Workers run scans sequentially — if the queue is deep this can take a few minutes.
                                      </p>
                                      {!isLocalNode && (node.id ?? 0) > 0 && (
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          className="mt-1.5 h-7 text-xs w-full"
                                          onClick={(e) => { e.stopPropagation(); setDetailsNode(node); }}
                                        >
                                          View Details
                                        </Button>
                                      )}
                                    </div>
                                  )}
                                  {node.scan_status === 'rejected' && (
                                    <div className="text-xs bg-red-50 border border-red-200 rounded p-2 my-1">
                                      <span className="font-medium text-red-700">Security: Rejected</span>
                                      {node.rejection_reason && <p className="text-red-600 mt-0.5 line-clamp-2">{node.rejection_reason}</p>}
                                      {node.scan_result?.summary && (
                                        <p className="text-red-500 mt-0.5">
                                          Critical: {node.scan_result.summary.critical || 0}, High: {node.scan_result.summary.high || 0}
                                        </p>
                                      )}
                                      {!isLocalNode && (node.id ?? 0) > 0 && (
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          className="mt-1.5 h-7 text-xs w-full"
                                          onClick={(e) => { e.stopPropagation(); setDetailsNode(node); }}
                                        >
                                          View Details
                                        </Button>
                                      )}
                                    </div>
                                  )}
                                  {node.scan_status === 'failed' && (
                                    <div className="text-xs bg-red-50 border border-red-200 rounded p-2 my-1">
                                      <span className="font-medium text-red-700">Scan failed</span>
                                      <p className="text-red-600 mt-0.5">The previous scan didn't complete. Click View Details for the full error and Retry button.</p>
                                      {!isLocalNode && (node.id ?? 0) > 0 && (
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          className="mt-1.5 h-7 text-xs w-full"
                                          onClick={(e) => { e.stopPropagation(); setDetailsNode(node); }}
                                        >
                                          View Details
                                        </Button>
                                      )}
                                    </div>
                                  )}
                                  {node.calibration_status && node.calibration_status !== 'uncalibrated' && (
                                    <div className="text-[10px] text-blue-600 font-medium">
                                      {node.calibration_status === 'calibrated' ? 'Calibrated' :
                                       node.calibration_status === 'calibrating' ? `Calibrating ${node.calibration_progress?.completed || 0}/${node.calibration_progress?.total || 9}` :
                                       node.calibration_status}
                                    </div>
                                  )}
                                  <p className="text-sm text-muted-foreground">
                                    {node.description}
                                  </p>
                                  {/* LDF-3 §3.4 — chip pair (cost + processing) */}
                                  <div className="flex items-center gap-1 pt-2">
                                    <NodeChips
                                      cost={node.cost_in_credits}
                                      costTiers={node.cost_credits_by_tier}
                                      gpuVariants={node.gpu_variants}
                                    />
                                    {node.cost_basis && (
                                      <CostBasisBadge costBasis={node.cost_basis} className="ml-1.5" />
                                    )}
                                  </div>
                                  {/* GO.9 — per-accelerator verified/unverified/
                                      incompatible/not-recommended badges */}
                                  {node.variant_status && (
                                    <div className="pt-1">
                                      <VariantStatusBadges variantStatus={node.variant_status} />
                                    </div>
                                  )}
                                  {/* node-code-edit-rebuild — Edit code / Copy as
                                      template. Everyone-facing (no admin flag
                                      gate); shown for any registered, non-local
                                      node and enforced server-side by RBAC/
                                      entitlement — the If/Else/Switch/Split/
                                      Human Approval sentinels (ids in localNodes
                                      or id <= 0) have no backing NodeDefinition
                                      row to edit. */}
                                  {!isLocalNode && (node.id ?? 0) > 0 && (
                                    <div className="flex items-center gap-1.5 pt-2">
                                      <Button
                                        size="sm"
                                        variant="outline"
                                        className="h-6 px-2 text-[11px]"
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          onEditNodeCode?.(node.id as number, node.name || "");
                                        }}
                                      >
                                        <Pencil className="w-3 h-3 mr-1" />
                                        Edit code
                                      </Button>
                                      <Button
                                        size="sm"
                                        variant="outline"
                                        className="h-6 px-2 text-[11px]"
                                        disabled={copyingIds.has(node.id as number)}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          handleCopyAsTemplate(node.id as number);
                                        }}
                                      >
                                        {copyingIds.has(node.id as number)
                                          ? <Loader2 className="w-3 h-3 mr-1 animate-spin" />
                                          : <Copy className="w-3 h-3 mr-1" />}
                                        Copy as template
                                      </Button>
                                    </div>
                                  )}
                                  {node.input_schema?.file_mode && (
                                    <div className="flex items-center pt-1">
                                      <span className="text-xs text-muted-foreground">
                                        Input Mode: {node.input_schema.file_mode}
                                      </span>
                                    </div>
                                  )}
                                  {node.output_schema?.file_mode && (
                                    <div className="flex items-center pt-1">
                                      <span className="text-xs text-muted-foreground">
                                        Output Mode: {node.output_schema.file_mode}
                                        {node.output_schema.paired !== undefined && ` (${node.output_schema.paired ? 'Paired' : 'Independent'})`}
                                      </span>
                                    </div>
                                  )}
                                </div>
                              </div>
                            </NodeHoverPanel>
                          );
                        })}
                    </CollapsibleContent>
                </Collapsible>
              ))}
          </div>
        ) : (
          <div className="flex justify-center items-center h-full">
            <p className="text-sm text-muted-foreground">
              No nodes found. Nodes are loaded by the API at start-up: reload the page; if it stays empty, check the API container logs.
            </p>
          </div>
        )}
      </ScrollArea>
      <div className="mt-4 text-xs text-gray-500 flex-shrink-0">
        Drag and drop nodes onto the canvas to build your workflow
      </div>

      <ScanDetailsDialog
        open={detailsNode !== null}
        onClose={() => setDetailsNode(null)}
        node={detailsNode}
        accessToken={accessToken}
        onRescan={async (nodeId) => {
          await handleScanNow(nodeId);
        }}
        rescanning={detailsNode?.id ? rescanningIds.has(detailsNode.id) : false}
      />
    </div>
  );
}