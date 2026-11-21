"use client";
import { apiUrl, dashboardUrl } from "@/lib/runtime-config";

import React, { useState, useEffect, useCallback } from 'react';
import { toast } from '@/components/ui/use-toast';
import type { WorkflowSummary, WorkflowVersion, NodeDefinition, Edge, NodeData, WorkflowExport, WorkflowImportResult, WorkflowValidateResult } from '@/lib/types';
import type { Node } from 'reactflow';
import { TOKEN_REFRESHED_EVENT, decodeTokenSub, readActiveIdentityCookie, decideStoredTokenTrust, clearWorkflowTokens, reconcileSession, forceReauth, tryRefreshToken, ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, getRefreshToken, getAccessToken, saveWorkflowTokens, getTokenClient, migrateLegacyLocalStorageTokens, broadcastLogout, AUTH_LOGOUT_EVENT, AUTH_LOGOUT_CHANNEL, parseWorkflowIdParam, decideColdLinkOutcome, stashPendingWorkflowId, consumePendingWorkflowId } from '@/lib/api-client';
import { startSilentSso, consumeSsoAttempted } from '@/lib/silent-sso';
import { startSessionIdleWarning, stopSessionIdleWarning } from '@/lib/session-idle-warning';
import { startProactiveTokenRefresh, stopProactiveTokenRefresh } from '@/lib/api-client';
import { safeLoadGraph, safeLoadGraphWithEnrichment } from '@/lib/graph-load-fallback';
import { parseHandoffTokens, stripHandoffTokens } from '@/lib/handoff-tokens';
import { usePollingBackoff } from '@/hooks/usePollingBackoff';
import { pollErrorForResponse } from '@/lib/poller';
import {
    countNodesWithUserInput,
    nodesHaveUserInput,
    stripUserInputFromNodes,
    getSanitizationSuppressed,
    setSanitizationSuppressed,
    userInputSnapshot,
} from '@/lib/save-sanitization';
import {
    parseComplianceViolationError,
    type ComplianceViolation,
} from '@/lib/compliance-violations';
import { useEntitlements } from '@/hooks/use-entitlements';

// API Configuration
const API_URL = apiUrl();
const DASHBOARD_URL = dashboardUrl();

type SaveDialogMode = "new" | "saveAs";
type DialogView = "workflows" | "versions";

// 2400UI127 — keystrokes inside this window collapse into one list fetch.
const WORKFLOW_SEARCH_DEBOUNCE_MS = 300;

// FIX-WB-COLD-LINK-DENIED-BEFORE-SILENT-SSO (Q9=B): how long initializeAuth waits
// for a started silent-SSO round-trip to land a token before deciding the cold
// link on the WB origin. A healthy round-trip is well under this; a hung or
// dead-ended one must not strand the visitor on a blank canvas.
const SILENT_SSO_LANDING_BUDGET_MS = 6000;

type NodeRun = {
    id: number;
    workflow_node_id: number;
    status: 'queued' | 'running' | 'success' | 'failure';
    started_at?: string;
    error_message?: string;
    workflow_node?: { graph_node_id: string };
};

type PreviewMap = {
    [graphNodeId: string]: Array<{
        url: string;
        file_extension: string;
        category: 'image' | 'model3d' | 'video' | 'audio' | 'text' | 'pdf' | 'splat' | 'pointcloud' | 'font' | 'archive' | 'other';
        content_hash: string;
        logical_name: string;
    }>;
};

// Helper function to simulate C4 hash generation
const calculateC4Hash = async (file: File): Promise<string> => {
    const buffer = await file.arrayBuffer();
    const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    const hashHex = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
    return `c4${hashHex.substring(0, 52)}`;
};

// Refresh All Nodes must never delete system rows: rows with a null tenant_id are
// shared platform nodes (seeded at startup). Hygiene only; the server guard on
// DELETE /nodes/{id} stays authoritative.
export const selectRefreshDeletableNodes = (nodes: NodeDefinition[]): NodeDefinition[] =>
    nodes.filter((n) => typeof n.tenant_id === "number");

export const useWorkflowApi = (
    nodes: Node<NodeData>[],
    edges: Edge[],
    setNodes: (nodes: Node[] | ((prevNodes: Node[]) => Node[])) => void,
    setEdges: (edges: Edge[]) => void,
    fileStore: { [key: string]: File },
    // Clears the undo/redo history stack. Called whenever the canvas is
    // replaced wholesale with a new baseline (new workflow, load a version,
    // watch a run, import) so undo can't cross into the previously loaded graph.
    // Defaults to a no-op so the hook remains usable without a history stack.
    resetHistory: () => void = () => {}
) => {
    // Simple dirty flag — set true by updateNodeData, cleared by save/load
    // BUG-1027t: the dirty flag must be read LIVE at decision time. A
    // render-time snapshot of the ref's value froze at the hook's first render
    // (it is never re-initialised), so executeWorkflow / watchRun kept seeing
    // "dirty" after a successful save.
    const _isDirty = React.useRef(false);
    const markDirty = React.useCallback(() => { _isDirty.current = true; }, []);
    // CAT-371: flag that cancel was requested so the poller can stop after N polls
    // if the backend leaves the run in CANCELLING (non-terminal).
    const cancelRequestedRef = React.useRef(false);
    const pollCountRef = React.useRef(0);
    const MAX_POLLS_AFTER_CANCEL = 30; // 60s at 2s base; cancels the poll loop itself

    // 2400UI24 — the install's edition, from the SAME entitlements source of
    // truth the rest of the builder gates on (components/node-library.tsx:179
    // `isCE`, components/assistant-panel.tsx:222). The /tenant/warnings
    // pre-check is the ONE place this read must be FAIL-CLOSED rather than
    // fail-open: on CE the route is not mounted at all (open-core/manifest.yaml
    // denies CORE/API/routers/tenant/*.py), so an unknown edition must NOT
    // fetch it — only a POSITIVELY resolved paid edition does. `resolved` is
    // true only when /platform/features returned a real entitlements block, so
    // loading/unresolved/errored states skip the pre-check (the server's 402
    // stays authoritative either way; skipping a warning toast is harmless).
    const { edition, resolved } = useEntitlements();
    const isPaidEdition = resolved && edition !== "ce";

    const [accessToken, setAccessToken] = useState<string | null>(null);
    const [refreshToken, setRefreshToken] = useState<string | null>(null);
    // Mirror of the access token this tab is actively using, so the reactive
    // session-reconcile listeners (focus / visibility / storage) can read the
    // current value without being re-registered on every token change.
    const accessTokenRef = React.useRef<string | null>(null);
    const [currentWorkflow, setCurrentWorkflow] = useState<WorkflowSummary | null>(null);
    const [workflowName, setWorkflowName] = useState<string>("");
    const [currentVersionNumber, setCurrentVersionNumber] = useState<number | null>(null);
    // currentVersionId is the DB primary key of the loaded WorkflowVersion.
    // The wizard's merge-apply path needs it to land a NEW version on the
    // SAME workflow (canvas-aware-workflow-wizard §6+§8). version_number
    // alone isn't load-bearing — the server identifies versions by id.
    const [currentVersionId, setCurrentVersionId] = useState<number | null>(null);
    const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
    // Set when opening a workflow the user has no access to (API 403 with
    // code 'workflow_access_denied'); drives the "request access" dialog.
    const [accessDenied, setAccessDenied] = useState<{ workflowId: number; workflowName: string } | null>(null);
    // GG.8 sealed workflows — set when the selected workflow is a SEALED
    // installed copy (API 403 "sealed" on /road/{id}/versions). The
    // canvas can't show its graph, so the builder renders the SealedAppForm
    // (inputs -> run -> outputs) instead. Null means "show the canvas".
    const [sealedWorkflowId, setSealedWorkflowId] = useState<number | null>(null);
    // Set when a RUN is blocked because the caller lacks access to secrets or
    // input files the workflow uses (API 403 secret_access_denied /
    // file_access_denied). Drives the "request access to run" dialog.
    const [runBlocked, setRunBlocked] = useState<{
        message: string;
        secrets: string[];
        cogIds: number[];
        // GG.UI.6b — structured purchase-required pre-flight (402
        // purchase_required): parsed blocked-run reasons shown by
        // BlockedRunPanel instead of the secret/file request-access rows.
        purchaseReasons?: import("@/lib/blocked-run").BlockedRunReason[];
    } | null>(null);
    const [versions, setVersions] = useState<WorkflowVersion[]>([]);
    // Multi-run switcher: `selectedRunId` is the run the CANVAS is bound to (may
    // be a live run OR a past one picked from the run rail); `selectedRunActive`
    // is whether that run is still live (drives the Stop button + the live poll).
    // `pollingRunId` is kept as a DERIVED alias (the active run, or null) so the
    // toolbar's Execute/Stop wiring, preview/download consumers, and the existing
    // start/cancel code paths keep working unchanged via the setPollingRunId shim.
    const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
    const [selectedRunActive, setSelectedRunActive] = useState<boolean>(false);
    const pollingRunId: number | null = selectedRunActive ? selectedRunId : null;
    // Shim: existing callers do setPollingRunId(id) to "watch a run" and
    // setPollingRunId(null) to stop watching. Map that onto the selected-run model
    // — an id selects + activates a run; null just marks it inactive (the run
    // stays on the canvas showing its final state). selectRun() (exposed to the
    // rail) is the same as setPollingRunId(id): pick any run to view/watch.
    const setPollingRunId = (id: number | null) => {
        if (id == null) { setSelectedRunActive(false); }
        else { setSelectedRunId(id); setSelectedRunActive(true); }
    };
    const selectRun = (id: number) => {
        // Switching runs re-fetches this run's node states/previews (a few
        // seconds) — surface a loading toast so the user isn't staring at a
        // stale canvas wondering what's happening.
        setPollingRunId(id);
        try { toast({ title: `Loading run #${id}…`, description: "Fetching this run's node states and previews." }); } catch { /* toast optional */ }
    };
    // "Fresh run" toggle. Default OFF = reuse unchanged node results via the
    // output cache (Airflow-style: re-running skips work that didn't change). ON
    // = ignore the cache and re-execute everything (needed when a node is
    // non-deterministic — e.g. generative image/LLM — and you want new output).
    // Execute sends reuse_cache = !freshRun.
    const [freshRun, setFreshRun] = useState<boolean>(false);

    const [isSaveDialogOpen, setSaveDialogOpen] = useState<boolean>(false);
    const [saveDialogMode, setSaveDialogMode] = useState<SaveDialogMode>("new");
    // Save-time sanitization prompt state (issue #21). Toggled when the
    // user hits Save on a workflow whose nodes carry per-run user input.
    // The pending-mode ref tracks whether the in-flight save was a
    // version save (current workflow) or a saveAs (rename + new
    // workflow) so the confirm callbacks know which inner handler to
    // dispatch.
    const [isSanitizationDialogOpen, setSanitizationDialogOpen] = useState<boolean>(false);
    const [sanitizationAffectedCount, setSanitizationAffectedCount] = useState<number>(0);
    const pendingSanitizationModeRef = React.useRef<"version" | "saveAs" | null>(null);
    // Compliance-violation dialog (FORTRESS 5.4). Fired when the compose-time
    // policy gate returns 422 with structured violations.
    const [complianceViolationsOpen, setComplianceViolationsOpen] = useState<boolean>(false);
    const [pendingComplianceViolations, setPendingComplianceViolations] = useState<ComplianceViolation[]>([]);
    const [isLoadDialogOpen, setLoadDialogOpen] = useState<boolean>(false);
    // Load dialog can show either the active workflows (default) or the
    // archived ones (archived_at set). Archived view swaps the per-row Archive
    // action for a Restore (un-archive) action.
    const [archivedView, setArchivedView] = useState<boolean>(false);
    const [isEditDialogOpen, setEditDialogOpen] = useState<boolean>(false);
    const [isConfirmDeleteDialogOpen, setConfirmDeleteDialogOpen] = useState<boolean>(false);
    const [isConfirmRefreshDialogOpen, setConfirmRefreshDialogOpen] = useState<boolean>(false);
    const [isMissingFileDialogOpen, setMissingFileDialogOpen] = useState(false);
    const [isExecuting, setIsExecuting] = useState(false);
    const [saveBeforeRunOpen, setSaveBeforeRunOpen] = useState(false);
    // Set when the user hits Execute on a workflow that has never been
    // saved (no id/version to run). Drives the "save before running"
    // prompt instead of a silent dead button.
    const [saveRequiredBeforeRunOpen, setSaveRequiredBeforeRunOpen] = useState(false);
    const _skipUnsavedCheck = React.useRef(false);
    const [isImportDialogOpen, setImportDialogOpen] = useState(false);
    const [importValidation, setImportValidation] = useState<WorkflowValidateResult | null>(null);
    const [pendingImportData, setPendingImportData] = useState<WorkflowExport | null>(null);
    const [isImporting, setIsImporting] = useState(false);
    const [isConfirmCancelRestartDialogOpen, setConfirmCancelRestartDialogOpen] = useState(false);
    const [activeRunInfo, setActiveRunInfo] = useState<{
        workflow_run_id: number;
        orchestrator_run_id: string | null;
        status: string;
        started_at: string | null;
    } | null>(null);
    const [pendingExecuteParams, setPendingExecuteParams] = useState<{
        latestVersionId: number;
        allCogIds: number[];
        allMetadataCogIds: number[];
    } | null>(null);

    const [newWorkflowName, setNewWorkflowName] = useState<string>("");
    const [dialogView, setDialogView] = useState<DialogView>("workflows");
    const [deletableNodes, setDeletableNodes] = useState<NodeDefinition[]>([]);
    const [nodeToDelete, setNodeToDelete] = useState<NodeDefinition | null>(null);
    const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
    // nodeLibraryKey retired — the library now listens for a
    // `mod:nodes:changed` window event and re-fetches in-place.
    // Key-bumping forced an unmount/remount of the whole sidebar
    // subtree, which kept wiping wizards/dialogs rendered above it.
    const [nodeLibraryKey] = useState(0);
    const setNodeLibraryKey = (_: unknown) => {
        // Backwards-compat no-op shim for any call site we missed.
        // Prefer window.dispatchEvent(new Event("mod:nodes:changed")).
        window.dispatchEvent(new Event("mod:nodes:changed"));
    };
    const [missingFileNodeName, setMissingFileNodeName] = useState("");

    // --- Centralized fetch utility to handle session errors ---
    //
    // Earlier version nuked the whole tab on ANY 401 (even from background
    // polls). That wiped in-progress wizard state, persisted build-progress
    // cards, and anything else the user was working on — and fired at random
    // moments whenever the access token happened to expire mid-session.
    //
    // New behaviour: on 401, try /auth/refresh-token with the stored refresh
    // token. If refresh succeeds, retry the original request with the new
    // access token + update state. Only redirect to Dashboard on a HARD
    // auth failure (refresh fails or no refresh token available).
    // J159b — no hook-local refresh. The old inline version hardcoded
    // `client_id: "dashboard-client"` for whatever refresh token the tab held,
    // including workflow-builder-client tokens minted by the silent-SSO path
    // (lib/silent-sso.ts). Keycloak refresh tokens are CLIENT-BOUND, so that
    // refresh either failed (hard bounce) or minted under the wrong client.
    // lib/api-client.ts's exported tryRefreshToken is the single source of
    // truth: it routes by getTokenClient() — dashboard-client goes via the BFF
    // (server-held client_secret), anything else refreshes Keycloak-direct
    // under the token's own client. It also owns the parallel-401 dedupe and
    // dispatches TOKEN_REFRESHED_EVENT, which the listener below feeds back
    // into this hook's React state.

    const authenticatedFetch = useCallback(async (url: string, options: RequestInit) => {
        const response = await fetch(url, options);

        if (response.status !== 401) {
            return response;
        }

        // 401 — attempt a silent refresh before giving up.
        const fresh = await tryRefreshToken();
        if (fresh) {
            // Re-fire the original request with the fresh token. Merge
            // Authorization in case the caller didn't supply it here
            // (some callers build headers separately).
            const mergedHeaders = {
                ...(options.headers as Record<string, string> | undefined),
                Authorization: `Bearer ${fresh}`,
            };
            return fetch(url, { ...options, headers: mergedHeaders });
        }

        // Refresh failed or no refresh token — hard redirect. This is
        // the only path that takes the user off the page; background
        // polls with a transient 401 never reach here.
        toast({
            title: "Session Expired",
            description: "Your session has timed out. Redirecting to login...",
            variant: "destructive",
        });
        // GSEC R-077: tokens live in sessionStorage; clear this tab's and
        // broadcast so every other WB tab drops its session too.
        sessionStorage.clear();
        clearWorkflowTokens();
        broadcastLogout();
        window.location.href = DASHBOARD_URL;
        throw new Error("Session expired");
    }, [tryRefreshToken]);

    // Fetch output folders after workflow completion
    const fetchOutputFolders = useCallback(async (runId: number) => {
        if (!accessToken) return;

        try {
            const response = await fetch(`${API_URL}/road/workflow-runs/${runId}/output-folders`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });

            if (!response.ok) {
                // If no output folders found, just show generic completion message
                toast({
                    title: "Workflow Complete",
                    description: "Processing has finished."
                });
                return;
            }

            const outputFolders = await response.json();

            if (outputFolders && outputFolders.length > 0) {
                // Show completion with folder names
                const folderNames = outputFolders.map((f: any) => f.name).join(', ');

                toast({
                    title: "✅ Workflow Complete!",
                    description: `Results saved to: ${folderNames}. Open Dashboard to view your files.`,
                    duration: 10000, // Show for 10 seconds
                });

                // Dispatch event to notify file browser to refresh
                window.dispatchEvent(new CustomEvent('workflow-completed', {
                    detail: { outputFolders, runId }
                }));
            } else {
                toast({
                    title: "Workflow Complete",
                    description: "Processing has finished."
                });

                // Still dispatch event even without output folders
                window.dispatchEvent(new CustomEvent('workflow-completed', {
                    detail: { outputFolders: [], runId }
                }));
            }
        } catch (error) {
            console.error("Error fetching output folders:", error);
            toast({
                title: "Workflow Complete",
                description: "Processing has finished."
            });
        }
    }, [accessToken]);

    // Fetch presigned preview URLs for node outputs
    const fetchNodePreviews = useCallback(async (runId: number) => {
        if (!accessToken) return;

        try {
            // clean-v2-api: /v1/runs (was /v1/node-runs; old path kept as a
            // back-compat alias — see CORE/API/routers/runs.py).
            const response = await fetch(`${API_URL}/runs/${runId}/previews`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });

            if (!response.ok) return;

            const previews: PreviewMap = await response.json();

            setNodes((currentNodes) =>
                currentNodes.map((node) => {
                    const nodeAssets = previews[node.id];
                    if (nodeAssets && nodeAssets.length > 0) {
                        return { ...node, data: { ...node.data, previewAssets: nodeAssets } };
                    }
                    return node;
                })
            );
        } catch (error) {
            console.error("Error fetching node previews:", error);
        }
    }, [accessToken, setNodes]);


    // Keep hook state in sync when a background component (e.g.
    // BuildQueueRow) silently refreshes the token via the shared
    // api-client. Without this, hook-initiated fetches would keep
    // sending the stale access token for one more round-trip before
    // their own 401+refresh cycle caught up.
    useEffect(() => {
        const handler = (e: Event) => {
            const detail = (e as CustomEvent<{ accessToken?: string; refreshToken?: string }>).detail;
            if (!detail) return;
            if (detail.accessToken) setAccessToken(detail.accessToken);
            if (detail.refreshToken) setRefreshToken(detail.refreshToken);
        };
        window.addEventListener(TOKEN_REFRESHED_EVENT, handler);
        return () => window.removeEventListener(TOKEN_REFRESHED_EVENT, handler);
    }, []);

    // Initialize auth - get tokens from URL parameters (passed from Dashboard)
    useEffect(() => {
        const initializeAuth = async () => {
            // GSEC R-077: one-time migration — delete any token keys a prior
            // release left in localStorage (they are NOT copied to
            // sessionStorage; the user re-authenticates cleanly).
            migrateLegacyLocalStorageTokens();

            // GSEC R-074: tokens arrive in the URL FRAGMENT (never sent to the
            // server, so never logged); the legacy query-string form is still
            // accepted for one release so links from an older Dashboard work.
            // parseHandoffTokens/stripHandoffTokens (lib/handoff-tokens.ts) —
            // strip removes BOTH forms via replaceState and keeps every other
            // query param (workflow, resume, node_search, …) intact.
            const { accessToken: tokenParam, refreshToken: refreshTokenParam } =
                parseHandoffTokens(window.location);

            // If tokens in URL, store them (sessionStorage, R-077) and clean
            // up the URL. The handoff comes from the Dashboard, so the token
            // was minted by dashboard-client — leave the default client in
            // effect for later refreshes.
            if (tokenParam) {
                // Q9=B (cold deep link): a Dashboard handoff lands on the WB
                // ROOT with the tokens in the fragment, so the `?workflow=<id>`
                // a visitor was aiming at is gone by the time we get here. If
                // this tab stashed a pending id (login round trip) and the URL
                // no longer carries the param, put it back — rebuilt from the
                // validated integer only, never from a stored string.
                // replaceState keeps the fragment so stripHandoffTokens below
                // can still clean it. A successful login also retires any
                // stale silent-SSO loop-guard marker.
                const pendingHandoffId = consumePendingWorkflowId();
                consumeSsoAttempted();
                if (pendingHandoffId !== null && parseWorkflowIdParam(window.location.search) === null) {
                    const u = new URL(window.location.href);
                    u.searchParams.set("workflow", String(pendingHandoffId));
                    window.history.replaceState({}, "", u.pathname + u.search + u.hash);
                }
                saveWorkflowTokens(tokenParam, refreshTokenParam);
                setAccessToken(tokenParam);

                // Store refresh token if provided (needed for long-running workflows)
                if (refreshTokenParam) {
                    setRefreshToken(refreshTokenParam);
                }

                // Clean up URL to remove tokens (fragment + legacy query)
                stripHandoffTokens(window.location, window.history);
                return;
            }

            // Check for existing tokens (sessionStorage, R-077 — per-tab)
            const storedToken = getAccessToken();
            const storedRefreshToken = getRefreshToken();

            if (storedToken) {
                // Cross-user session-bleed guard (security). WB is a separate
                // origin from the Dashboard, so a Dashboard logout / user
                // switch cannot clear WB's localStorage. Before trusting a
                // stored token on a load that carried NO fresh ?token=,
                // confirm it belongs to the currently-active user via the
                // parent-domain identity cookie the Dashboard maintains. On a
                // mismatch or an explicit logout marker, wipe WB's tokens and
                // force a fresh Dashboard handoff instead of resurrecting the
                // prior user's session and showing their workflows.
                const storedSub = decodeTokenSub(storedToken);
                const cookieIdentity = readActiveIdentityCookie();
                if (decideStoredTokenTrust(storedSub, cookieIdentity) === 'reject') {
                    console.warn(
                        'WB auth: stored token does not match the active ' +
                        'Dashboard identity (user switch or logout) — ' +
                        'clearing WB session and forcing a fresh handoff.',
                    );
                    clearWorkflowTokens();
                    // Q9=B: a user switch or an explicit logout legitimately
                    // needs the Dashboard, but the deep link must survive the
                    // round trip — remember the id (bare integer) so the
                    // handoff back to WB restores `?workflow=<id>`.
                    const pid = parseWorkflowIdParam(window.location.search);
                    if (pid !== null) stashPendingWorkflowId(pid);
                    window.location.href = DASHBOARD_URL;
                    return;
                }

                // Verify token is still valid. AbortController with a
                // 10s budget — pre-fix this fetch had no timeout, so a
                // hung API (mid-restart, NAT timeout, lost packet w/o
                // RST) left the WB stuck on <Loading…> forever and
                // playtesters had to refresh. 10s is well above a
                // healthy /me round-trip (<200ms) and well below the
                // browser's default fetch timeout (minutes).
                const ctrl = new AbortController();
                const timeoutId = window.setTimeout(() => ctrl.abort(), 10_000);
                let timedOut = false;
                try {
                    const response = await fetch(`${API_URL}/me`, {
                        headers: { 'Authorization': `Bearer ${storedToken}` },
                        signal: ctrl.signal,
                    });
                    window.clearTimeout(timeoutId);

                    if (response.ok) {
                        setAccessToken(storedToken);
                        if (storedRefreshToken) {
                            setRefreshToken(storedRefreshToken);
                        }
                        return;
                    }
                    // 401 / 403. Before bouncing, distinguish "the access token
                    // simply expired for the SAME user" from "wrong user".
                    // decideStoredTokenTrust already confirmed above this is not
                    // a cross-user / logged-out token (we'd have rejected), so a
                    // 401 here is an expired access token for a still-valid
                    // session — try the held refresh token so a same-user reload
                    // isn't kicked out. Only if refresh fails do we wipe+bounce.
                    if (storedRefreshToken) {
                        const refreshed = await tryRefreshToken();
                        if (refreshed) {
                            setAccessToken(refreshed);
                            const rotatedRefresh = getRefreshToken();
                            if (rotatedRefresh) setRefreshToken(rotatedRefresh);
                            return;
                        }
                    }
                    // Refresh unavailable or rejected — token is dead, fall
                    // through to the silent-SSO / login path below.
                    clearWorkflowTokens();
                } catch (error) {
                    window.clearTimeout(timeoutId);
                    timedOut = (error as any)?.name === 'AbortError';
                    if (timedOut) {
                        console.warn(
                            'WB auth: /me did not respond within 10s — ' +
                            'forcing redirect to Dashboard for fresh login. ' +
                            'Tim 2026-06-04: "force" on WB hang.',
                        );
                    } else {
                        console.error('Token validation failed:', error);
                    }
                    // Wipe tokens on any failure so the next visit
                    // doesn't replay against the same stale value.
                    clearWorkflowTokens();
                }
            }

            // No valid token in this tab (fresh tab, expired+unrefreshable,
            // or /me timed out). GSEC R-077 / Q27=A: before showing any login
            // screen, try a SILENT SSO re-auth — a Keycloak prompt=none PKCE
            // round-trip. If the user has an active SSO session (logged into
            // the Dashboard on the same parent domain), Keycloak redirects
            // straight back to /auth/callback with a code and the user never
            // sees a login screen. If there is no session, Keycloak answers
            // `login_required` and /auth/callback bounces to the normal
            // Dashboard login. If we can't even build the auth request, go
            // straight to the Dashboard.
            const returnTo = window.location.pathname + window.location.search;
            const pendingId = parseWorkflowIdParam(window.location.search);
            // Q9=B: `back` is non-null only when we just came back from
            // /auth/callback after Keycloak said `login_required` (the
            // callback marked the attempt and bounced us to this link). Do
            // NOT start silent SSO a second time — that is the loop guard.
            const back = consumeSsoAttempted();
            let priorLogin: boolean;
            if (back === null) {
                priorLogin = Boolean(storedToken);
                // FIX-WB-COLD-LINK-DENIED-BEFORE-SILENT-SSO (Q9=B): `startSilentSso`
                // returns true as soon as the auth URL BUILDS - it never proves a
                // token landed for this principal. The old unconditional
                // early return right after the call made the decide block below (and the
                // access-denied dialog) unreachable whenever the round-trip never
                // landed, so a stale-login cold link painted a blank canvas
                // instead of the denial. Fail closed: capture the WB origin BEFORE
                // the redirect, wait a bounded budget for the round-trip to land a
                // token (the owner still wins, unchanged), and if none lands,
                // restore the WB origin + the link the visitor was aiming at and
                // fall through to the decide below. Never deny before the round-trip
                // has landed or timed out.
                const wbOrigin = window.location.origin;
                const started = await startSilentSso(returnTo, { priorLogin });
                if (started) {
                    let landed = false;
                    const landingDeadline = Date.now() + SILENT_SSO_LANDING_BUDGET_MS;
                    while (Date.now() < landingDeadline) {
                        if (getAccessToken()) { landed = true; break; }
                        await new Promise<void>((snooze) => window.setTimeout(snooze, 200));
                    }
                    if (landed) return;
                    console.log('WB auth: silent SSO started but no token landed, deciding cold link outcome');
                    window.location.href = wbOrigin + returnTo;
                } else {
                    console.log('WB auth: silent SSO unavailable, deciding cold link outcome');
                }
            } else {
                priorLogin = back.priorLogin;
            }
            const outcome = decideColdLinkOutcome({
                pendingWorkflowId: pendingId,
                priorLogin,
                identityCookie: readActiveIdentityCookie(),
            });
            if (outcome === "denied" && pendingId !== null) {
                // Stale login on a deep link: stay on the WB origin and show
                // the existing access-denied dialog with the GENERIC name
                // (`workflow <id>`) — identical text for existing, missing and
                // other-tenant ids, so nothing leaks beyond the API's 403
                // envelope. The real name only ever comes from the API's
                // structured 403 on a live-token path (unchanged).
                setAccessDenied({ workflowId: pendingId, workflowName: `workflow ${pendingId}` });
                return;
            }
            // Never-logged-in visitor (Q9 case 2) or no deep link at all
            // (case 4 = today's behaviour): Dashboard login, and the link is
            // remembered so the handoff returns to `/?workflow=<id>`.
            if (pendingId !== null) stashPendingWorkflowId(pendingId);
            window.location.href = DASHBOARD_URL;
        };

        initializeAuth();
    }, []);

    // GSEC FX.54 part 2 (Q37=A): idle-warning dialog at T-2 min before idle
    // logout, with "Stay signed in" (refreshes the token) and "Sign out". The
    // tier numbers come from GET /me's session_tier field.
    useEffect(() => {
        if (!accessToken) return;
        void startSessionIdleWarning(accessToken);
        return () => stopSessionIdleWarning();
    }, [accessToken]);

    // : the Dashboard refreshes its access
    // token ahead of expiry ("Started proactive token refresh interval"); the
    // builder only ever refreshed REACTIVELY (authenticatedFetch saw a 401
    // first), so near the short token life the owner's Workflow Builder
    // requests raced the expiry and 401'd — and a wave of parallel 401s
    // storming one refresh. Arm the same window here: fire tryRefreshToken
    // ~60 s before the current access token's `exp`, re-arm after each
    // refresh, cancel on logout/unmount.
    useEffect(() => {
        if (!accessToken) return;
        void startProactiveTokenRefresh(accessToken);
        return () => stopProactiveTokenRefresh();
    }, [accessToken]);

    // Keep the ref in sync so the reactive reconcile listeners below always
    // read the token this tab is currently using.
    useEffect(() => {
        accessTokenRef.current = accessToken;
    }, [accessToken]);

    // Reactive cross-user session-bleed guard (the persistent-bleed fix).
    //
    // initializeAuth's cookie check runs ONCE, on mount. A WB tab left open
    // never re-runs it, so a user switch that happens AFTER the tab loaded
    // (log out qa-owner → log in qa-user in the Dashboard, while an old
    // qa-owner WB tab stays open) was never caught — the stale tab kept
    // showing qa-owner's data. Re-validate whenever the tab regains focus /
    // visibility, and immediately when a sibling tab rewrites this origin's
    // stored token, so a stale tab can never fetch or display another user's
    // data. See reconcileSession() in api-client for the decision table.
    useEffect(() => {
        let bouncing = false;

        const reconcile = () => {
            if (bouncing) return;
            const inMemoryToken = accessTokenRef.current;
            // No token yet (mount still initialising, or already bounced) —
            // let initializeAuth own that path.
            if (!inMemoryToken) return;

            // R-077: tokens are per-tab (sessionStorage), so the "stored"
            // token this tab reads is always its own — no cross-tab rotation
            // to adopt. The cookie-based identity check (cross-origin, the
            // real bleed guard) is what reconcileSession still relies on.
            const storedToken = getAccessToken();
            const inMemorySub = decodeTokenSub(inMemoryToken);
            const storedSub = decodeTokenSub(storedToken);
            const cookieIdentity = readActiveIdentityCookie();

            const action = reconcileSession(inMemorySub, storedSub, cookieIdentity);
            if (action === 'ok') {
                return;
            }

            // Cross-user / logout → this tab is stale. Bounce through a fresh
            // Dashboard handoff BEFORE any further data fetch. Wipe the stored
            // token only when it is itself the offending stale one; when a
            // sibling already stored the correct new-user token, keep it.
            bouncing = true;
            console.warn(
                'WB auth: active session no longer matches this tab ' +
                `(${action}) — forcing a fresh Dashboard handoff to prevent ` +
                'cross-user data bleed.',
            );
            forceReauth(action === 'reauth-wipe');
        };

        const onStorage = (e: StorageEvent) => {
            if (e.key === ACCESS_TOKEN_KEY || e.key === REFRESH_TOKEN_KEY || e.key === null) {
                reconcile();
            }
        };
        const onVisibility = () => {
            if (document.visibilityState === 'visible') reconcile();
        };

        // Cross-tab logout (R-077). sessionStorage is per-tab, so a logout in
        // a sibling WB tab can't touch this tab's tokens. Two signals: the
        // BroadcastChannel message (fast path) and the localStorage logout
        // sentinel (storage-event fallback for browsers without BroadcastChannel).
        // On either, drop THIS tab's tokens and go to the logged-out state.
        let logoutBounced = false;
        const onLogout = () => {
            if (logoutBounced) return;
            logoutBounced = true;
            console.warn('WB auth: logout in a sibling tab — clearing this tab and signing out.');
            forceReauth(true);
        };
        let logoutChannel: BroadcastChannel | null = null;
        if (typeof BroadcastChannel !== "undefined") {
            logoutChannel = new BroadcastChannel(AUTH_LOGOUT_CHANNEL);
            logoutChannel.onmessage = (e: MessageEvent) => {
                if (e?.data && e.data.type === "logout") onLogout();
            };
        }
        const onLogoutStorage = (e: StorageEvent) => {
            if (e.key === "mod_wb_logout_sentinel") onLogout();
        };

        window.addEventListener('focus', reconcile);
        window.addEventListener('storage', onStorage);
        window.addEventListener('storage', onLogoutStorage);
        document.addEventListener('visibilitychange', onVisibility);
        return () => {
            window.removeEventListener('focus', reconcile);
            window.removeEventListener('storage', onStorage);
            window.removeEventListener('storage', onLogoutStorage);
            document.removeEventListener('visibilitychange', onVisibility);
            if (logoutChannel) logoutChannel.close();
        };
    }, []);

    // Mark every node queued when a run starts polling.
    useEffect(() => {
        if (!pollingRunId || !accessToken) return;
        setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: 'queued' } })));
    }, [pollingRunId, accessToken, setNodes]);

    // Poll node statuses for the active run (2s base) through the shared helper:
    // backs off on 5xx, stops-after-N with the reconnect banner, pauses when the
    // tab is hidden, single-flight. A transient failure throws so the poller
    // slows down instead of hammering; a terminal condition clears pollingRunId,
    // which tears the poller down via its enabled dep.
    usePollingBackoff(
        async () => {
            if (!pollingRunId || !accessToken) return;
            // Polling doesn't use authenticatedFetch to avoid toasts on every check
            // clean-v2-api: /v1/runs (was /v1/node-runs; old path kept as a
            // back-compat alias — see CORE/API/routers/runs.py).
            const response = await fetch(`${API_URL}/runs/${pollingRunId}`, {
                headers: { Authorization: `Bearer ${accessToken}` },
            });

            if (response.status === 404 || response.status === 401 || response.status === 403) {
                setPollingRunId(null);
                if (response.headers.get("X-Error-Code")) {
                    toast({
                        title: "Session Expired",
                        description: "Your session has timed out. Please log in again.",
                        variant: "destructive",
                    });
                }
                return;
            }
            // Transient (5xx / timeout / 429 / 503) → throw so the poller backs
            // off instead of hammering a struggling API.
            const _pollErr = pollErrorForResponse(response);
            if (_pollErr) throw _pollErr;

            // CAT-371: stop polling if cancel was requested and we've exceeded
            // MAX_POLLS_AFTER_CANCEL (the backend may leave the run CANCELLING
            // for too long, and the poller won't stop on a non-terminal success).
            pollCountRef.current += 1;
            if (cancelRequestedRef.current && pollCountRef.current > MAX_POLLS_AFTER_CANCEL) {
                setPollingRunId(null);
                toast({
                    title: "Run Cancelled",
                    description: "Polling stopped after max polls (backend may have left the run in CANCELLING).",
                });
                return;
            }

                const nodeRuns: NodeRun[] = await response.json();

                setNodes((currentNodes) => {
                    const updated = currentNodes.map((node) => {
                        const matchingRun = nodeRuns.find(run =>
                            run.workflow_node?.graph_node_id === node.id
                        );
                        if (matchingRun) {
                            return { ...node, data: {
                                ...node.data,
                                status: matchingRun.status,
                                error_message: matchingRun.error_message,
                                started_at: matchingRun.started_at,
                            } };
                        }
                        return node;
                    });

                    // For running process nodes: find their parent's preview and set as inputPreviewUrl.
                    // This shows what's being processed with a "Processing..." overlay.
                    return updated.map((node) => {
                        if (node.data.status !== 'running' || node.data.inputPreviewUrl) return node;

                        // Find parent nodes via edges
                        const parentIds = edges
                            .filter((e: any) => e.target === node.id)
                            .map((e: any) => e.source);

                        for (const pid of parentIds) {
                            const parent = updated.find((n) => n.id === pid);
                            const previewAssets = parent?.data?.previewAssets;
                            if (previewAssets && previewAssets.length > 0) {
                                // Prefer image, fall back to any asset
                                const asset = previewAssets.find(
                                    (a: any) => a.category === 'image'
                                ) || previewAssets[0];
                                if (asset) {
                                    return {
                                        ...node,
                                        data: {
                                            ...node.data,
                                            inputPreviewUrl: asset.url,
                                            inputPreviewCategory: asset.category,
                                        },
                                    };
                                }
                            }
                        }
                        return node;
                    });
                });

                // Fetch previews for any completed nodes (including newly completed)
                const completedNodeIds = nodeRuns
                    .filter(run => run.status === 'success')
                    .map(run => run.workflow_node?.graph_node_id)
                    .filter(Boolean);

                if (completedNodeIds.length > 0) {
                    fetchNodePreviews(pollingRunId).catch(() => {});
                }

                // Run is done when every NodeRun reached a terminal state.
                // Two earlier bugs hit by the old `.every(success|failure)`
                // check:
                //   1. Empty nodeRuns at the very first poll (the run is
                //      enqueued but NodeRun rows haven't been written yet)
                //      → `[].every()` returns true → polling stopped
                //      immediately, pollingRunId cleared, Cancel button
                //      disappeared mid-run. User saw "Cancel doesn't work"
                //      because the next click hit a stale state.
                //   2. BLOCKED / pending_action statuses (orphan-guard
                //      cascade, downstream-of-failure) never matched
                //      success/failure → loop polled forever, toolbar
                //      stuck showing Cancel after the run had clearly
                //      ended. User saw "should go back to Execute".
                // The schema serializer normalizes completed→success and
                // failed→failure, so the terminal set is {success,
                // failure, blocked}. pending_action stays NON-terminal —
                // it's a real "waiting on user" state and the toolbar
                // should still show Cancel.
                const TERMINAL_NODE_STATUSES = new Set(['success', 'failure', 'blocked']);
                const isFinished =
                    nodeRuns.length > 0 &&
                    nodeRuns.every(run => TERMINAL_NODE_STATUSES.has(run.status));
                if (isFinished) {
                    const runId = pollingRunId;
                    setPollingRunId(null);

                    // Fetch output folders to show user where results are
                    fetchOutputFolders(runId);
                }
        },
        { baseMs: 2000, enabled: !!(pollingRunId && accessToken) },
        [pollingRunId, accessToken]
    );

    /**
     * Reset ALL run/execution state back to idle.
     *
     * The critical bug this fixes: `pollingRunId` (which drives the toolbar's
     * Execute↔Cancel button via `activeRunId`) and `activeRunInfo` were never
     * cleared when the loaded workflow changed. So after running workflow A and
     * then hitting New or loading workflow B, the button stayed stuck on
     * "Cancel", a new run couldn't be started, and the still-live poll (keyed
     * on `pollingRunId`) kept writing run A's node statuses onto whatever graph
     * was now on the canvas.
     *
     * Clearing `pollingRunId` also tears down the polling interval — the effect
     * above is keyed on it and its cleanup runs `clearInterval`. Call this from
     * every path that swaps or clears the loaded graph (New, load-a-different-
     * version, wizard-apply, deleting the current workflow).
     */
    const resetRunState = useCallback(() => {
        // Full reset: deactivate AND unbind the canvas from any run (the shim's
        // null only deactivates; a reset should clear the selected run entirely).
        setSelectedRunActive(false);
        setSelectedRunId(null);
        setActiveRunInfo(null);
        setPendingExecuteParams(null);
        setIsExecuting(false);
        setConfirmCancelRestartDialogOpen(false);
        // CAT-371: reset the cancel guard so the poller can resume for the new run
        cancelRequestedRef.current = false;
        pollCountRef.current = 0;
        setSaveBeforeRunOpen(false);
        // GG.8 — every "load new content onto the canvas" path calls this
        // first; clearing it here means the sealed app-form only shows for
        // the specific selection that set it (handleWorkflowSelection's
        // "sealed" 403 branch, which sets it again right after).
        setSealedWorkflowId(null);
        _skipUnsavedCheck.current = false;
    }, []);

    const handleSaveInitiation = (mode: SaveDialogMode) => {
        if (nodes.length === 0) {
            toast({ title: "Nothing to save", description: "Add some nodes to your workflow first", variant: "destructive" });
            return;
        }
        setSaveDialogMode(mode);
        setNewWorkflowName(mode === "saveAs" ? `${workflowName} Copy` : "");
        setSaveDialogOpen(true);
    };

    // Strip transient, per-run canvas state this session stamps onto the graph
    // so it is never persisted into a saved graph_json version. This state is
    // derived per-run at view time (fan-out badge counts on edges; the pause
    // badge on nodes) and is NOT part of the workflow definition — the backend
    // ignores it (pause points live in WorkflowRun.paused_nodes, fan counts are
    // computed from the task queue). Both self-correct on load, but they should
    // not bloat or dirty the saved version.
    const _edgesForSave = (): Edge[] => edges.map((e) => {
        const d = e.data as Record<string, unknown> | undefined;
        if (!d || (d.fanCount === undefined && d.fanSourceId === undefined)) return e;
        const { fanCount: _fc, fanSourceId: _fs, ...rest } = d;
        return { ...e, data: rest };
    });
    const _nodesForSave = (ns: Node<NodeData>[]): Node<NodeData>[] => ns.map((n) => {
        const d = n.data as unknown as Record<string, unknown> | undefined;
        if (!d || d.paused === undefined) return n;
        const { paused: _p, ...rest } = d;
        return { ...n, data: rest as unknown as NodeData };
    });

    // Inner version-save: dispatches the version POST against the
    // current workflow. Accepts an optional `nodesOverride` so the
    // "Save a clean copy" path (issue #21) can save a stripped graph
    // without mutating the canvas state. Wrapped by `handleSaveVersion`
    // below which adds the sanitization prompt.
    const _doSaveVersion = async (nodesOverride?: Node<NodeData>[]): Promise<void> => {
        if (!currentWorkflow || !accessToken) {
            toast({ title: "Error", description: "No workflow loaded to create a new version for.", variant: "destructive" });
            return;
        }
        const graphNodes = nodesOverride ?? nodes;
        try {
            const response = await authenticatedFetch(`${API_URL}/road/${currentWorkflow.id}/versions`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify({ graph_json: { nodes: _nodesForSave(graphNodes), edges: _edgesForSave() } }),
            });
            if (!response.ok) {
                // FORTRESS 5.4 — check for compliance-policy 422 before
                // falling through to the generic error.
                if (response.status === 422) {
                    try {
                        const body = await response.json();
                        const violations = parseComplianceViolationError(response.status, body);
                        if (violations) {
                            setPendingComplianceViolations(violations);
                            setComplianceViolationsOpen(true);
                            return;
                        }
                    } catch { /* not JSON — fall through */ }
                }
                throw new Error("Failed to create new version.");
            }
            const newVersion: WorkflowVersion = await response.json();
            setCurrentVersionNumber(newVersion.version_number);
            setCurrentVersionId(newVersion.id);
            setVersions((prevVersions) => [...prevVersions, newVersion]);
            _isDirty.current = false;
            toast({ title: "Version Saved", description: `Successfully saved v${newVersion.version_number} of "${workflowName}".` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error saving version:", error);
                toast({ title: "Save Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    // Inner save-as: creates a new workflow entity AND its first
    // version. Accepts overrides so "Save a clean copy" can target a
    // different name + a stripped node set.
    const _doSaveConfirm = async (
        nameOverride?: string,
        nodesOverride?: Node<NodeData>[],
    ): Promise<void> => {
        const name = nameOverride ?? newWorkflowName;
        if (!accessToken || !name) {
            toast({ title: "Input Error", description: "Workflow name cannot be empty.", variant: "destructive" });
            return;
        }

        const existingWorkflow = workflows.find((wf) => wf.name === name);
        if (existingWorkflow) {
            toast({ title: "Name Conflict", description: "A workflow with this name already exists.", variant: "destructive" });
            return;
        }

        const graphNodes = nodesOverride ?? nodes;

        try {
            const createWorkflowResponse = await authenticatedFetch(`${API_URL}/road/`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify({ name }),
            });
            if (!createWorkflowResponse.ok) throw new Error("Failed to create new workflow entity.");
            const newWorkflow: WorkflowSummary = await createWorkflowResponse.json();

            const createVersionResponse = await authenticatedFetch(`${API_URL}/road/${newWorkflow.id}/versions`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify({ graph_json: { nodes: _nodesForSave(graphNodes), edges: _edgesForSave() } }),
            });
            if (!createVersionResponse.ok) throw new Error("Failed to create the first version.");
            const firstVersion: WorkflowVersion = await createVersionResponse.json();

            // Don't update canvas state when saving a clean copy — the
            // user wanted a separate workflow, not to navigate away
            // from their current one. Only swap currentWorkflow on the
            // regular saveAs path.
            if (nameOverride === undefined) {
                setCurrentWorkflow(newWorkflow);
                setWorkflowName(newWorkflow.name);
                setCurrentVersionNumber(firstVersion.version_number);
                setCurrentVersionId(firstVersion.id);
                setVersions([firstVersion]);
                // BUG-1027t: the canvas graph is now exactly what the server
                // stored, so the save clears the dirty flag — same as the
                // version-save path. The clean-copy branch (nameOverride set)
                // deliberately does NOT: the canvas still holds the user-input
                // graph that was saved only in stripped form under another name.
                _isDirty.current = false;
            }
            // Refresh the workflows list so the new entity appears in
            // the load dialog regardless of which path created it.
            setWorkflows((prev) => [...prev, newWorkflow]);
            toast({ title: "Workflow Saved", description: `Successfully saved new workflow "${newWorkflow.name}".` });
            setSaveDialogOpen(false);
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error saving workflow:", error);
                toast({ title: "Save Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    // Sanitization prompt logic (issue #21). Returns whether to show
    // the dialog and the affected-node count. Suppression flag (set on
    // "Save as-is") gates the prompt UNTIL the user-input snapshot
    // changes — adding a new user-input field re-triggers the prompt.
    const _shouldPromptSanitization = (): { needs: boolean; count: number } => {
        if (!nodesHaveUserInput(nodes)) return { needs: false, count: 0 };
        if (currentWorkflow?.id != null) {
            const suppress = getSanitizationSuppressed(currentWorkflow.id);
            if (suppress && suppress.snapshot === userInputSnapshot(nodes)) {
                return { needs: false, count: 0 };
            }
        }
        return { needs: true, count: countNodesWithUserInput(nodes) };
    };

    const handleSaveVersion = async () => {
        // A workflow that has never been saved has no id to hang a version on,
        // so "Save" means "save it for the first time". Rerouting here rather
        // than at each call site: the toolbar button already ternaried its way
        // to Save As, but the Save-version menu item and any future caller did
        // not, and the failure mode was a dead click — `_doSaveVersion` bailing
        // on its own guard with an error toast about a workflow the person was
        // in the middle of creating.
        if (!currentWorkflow) {
            handleSaveInitiation('saveAs');
            return;
        }
        const { needs, count } = _shouldPromptSanitization();
        if (needs) {
            setSanitizationAffectedCount(count);
            pendingSanitizationModeRef.current = "version";
            setSanitizationDialogOpen(true);
            return;
        }
        await _doSaveVersion();
    };

    const handleSaveConfirm = async () => {
        // Save As always prompts when user input is present — the new
        // workflow has no id yet so per-workflow suppression doesn't
        // apply. (Suppression flag tracks per existing workflow id.)
        if (nodesHaveUserInput(nodes)) {
            setSanitizationAffectedCount(countNodesWithUserInput(nodes));
            pendingSanitizationModeRef.current = "saveAs";
            setSanitizationDialogOpen(true);
            return;
        }
        await _doSaveConfirm();
    };

    const confirmSaveAsIs = async () => {
        const mode = pendingSanitizationModeRef.current;
        pendingSanitizationModeRef.current = null;
        setSanitizationDialogOpen(false);
        // Stamp suppression for the current workflow on version saves so
        // subsequent saves don't re-prompt until the user-input snapshot
        // changes. saveAs creates a new workflow, so per-id suppression
        // doesn't apply here (the prompt fires once for that fresh id).
        if (mode === "version" && currentWorkflow?.id != null) {
            setSanitizationSuppressed(currentWorkflow.id, userInputSnapshot(nodes));
            await _doSaveVersion();
        } else if (mode === "saveAs") {
            await _doSaveConfirm();
        }
    };

    const confirmSaveCleanCopy = async () => {
        const mode = pendingSanitizationModeRef.current;
        pendingSanitizationModeRef.current = null;
        setSanitizationDialogOpen(false);
        const cleanedNodes = stripUserInputFromNodes(nodes);
        // Always saves as a NEW workflow, regardless of how the prompt
        // was triggered. The user explicitly opted for a separate clean
        // copy — we don't want to overwrite the user-input version OR
        // create a clean version that the canvas then navigates to.
        const baseName = mode === "saveAs" ? newWorkflowName : workflowName;
        const cleanName = `${baseName} (clean)`;
        await _doSaveConfirm(cleanName, cleanedNodes);
    };

    const cancelSanitization = () => {
        pendingSanitizationModeRef.current = null;
        setSanitizationDialogOpen(false);
    };

    // Fetch the workflow list into state. When `includeArchived` is true the
    // request passes include_archived=true (which returns active AND archived)
    // and we keep ONLY the archived rows so the archived view shows exactly the
    // restorable set. Returns whether the fetch succeeded so callers decide
    // whether to open/flip the dialog.
    const _fetchWorkflowList = async (includeArchived: boolean, search?: string, seq?: number): Promise<boolean> => {
        if (!accessToken) {
            toast({ title: "Authentication Error", description: "Access token not found.", variant: "destructive" });
            return false;
        }
        try {
            const term = (search ?? "").trim();
            const searchQs = term ? `&search=${encodeURIComponent(term)}` : "";
            const qs = `?limit=50&offset=0&sort_by=name&order=asc${includeArchived ? "&include_archived=true" : ""}${searchQs}`;
            const response = await authenticatedFetch(`${API_URL}/road/${qs}`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });
            if (!response.ok) throw new Error(`Failed to fetch workflows: ${response.status} ${await response.text()}`);
            const data: WorkflowSummary[] = await response.json();
            // A newer keystroke landed while this request was in flight — its
            // fetch owns the list, so don't let this stale one clobber it.
            if (seq !== undefined && seq !== _searchSeq.current) return true;
            setWorkflows(includeArchived ? data.filter((w) => w.archived_at) : data);
            return true;
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error loading workflows:", error);
                toast({ title: "Error Loading Workflows", description: "Could not fetch workflows from the server.", variant: "destructive" });
            }
            return false;
        }
    };

    const handleLoadWorkflowList = async () => {
        setArchivedView(false);
        setWorkflowSearchTerm("");
        const ok = await _fetchWorkflowList(false);
        if (ok) {
            setDialogView("workflows");
            setLoadDialogOpen(true);
        }
    };

    // 2400UI127 — the list endpoint caps at 50 rows sorted by name, so a tenant
    // with more workflows could never reach rows past the 50th. `search` is an
    // ilike name-substring filter (workflow_cud.py:552) applied BEFORE the limit,
    // so the Load dialog's search box narrows the window server-side. Keystrokes
    // are debounced: each call waits WORKFLOW_SEARCH_DEBOUNCE_MS and only the
    // newest one still matches the sequence counter, so typing "abc" fires ONE
    // fetch (search=abc) instead of three.
    const [workflowSearchTerm, setWorkflowSearchTerm] = useState<string>("");
    const _searchSeq = React.useRef(0);

    const searchWorkflows = async (term: string): Promise<boolean> => {
        const trimmed = (term ?? "").trim();
        setWorkflowSearchTerm(trimmed);
        const seq = _searchSeq.current + 1;
        _searchSeq.current = seq;
        await new Promise<void>((resolve) => {
            setTimeout(resolve, WORKFLOW_SEARCH_DEBOUNCE_MS);
        });
        // A newer keystroke already owns the next fetch — stay quiet.
        if (seq !== _searchSeq.current) return true;
        return await _fetchWorkflowList(archivedView, trimmed, seq);
    };

    // Drop the term whenever the Load dialog closes or flips between the active
    // and archived lists, so the next open starts unfiltered.
    const clearWorkflowSearch = () => {
        _searchSeq.current += 1;
        setWorkflowSearchTerm("");
    };

    useEffect(() => {
        if (!isLoadDialogOpen && workflowSearchTerm) clearWorkflowSearch();
    }, [isLoadDialogOpen, workflowSearchTerm]);

    // Flip between the active and archived workflow lists inside the open Load
    // dialog. Only commits the view flag once the corresponding fetch succeeds.
    const toggleArchivedView = async () => {
        const next = !archivedView;
        clearWorkflowSearch();
        const ok = await _fetchWorkflowList(next);
        if (ok) {
            setDialogView("workflows");
            setArchivedView(next);
        }
    };

    /** Un-archive a workflow (POST /road/{id}/unarchive) — clears
     *  archived_at so it returns to the active Load list. Called from the
     *  archived view's per-row Restore action; drops the row from the
     *  currently-shown archived list on success. */
    const handleUnarchiveWorkflow = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/${workflow.id}/unarchive`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
            });
            if (!resp.ok) throw new Error(`Failed to restore workflow (${resp.status})`);
            setWorkflows((prev) => prev.filter((w) => w.id !== workflow.id));
            toast({ title: "Restored", description: `"${workflow.name}" has been restored to the active list.` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Restore Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    const handleDeleteWorkflow = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        // Confirmation is handled by the AlertDialog in the Load dialog row.
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/${workflow.id}`, {
                method: "DELETE",
                headers: { Authorization: `Bearer ${accessToken}` },
            });
            if (!resp.ok) throw new Error("Failed to delete workflow");
            setWorkflows((prev) => prev.filter((w) => w.id !== workflow.id));
            if (currentWorkflow?.id === workflow.id) {
                resetRunState();
                setCurrentWorkflow(null);
                setCurrentVersionNumber(null);
                setCurrentVersionId(null);
                setNodes([]);
                setEdges([]);
                resetHistory();
            }
            toast({ title: "Deleted", description: `"${workflow.name}" has been deleted.` });
        } catch (error) {
            toast({ title: "Delete Failed", description: (error as Error).message, variant: "destructive" });
        }
    };

    /** Rename a workflow (PATCH /road/{id}). Updates the Load list and,
     *  if the renamed workflow is the one on the canvas, the toolbar pill. */
    const handleRenameWorkflow = async (workflow: WorkflowSummary, newName: string) => {
        if (!accessToken) return;
        const trimmed = newName.trim();
        if (!trimmed || trimmed === workflow.name) return;
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/${workflow.id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify({ name: trimmed }),
            });
            if (!resp.ok) throw new Error(`Failed to rename workflow (${resp.status})`);
            const updated: WorkflowSummary = await resp.json();
            setWorkflows((prev) => prev.map((w) => (w.id === workflow.id ? { ...w, name: updated.name } : w)));
            if (currentWorkflow?.id === workflow.id) {
                setCurrentWorkflow((prev) => (prev ? { ...prev, name: updated.name } : prev));
                setWorkflowName(updated.name);
            }
            toast({ title: "Renamed", description: `Workflow renamed to "${updated.name}".` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Rename Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    /** Duplicate a workflow (POST /road/{id}/duplicate). Creates a copy
     *  (name + " (copy)") owned by the caller and adds it to the Load list. */
    const handleDuplicateWorkflow = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/${workflow.id}/duplicate`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify({}),
            });
            if (!resp.ok) throw new Error(`Failed to duplicate workflow (${resp.status})`);
            const copy: WorkflowSummary = await resp.json();
            setWorkflows((prev) => (prev.some((w) => w.id === copy.id) ? prev : [...prev, copy]));
            toast({ title: "Duplicated", description: `Created "${copy.name}".` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Duplicate Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    /** Archive a workflow (POST /road/{id}/archive) — soft-hide, reversible.
     *  Drops it from the (default) Load list; if it's the one on the canvas, the
     *  canvas is cleared and run state reset (same as delete). */
    const handleArchiveWorkflow = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/${workflow.id}/archive`, {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
            });
            if (!resp.ok) throw new Error(`Failed to archive workflow (${resp.status})`);
            setWorkflows((prev) => prev.filter((w) => w.id !== workflow.id));
            if (currentWorkflow?.id === workflow.id) {
                resetRunState();
                setCurrentWorkflow(null);
                setWorkflowName("");
                setCurrentVersionNumber(null);
                setCurrentVersionId(null);
                setNodes([]);
                setEdges([]);
                resetHistory();
            }
            toast({ title: "Archived", description: `"${workflow.name}" has been archived.` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Archive Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    /** Delete a SINGLE workflow version (DELETE /road/workflow-versions/{id}).
     *  The backend refuses (409) the only/last version and versions that have run
     *  history; those messages are surfaced to the user. On success the versions
     *  list for the currently-open workflow is refreshed. */
    const handleDeleteVersion = async (version: WorkflowVersion) => {
        if (!accessToken) return;
        try {
            const resp = await authenticatedFetch(`${API_URL}/road/workflow-versions/${version.id}`, {
                method: "DELETE",
                headers: { Authorization: `Bearer ${accessToken}` },
            });
            if (resp.status === 409) {
                const body = await resp.json().catch(() => ({} as any));
                toast({
                    title: "Can't delete version",
                    description: (body as any)?.detail || "This version cannot be deleted.",
                    variant: "destructive",
                });
                return;
            }
            if (!resp.ok && resp.status !== 204) throw new Error(`Failed to delete version (${resp.status})`);

            // Refresh the versions list for the open workflow so the row disappears.
            let remaining = versions.filter((v) => v.id !== version.id);
            if (currentWorkflow) {
                try {
                    const listResp = await authenticatedFetch(
                        `${API_URL}/road/${currentWorkflow.id}/versions?limit=50&offset=0`,
                        { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
                    );
                    if (listResp.ok) remaining = await listResp.json();
                } catch { /* fall back to the locally-filtered list */ }
            }
            setVersions(remaining);
            // If the deleted version was the one loaded on the canvas, drop the
            // stale version pointer (the graph stays for editing/re-saving).
            if (currentVersionId === version.id) {
                setCurrentVersionId(null);
                setCurrentVersionNumber(null);
            }
            toast({ title: "Version deleted", description: `Version ${version.version_number} was deleted.` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Delete Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    };

    const handleWorkflowSelection = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        try {
            const response = await authenticatedFetch(`${API_URL}/road/${workflow.id}/versions?limit=50&offset=0`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });
            if (response.status === 403) {
                const body = await response.json().catch(() => ({} as any));
                const d = (body as any)?.detail;
                if (d && d.code === "workflow_access_denied") {
                    // Clicking a workflow you can't use opens the "request access"
                    // dialog instead of silently failing.
                    setAccessDenied({ workflowId: workflow.id, workflowName: d.workflow_name || workflow.name });
                    return;
                }
                if (d === "sealed") {
                    // GG.8 — a SEALED installed copy: the graph is hidden, but the
                    // buyer can still run it. Show the app form instead of the
                    // canvas/version picker.
                    resetRunState();
                    setNodes([]);
                    setEdges([]);
                    resetHistory();
                    setCurrentWorkflow(workflow);
                    setWorkflowName(workflow.name);
                    setCurrentVersionNumber(null);
                    setCurrentVersionId(null);
                    setVersions([]);
                    setSealedWorkflowId(workflow.id);
                    setLoadDialogOpen(false);
                    _isDirty.current = false;
                    return;
                }
            }
            if (!response.ok) throw new Error("Failed to fetch workflow versions.");
            const data: WorkflowVersion[] = await response.json();
            setSealedWorkflowId(null);
            setVersions(data);
            setCurrentWorkflow(workflow);
            setDialogView("versions");
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error loading workflow versions:", error);
                toast({ title: "Error Loading Versions", description: "Could not fetch versions for the selected workflow.", variant: "destructive" });
            }
        }
    };

    /** Deep-link / bought-workflow open (`?workflow=<id>`, Library "Open
     *  workflow"). Unlike `handleWorkflowSelection` — which assumes the Load
     *  dialog is already open — this loads the workflow's LATEST version
     *  straight onto the canvas: name set, dirty flag cleared, no dialog.
     *  Reuses `handleVersionSelection` for the graph load. A 403 with code
     *  `workflow_access_denied` raises the request-access banner (same as the
     *  list flow); a sealed installed copy (403 "sealed") shows the app form.
     *  A workflow with zero versions opens empty, name set. */
    const openWorkflowById = async (workflow: WorkflowSummary) => {
        if (!accessToken) return;
        try {
            const response = await authenticatedFetch(`${API_URL}/road/${workflow.id}/versions?limit=50&offset=0`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });
            if (response.status === 403) {
                const body = await response.json().catch(() => ({} as any));
                const d = (body as any)?.detail;
                if (d && d.code === "workflow_access_denied") {
                    setAccessDenied({ workflowId: workflow.id, workflowName: d.workflow_name || workflow.name });
                    return;
                }
                if (d === "sealed") {
                    resetRunState();
                    setNodes([]);
                    setEdges([]);
                    resetHistory();
                    setCurrentWorkflow(workflow);
                    setWorkflowName(workflow.name);
                    setCurrentVersionNumber(null);
                    setCurrentVersionId(null);
                    setVersions([]);
                    setSealedWorkflowId(workflow.id);
                    setLoadDialogOpen(false);
                    _isDirty.current = false;
                    return;
                }
            }
            if (!response.ok) throw new Error(`Failed to fetch workflow versions (${response.status}).`);
            const data: WorkflowVersion[] = await response.json();
            setSealedWorkflowId(null);
            setVersions(data);
            setCurrentWorkflow(workflow);
            if (data.length === 0) {
                // No versions yet: open the (empty) workflow with its name set.
                resetRunState();
                setNodes([]);
                setEdges([]);
                resetHistory();
                setWorkflowName(workflow.name);
                setCurrentVersionNumber(null);
                setCurrentVersionId(null);
                setLoadDialogOpen(false);
                _isDirty.current = false;
                return;
            }
            // API orders versions by version_number desc — data[0] is latest.
            await handleVersionSelection(data[0], workflow);
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error opening workflow by id:", error);
                toast({ title: "Error Opening Workflow", description: "Could not open the requested workflow.", variant: "destructive" });
            }
        }
    };

    /** LDF-3 §2.1 — Start a fresh workflow without reloading the page.
     *
     * Resets every piece of canvas state to the same shape a fresh
     * mount has, and strips the workflow ID from the URL so the user
     * isn't accidentally still on /road/<id>. Caller is
     * responsible for the unsaved-changes confirm dialog. */
    const startNewWorkflow = (): void => {
        // Tear down any active run first: without this the Execute button stays
        // stuck on "Cancel" and the previous run's poll keeps writing onto the
        // fresh canvas.
        resetRunState();
        setNodes([]);
        setEdges([]);
        resetHistory();
        setCurrentWorkflow(null);
        setWorkflowName("");
        setCurrentVersionNumber(null);
        setCurrentVersionId(null);
        setVersions([]);
        setSealedWorkflowId(null);
        _isDirty.current = false;
        try {
            if (typeof window !== "undefined" && window.history?.replaceState) {
                const url = new URL(window.location.href);
                if (url.searchParams.has("workflow_id")) {
                    url.searchParams.delete("workflow_id");
                    window.history.replaceState({}, "", url.toString());
                }
            }
        } catch {
            // history rewrite is best-effort; never let it block the reset
        }
        toast({ title: "New workflow", description: "Canvas cleared. Build something." });
    };

    /** Apply-to-canvas handoff for the Workflow Wizard (LDF-3 §1).
     *
     * The wizard's `/support/workflow-wizard/apply` endpoint creates a
     * new workflow + version on the server; this helper fetches both,
     * primes the canvas state to match a fresh Load, and selects every
     * pasted node so the user can immediately move them as a group.
     * The wizard panel itself closes via its own onClose() right after
     * calling this. */
    const handleWizardApplied = async (
        workflowId: number,
        versionId: number,
        appliedName: string,
    ): Promise<void> => {
        if (!accessToken) return;
        try {
            const wfResp = await authenticatedFetch(
                `${API_URL}/road/${workflowId}`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            if (!wfResp.ok) throw new Error(`Failed to load workflow ${workflowId}`);
            const workflow: WorkflowSummary = await wfResp.json();

            const versResp = await authenticatedFetch(
                `${API_URL}/road/${workflowId}/versions?limit=50&offset=0`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            const versionList: WorkflowVersion[] = versResp.ok ? await versResp.json() : [];

            const verResp = await authenticatedFetch(
                `${API_URL}/road/workflow-versions/${versionId}`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            if (!verResp.ok) throw new Error(`Failed to load version ${versionId}`);
            const version: WorkflowVersion = await verResp.json();

            // LDF-3 §1.5 + LDF-4 #2/#3 — use the enriching helper so
            // wizard-emitted nodes get full schema data + positions
            // backfilled, then mark them all selected.
            const { nodes: safeNodes, edges: safeEdges } = await safeLoadGraphWithEnrichment(
                version.graph_json as any, API_URL, accessToken,
            );
            // New workflow onto the canvas — clear any prior run state.
            resetRunState();
            const selectedNodes = safeNodes.map(n => ({ ...n, selected: true }));
            setNodes(selectedNodes);
            setEdges(safeEdges);
            resetHistory();

            setCurrentWorkflow(workflow);
            setWorkflowName(workflow.name || appliedName);
            setCurrentVersionNumber(version.version_number);
            setCurrentVersionId(version.id);
            setVersions(versionList);
            setWorkflows((prev) => prev.some(w => w.id === workflow.id) ? prev : [...prev, workflow]);
            _isDirty.current = false;

            toast({
                title: "Wizard applied",
                description: `Loaded "${workflow.name || appliedName}" onto the canvas.`,
            });
        } catch (e) {
            console.error("handleWizardApplied failed", e);
            toast({
                title: "Apply failed",
                description: (e as Error)?.message || "Could not load the wizard's workflow.",
                variant: "destructive",
            });
        }
    };

    /** LDF-4 #5 — Watch a live workflow run on the canvas.
     *
     * Loads the run's workflow + version onto the canvas and pins the
     * canvas poll-overlay (setPollingRunId) to the run id so per-node
     * status updates flow into node.data.status as they happen.
     *
     * Called from the Process queue's Watch button. Honours the
     * unsaved-changes flag with a confirm() so the user doesn't lose
     * work by accident; the existing dialog-based prompts assume a
     * single Save+Run path, which doesn't apply here. */
    const watchRun = async (
        workflowId: number,
        versionId: number,
        runId: number,
    ): Promise<void> => {
        if (!accessToken) return;

        if (_isDirty.current) {
            const ok = typeof window !== "undefined"
                ? window.confirm(
                    "You have unsaved changes on the current canvas. " +
                    "Watching another run will replace what's on the canvas. " +
                    "Continue?",
                )
                : true;
            if (!ok) return;
        }

        try {
            const wfResp = await authenticatedFetch(
                `${API_URL}/road/${workflowId}`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            if (wfResp.status === 403) {
                const body = await wfResp.json().catch(() => ({} as any));
                const d = (body as any)?.detail;
                if (d && d.code === "workflow_access_denied") {
                    // Not a silent no-op: surface a "request access" dialog.
                    setAccessDenied({ workflowId, workflowName: d.workflow_name || "this workflow" });
                    return;
                }
            }
            if (!wfResp.ok) throw new Error(`Failed to load workflow ${workflowId}`);
            const workflow: WorkflowSummary = await wfResp.json();

            const versResp = await authenticatedFetch(
                `${API_URL}/road/${workflowId}/versions?limit=50&offset=0`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            const versionList: WorkflowVersion[] = versResp.ok ? await versResp.json() : [];

            const verResp = await authenticatedFetch(
                `${API_URL}/road/workflow-versions/${versionId}`,
                { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } },
            );
            if (!verResp.ok) throw new Error(`Failed to load version ${versionId}`);
            const version: WorkflowVersion = await verResp.json();

            const { nodes: safeNodes, edges: safeEdges } = await safeLoadGraphWithEnrichment(
                version.graph_json as any, API_URL, accessToken,
            );
            // Clear any prior run state before pinning this watch's run id —
            // otherwise stale activeRunInfo from an earlier run lingers.
            resetRunState();
            setNodes(safeNodes);
            setEdges(safeEdges);
            resetHistory();

            setCurrentWorkflow(workflow);
            setWorkflowName(workflow.name);
            setCurrentVersionNumber(version.version_number);
            setCurrentVersionId(version.id);
            setVersions(versionList);
            setWorkflows((prev) => prev.some(w => w.id === workflow.id) ? prev : [...prev, workflow]);
            _isDirty.current = false;

            setPollingRunId(runId);

            toast({
                title: "Watching run",
                description: `Live progress for run #${runId} will appear on the canvas.`,
            });
        } catch (e) {
            console.error("watchRun failed", e);
            toast({
                title: "Couldn't watch run",
                description: (e as Error)?.message || "Failed to load the run's workflow.",
                variant: "destructive",
            });
        }
    };

    const handleVersionSelection = async (version: WorkflowVersion, workflowOverride?: WorkflowSummary) => {
        // `workflowOverride` lets a caller (e.g. the deep-link open handler)
        // pin the owning workflow without relying on `currentWorkflow` state,
        // which may not have flushed yet in the same tick.
        const workflow = workflowOverride ?? currentWorkflow;
        // LDF-4 #2/#3 — fix Tim's "Generate Albedo Map" regression.
        // Wizard-saved graphs only contained {label, nodeDefinitionId}
        // in node.data — missing input_schema / output_schema /
        // process_type / cost_in_credits etc. Without those, the
        // input/process/output Node components can't render <Handle>
        // children, so edges have nothing to connect to.
        // safeLoadGraphWithEnrichment fetches the live NodeDefinition
        // catalog and merges the missing fields (saved values take
        // precedence over def fields).
        // Swapping the loaded graph — reset run state so a run that belongs to
        // the previously-loaded workflow doesn't keep the Execute button stuck
        // on "Cancel" (and its poll writing onto this new graph).
        resetRunState();
        const { nodes: safeNodes, edges: safeEdges } = await safeLoadGraphWithEnrichment(
            version.graph_json as any, API_URL, accessToken,
        );
        setNodes(safeNodes);
        setEdges(safeEdges);
        resetHistory();
        if (workflow) {
            setWorkflowName(workflow.name);
            setCurrentVersionNumber(version.version_number);
            setCurrentVersionId(version.id);
        }
        setLoadDialogOpen(false);
        _isDirty.current = false;
        toast({ title: "Workflow Version Loaded", description: `Version ${version.version_number} has been loaded.` });

        // Restore previews from latest workflow run (if any)
        if (workflow && accessToken) {
            try {
                const runsResp = await fetch(
                    // include_details=true: this path restores per-node statuses from
                    // the latest run, so it needs node_runs (the default list is now
                    // lightweight for the hot RunRail poll).
                    `${API_URL}/road/${workflow.id}/runs?limit=1&order=desc&include_details=true`,
                    { headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" } }
                );
                if (runsResp.ok) {
                    const runs = await runsResp.json();
                    if (runs.length > 0) {
                        const latestRun = runs[0];
                        (window as any).__lastWorkflowRunId = latestRun.id;
                        // Restore node statuses from the run
                        if (latestRun.node_runs?.length > 0) {
                            setNodes(nds => nds.map(n => {
                                const nr = latestRun.node_runs.find(
                                    (r: any) => r.node_id === n.id
                                );
                                if (nr) {
                                    return { ...n, data: { ...n.data, status: nr.status, started_at: nr.started_at } };
                                }
                                return n;
                            }));
                        }
                        // Fetch fresh presigned preview URLs
                        fetchNodePreviews(latestRun.id);
                    }
                }
            } catch {
                // Silent — preview restoration is best-effort
            }
        }
    };

    const executeWorkflow = async () => {
        setIsExecuting(true);

        // Never-saved workflow: it has no id/version to run against, so
        // instead of a silent no-op prompt the user to save (name) it
        // first. Confirming opens the Save dialog.
        if (!_skipUnsavedCheck.current && !currentWorkflow) {
            if (nodes.length === 0) {
                toast({ title: "Nothing to run", description: "Add some nodes and save your workflow before running.", variant: "destructive" });
                setIsExecuting(false);
                return;
            }
            setSaveRequiredBeforeRunOpen(true);
            setIsExecuting(false);
            return;
        }

        // Check for unsaved changes — prompt user to save first
        if (!_skipUnsavedCheck.current && currentWorkflow && accessToken && _isDirty.current) {
            setSaveBeforeRunOpen(true);
            setIsExecuting(false);
            return;
        }
        _skipUnsavedCheck.current = false;

        // #102/#104: Check for usage warnings before execution.
        // 2400UI24 — /tenant/warnings is PAID-only: CORE/API/main.py keeps its
        // mount inside _CLOUD_PAID_ROUTER_MOUNTS and open-core/manifest.yaml
        // denies CORE/API/routers/tenant/*.py, so on CE the route is absent and
        // the fetch 404s on every Execute (the e2e net-guard flags any /v1/ 404).
        // 2400UI24b — fail-CLOSED here (unlike the UI gates): unknown
        // (loading/unresolved/errored) edition must NOT fetch it either, since
        // on CE the route is absent and the 404 trips the e2e net-guard. Only a
        // POSITIVELY resolved paid edition requests the pre-check.
        if (isPaidEdition) try {
            const warningResp = await fetch(`${API_URL}/tenant/warnings`, {
                headers: { 'Authorization': `Bearer ${accessToken}` }
            });
            if (warningResp.ok) {
                const { warnings } = await warningResp.json();
                const errors = warnings?.filter((w: any) => w.level === 'error') || [];
                if (errors.length > 0) {
                    toast({ title: "Cannot Execute", description: errors[0].message, variant: "destructive" });
                    setIsExecuting(false);
                    return;
                }
                const warns = warnings?.filter((w: any) => w.level === 'warning') || [];
                if (warns.length > 0) {
                    toast({ title: "Warning", description: warns[0].message });
                }
            }
        } catch { /* proceed if warning check fails */ }

        // Immediately set all nodes to "queued" status (purple color)
        setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: 'queued' } })));

        if (!accessToken || !currentWorkflow || currentVersionNumber === null) {
            toast({ title: "Execution Error", description: "No workflow version is loaded.", variant: "destructive" });
            setIsExecuting(false);
            return;
        }

        const inputNodes = nodes.filter(node => node.type === 'input');

        if (inputNodes.length === 0) {
            toast({ title: "Execution Error", description: "No input nodes found in workflow.", variant: "destructive" });
            setIsExecuting(false);
            return;
        }

        // Collect all cog_ids from all input nodes
        const allCogIds: number[] = [];
        const allMetadataCogIds: number[] = [];

        // rc20 row58-r2: registry-source input nodes (RAG Input / LoRA Input:
        // CORE/Metadata/system/{RAG,LoRA}Input_INPUT_Node.py:19 configuration_schema
        // extra_variables with a "named_ref"-typed var) select a saved artifact by
        // NAME, not a file — the panel stores that value in node.data.extraVariables
        // under the variable key (components/node-config-panel.tsx:2324 passes
        // extraVariableValues={data.extraVariables || {}}; :2327 merges the field's
        // onChange into data.extraVariables). Such a node is no-file-OK when its
        // named_ref value is non-empty; an empty one still needs the dialog.
        const hasNamedRefSelection = (data: any) => {
            let extraVarDefs = data.configuration_schema?.extra_variables;
            if (!extraVarDefs || typeof extraVarDefs !== 'object') {
                // FLAT CONFIGURATION_SCHEMA (BUG.WB-NODE-VARS-UI): same key rules
                // as the panel (node-config-panel.tsx) and the server
                // (node_config_env_defaults.py:162 flat_env_defaults + :38
                // _IGNORED_KEYS) - a flat-form node whose named_ref variable
                // would otherwise be invisible to this scan.
                const schema = data.configuration_schema;
                const flat: { [key: string]: any } = {};
                if (schema && typeof schema === 'object') {
                    for (const key in schema) {
                        if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key)) continue;
                        if (key === 'EXTRA_VARIABLES' || key === 'TYPE' || key === 'PROPERTIES' ||
                            key === 'REQUIRED' || key === 'DEFAULT' || key === 'ENUM' ||
                            key === 'ITEMS' || key === 'SCHEMA' || key === 'TITLE' ||
                            key === 'DESCRIPTION') continue;
                        const entry = schema[key];
                        if (!entry || typeof entry !== 'object') continue;
                        flat[key] = entry;
                    }
                }
                extraVarDefs = Object.keys(flat).length > 0 ? flat : null;
            }
            if (!extraVarDefs || typeof extraVarDefs !== 'object') return false;
            const values = data.extraVariables || {};
            return Object.keys(extraVarDefs).some((key) => {
                const def = extraVarDefs[key];
                return def && def.type === 'named_ref' &&
                    String(values[key] ?? def.default ?? '').trim().length > 0;
            });
        };

        for (const inputNode of inputNodes) {
            const selectedFiles = inputNode.data.selectedFiles || [];
            const selectedMetadata = inputNode.data.selectedMetadata || [];
            const extraVars = inputNode.data.extraVariables || {};
            const inputType = inputNode.data.input_schema?.input_type;

            // Text/script input nodes don't need files — they have text_content in extraVariables
            const hasTextContent = extraVars.text_content && String(extraVars.text_content).trim().length > 0;
            // rc20 row58: EVERY seeded system input node carries input_schema.input_type
            // "NONE" (CORE/Metadata/system/*_INPUT_Node.py), so input_type alone cannot
            // tell a file-bearing input (Image/Resource/Audio/Video/Mesh/Nerf/Comic) from
            // a text/script one. The existing discriminator for that is node_kind (the
            // stable key the palette/graph already carry: lib/graph-load-fallback.ts:102,
            // nodes/node-stack-overlay.tsx:25) — seeded text_input / script_input.
            const nodeKind = String(inputNode.data.node_kind || '').toLowerCase();
            const isTextOrScriptInput = nodeKind === 'text_input' || nodeKind === 'script_input';
            const isNoFileInput = inputType === 'NONE' &&
                (hasTextContent || isTextOrScriptInput || hasNamedRefSelection(inputNode.data));

            if (selectedFiles.length === 0 && !hasTextContent && !isNoFileInput) {
                setMissingFileNodeName(inputNode.data.label || 'an input node');
                setMissingFileDialogOpen(true);
                setIsExecuting(false);
                return;
            }

            // Add file IDs to the arrays
            selectedFiles.forEach((file: any) => {
                if (file.id && !allCogIds.includes(file.id)) {
                    allCogIds.push(file.id);
                }
            });

            selectedMetadata.forEach((file: any) => {
                if (file.id && !allMetadataCogIds.includes(file.id)) {
                    allMetadataCogIds.push(file.id);
                }
            });
        }

        // Allow execution with no files if any input node has text content
        const isTextOrScriptKind = (data: any) => {
            const kind = String(data?.node_kind || '').toLowerCase();
            return kind === 'text_input' || kind === 'script_input';
        };
        const hasAnyTextContent = inputNodes.some(n =>
            (n.data.extraVariables?.text_content && String(n.data.extraVariables.text_content).trim().length > 0) ||
            (n.data.input_schema?.input_type === 'NONE' && (isTextOrScriptKind(n.data) || hasNamedRefSelection(n.data)))
        );
        if (allCogIds.length === 0 && !hasAnyTextContent) {
            toast({ title: "Execution Error", description: "No files selected in input nodes.", variant: "destructive" });
            setIsExecuting(false);
            return;
        }

        try {
            const latestVersion = versions.find(v => v.version_number === currentVersionNumber);
            if (!latestVersion) {
                throw new Error("Could not find the current version details.");
            }

            const runRequestBody = {
                cog_ids: allCogIds,
                metadata_cog_ids: allMetadataCogIds.length > 0 ? allMetadataCogIds : undefined,
                parent_folder_id: undefined,  // TODO: Get from first file's parent?
                refresh_token: refreshToken || undefined,  // Pass refresh token for secret access during workflow execution
                reuse_cache: !freshRun,  // reuse unchanged node results unless "Fresh run" is on
            };

            const runUrl = `${API_URL}/road/workflow-versions/${latestVersion.id}/run`;

            const runResponse = await authenticatedFetch(runUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify(runRequestBody),
            });

            if (await handleRunBlock(runResponse)) return;
            if (!runResponse.ok) {
                throw new Error(`Workflow execution failed: ${await runResponse.text()}`);
            }

            const runResult = await runResponse.json();

            // Check if this is a duplicate/already-running response
            if (runResult.duplicate) {
                // Reset node statuses back to normal (not queued)
                setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));

                // Store the active run info and pending execution params for the confirmation dialog
                setActiveRunInfo({
                    workflow_run_id: runResult.workflow_run_id,
                    orchestrator_run_id: runResult.orchestrator_run_id,
                    status: runResult.status,
                    started_at: runResult.started_at
                });

                const latestVersion = versions.find(v => v.version_number === currentVersionNumber);
                if (latestVersion) {
                    setPendingExecuteParams({
                        latestVersionId: latestVersion.id,
                        allCogIds,
                        allMetadataCogIds
                    });
                }

                // Show the confirmation dialog
                setConfirmCancelRestartDialogOpen(true);
                return;
            }

            if (runResult.workflow_run_id) {
                setPollingRunId(runResult.workflow_run_id);
                // CAT-371: reset guard for the new run
                cancelRequestedRef.current = false;
                pollCountRef.current = 0;
                // Store run ID globally for output node download functionality
                (window as any).__lastWorkflowRunId = runResult.workflow_run_id;
            }

            toast({
                title: "Workflow Executing",
                description: `Processing ${allCogIds.length} file(s)${allMetadataCogIds.length > 0 ? ` with ${allMetadataCogIds.length} metadata file(s)` : ''}.`
            });

        } catch (error) {
             if ((error as Error).message !== "Session expired") {
                toast({ title: "Execution Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setIsExecuting(false);
        }
    };

    // Airflow-style pause/resume of the SELECTED run. `selectedRunPaused` is kept
    // in sync from the RunRail's run poll (setSelectedRunPaused) so the toolbar
    // shows Pause vs Resume correctly even after switching runs.
    const [selectedRunPaused, setSelectedRunPaused] = useState<boolean>(false);
    const _runControl = async (action: "pause" | "resume") => {
        if (!accessToken || !selectedRunId) return;
        try {
            const r = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${selectedRunId}/${action}`,
                { method: "POST", headers: { Authorization: `Bearer ${accessToken}` } }
            );
            if (!r.ok) {
                const e = await r.json().catch(() => ({}));
                throw new Error(e.detail || `${action} failed`);
            }
            setSelectedRunPaused(action === "pause");
            toast({
                title: action === "pause" ? "Run paused" : "Run resumed",
                description: action === "pause"
                    ? "Nodes in progress finish; no new nodes start until you resume."
                    : "The frontier is re-opened — downstream nodes continue.",
            });
        } catch (err) {
            if ((err as Error).message !== "Session expired") {
                toast({ title: `${action === "pause" ? "Pause" : "Resume"} failed`, description: (err as Error).message, variant: "destructive" });
            }
        }
    };
    const pauseRun = () => _runControl("pause");
    const resumeRun = () => _runControl("resume");

    // Airflow-style "restart from here": start a NEW run (of the selected run's
    // inputs) that REUSES unchanged upstream outputs via the output cache and
    // FORCE re-runs this node + everything downstream (so edited/failed nodes,
    // and even unchanged ones you explicitly restart, re-execute). Downstream is
    // computed by BFS over the canvas edges.
    const restartFromNode = async (graphNodeId: string) => {
        if (!accessToken || !selectedRunId) {
            toast({ title: "Pick a run first", description: "Select the run you want to restart from this node.", variant: "destructive" });
            return;
        }
        const bust = new Set<string>([graphNodeId]);
        let frontier = [graphNodeId];
        while (frontier.length) {
            const next: string[] = [];
            for (const nid of frontier) {
                for (const e of (edges as any[])) {
                    if (e.source === nid && !bust.has(e.target)) { bust.add(e.target); next.push(e.target); }
                }
            }
            frontier = next;
        }
        try {
            const r = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${selectedRunId}/restart`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                    body: JSON.stringify({ resume_mode: "full", reuse_cache: true, cache_bust_nodes: [...bust] }),
                }
            );
            if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || "Restart failed"); }
            const res = await r.json();
            const newId = res?.new_run?.workflow_run_id ?? res?.new_run_id;
            if (newId) {
                setSelectedRunId(newId);
                setSelectedRunActive(true);
                setSelectedRunPaused(false);
                setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: 'queued' } })));
                (window as any).__lastWorkflowRunId = newId;
            }
            toast({ title: "Restarted from node", description: `Reusing unchanged upstream; re-running ${bust.size} node(s) from here.` });
        } catch (err) {
            if ((err as Error).message !== "Session expired") {
                toast({ title: "Restart failed", description: (err as Error).message, variant: "destructive" });
            }
        }
    };

    // Per-node pause (Airflow-style): a node marked as a pause point HOLDS the
    // run when it's reached. pausedNodes tracks the SELECTED run's pause points
    // (synced from the run rail's poll via setPausedNodes); the canvas marks
    // node.data.paused so nodes render a pause badge.
    const [pausedNodes, setPausedNodes] = useState<string[]>([]);
    const _nodePauseCtl = async (action: "pause-node" | "resume-node", graphNodeId: string) => {
        if (!accessToken || !selectedRunId) {
            toast({ title: "Pick a run first", description: "Select the run you want to pause a node on.", variant: "destructive" });
            return;
        }
        // optimistic
        setPausedNodes((prev) => action === "pause-node"
            ? Array.from(new Set([...prev, graphNodeId]))
            : prev.filter((n) => n !== graphNodeId));
        try {
            const r = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${selectedRunId}/${action}`,
                { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                  body: JSON.stringify({ node_id: graphNodeId }) }
            );
            if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || `${action} failed`); }
            const res = await r.json();
            if (Array.isArray(res?.paused_nodes)) setPausedNodes(res.paused_nodes);
            refreshNodeControls().catch(() => {});  // immediate colour update (yellow on pause)
            toast({
                title: action === "pause-node" ? "Pause point set" : "Node resumed",
                description: action === "pause-node"
                    ? "The run will stop and wait when it reaches this node."
                    : "The node continues; downstream resumes.",
            });
        } catch (err) {
            if ((err as Error).message !== "Session expired") {
                toast({ title: "Pause failed", description: (err as Error).message, variant: "destructive" });
            }
        }
    };
    const pauseNode = (graphNodeId: string) => _nodePauseCtl("pause-node", graphNodeId);
    const resumeNode = (graphNodeId: string) => _nodePauseCtl("resume-node", graphNodeId);

    // node-run-controls — Airflow-style in-run node actions. One call for all of
    // skip / force_fail / cancel_downstream_now|drain / rerun_same_data /
    // rerun_with_downstream / clear_cache / delete_outputs. Gated on a selected run.
    const _NODE_ACTION_LABELS: Record<string, string> = {
        skip: "Node skipped (marked success)",
        force_fail: "Node force-failed",
        cancel_downstream_now: "Downstream cancelled",
        cancel_downstream_drain: "Will stop after this node finishes",
        rerun_same_data: "Re-running with the same data",
        rerun_with_downstream: "Re-running this node + downstream",
        clear_cache: "Cache cleared — recomputes on next reuse",
    };
    const nodeAction = async (graphNodeId: string, action: string) => {
        if (!accessToken || !selectedRunId) {
            toast({ title: "Pick a run first", description: "Select the run you want to act on.", variant: "destructive" });
            return;
        }
        try {
            const r = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${selectedRunId}/node-action`,
                { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                  body: JSON.stringify({ node_id: graphNodeId, action }) }
            );
            if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || `${action} failed`); }
            const res = await r.json().catch(() => ({}));
            toast({ title: _NODE_ACTION_LABELS[action] || "Done", description: res?.message || undefined });
            refreshNodeControls().catch(() => {});  // reflect the new state on the canvas immediately
        } catch (err) {
            if ((err as Error).message !== "Session expired") {
                toast({ title: "Action failed", description: (err as Error).message, variant: "destructive" });
            }
        }
    };

    // DAG schedule pause: stop the workflow's schedule/triggers from starting new runs.
    const pauseSchedule = async (paused: boolean) => {
        if (!accessToken || !currentWorkflow?.id) {
            toast({ title: "Open a workflow first", variant: "destructive" });
            return;
        }
        try {
            const r = await authenticatedFetch(
                `${API_URL}/road/${currentWorkflow.id}/schedule-pause`,
                { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                  body: JSON.stringify({ paused }) }
            );
            if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || "schedule-pause failed"); }
            toast({ title: paused ? "Schedule paused" : "Schedule resumed",
                    description: paused ? "No new runs will start until you resume." : "New runs can start again." });
        } catch (err) {
            if ((err as Error).message !== "Session expired") {
                toast({ title: "Schedule pause failed", description: (err as Error).message, variant: "destructive" });
            }
        }
    };

    // Reflect pause points onto the canvas nodes so they can render a badge.
    useEffect(() => {
        const set = new Set(pausedNodes);
        setNodes((nds) => nds.map(n => (
            !!n.data?.paused === set.has(n.id) ? n : { ...n, data: { ...n.data, paused: set.has(n.id) } }
        )));
    }, [pausedNodes, setNodes]);

    // Fan-out visualization: per-node task counts for the SELECTED run. A node
    // whose count > 1 ran as a batch fan-out (one task per input file); its
    // outgoing edges get a "FAN ×N" badge (stamped below). Polls while the run
    // is live so the badge appears as tasks are enqueued; a single fetch for a
    // finished run.
    const [fanCounts, setFanCounts] = useState<Record<string, number>>({});
    const loadFanCounts = useCallback(async () => {
        if (!selectedRunId || !accessToken) return;
        // clean-v2-api: /v1/runs (was /v1/node-runs; old path kept as a
        // back-compat alias — see CORE/API/routers/runs.py).
        const r = await fetch(`${API_URL}/runs/${selectedRunId}/fanout-summary`, {
            headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        });
        const err = pollErrorForResponse(r);
        if (err) throw err;
        const d = await r.json();
        if (d && d.node_task_counts) setFanCounts(d.node_task_counts);
    }, [selectedRunId, accessToken]);
    // Clear when no run is selected; load once when the selected run changes so a
    // FINISHED run still shows its fan counts (the poller below only runs live).
    useEffect(() => {
        if (!selectedRunId || !accessToken) { setFanCounts({}); return; }
        loadFanCounts().catch(() => {});
    }, [selectedRunId, accessToken, loadFanCounts]);
    // Poll while the selected run is live so the FAN badge appears as tasks
    // enqueue — through the shared helper (backoff / stop / pause / single-flight).
    usePollingBackoff(
        loadFanCounts,
        { baseMs: 4000, enabled: !!(selectedRunId && accessToken && selectedRunActive) },
        [selectedRunId, selectedRunActive, accessToken]
    );

    // Stamp fan info onto edges whose SOURCE node fanned (>1 task). Guarded so
    // setEdges runs only when a value actually changes — no render loop even
    // though the effect depends on `edges` (which setEdges mutates).
    useEffect(() => {
        let changed = false;
        const next = edges.map((e) => {
            const c = fanCounts[e.source];
            const fc = c && c > 1 ? c : undefined;
            const cur = e.data?.fanCount;
            if ((cur ?? undefined) === fc) return e;
            changed = true;
            return { ...e, data: { ...e.data, fanCount: fc, fanSourceId: fc ? e.source : undefined } };
        });
        if (changed) setEdges(next);
    }, [fanCounts, edges, setEdges]);

    // node-run-controls — per-node OPERATOR CONTROL state (paused/draining/
    // skipped/re-running) for the selected run. Distinct from execution status;
    // the border helper prioritizes it so these states get their own colour.
    const [nodeControls, setNodeControls] = useState<Record<string, string>>({});
    // Fetch control state NOW — callable right after an action so colours update
    // immediately instead of waiting for the poll (the "nothing happened" fix).
    // Throws on transient failure so the poller backs off; on-demand callers use
    // .catch() (fire-and-forget colour refresh after an action).
    const refreshNodeControls = useCallback(async () => {
        if (!selectedRunId || !accessToken) return;
        const r = await fetch(`${API_URL}/road/workflow-runs/${selectedRunId}/node-controls`, {
            headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        });
        const err = pollErrorForResponse(r);
        if (err) throw err;
        const d = await r.json();
        if (d && d.controls) {
            setNodeControls(d.controls);
            // The controls map is derived from WorkflowRun.paused_nodes, so it is
            // the authoritative pause-point set for the selected run: a node held
            // by a server BREAKPOINT never went through the pause-node POST, so
            // without this sync pausedNodes stays empty and the paused-only menu
            // (Inspect frame) never renders. Only when the field is PRESENT — an
            // absent payload must not clobber the optimistic add in _nodePauseCtl.
            setPausedNodes(Object.keys(d.controls).filter((key) => d.controls[key] === "paused"));
        }
    }, [selectedRunId, accessToken, setPausedNodes]);
    // Clear control state when no run is selected (the poller does an immediate
    // tick on start, so no manual first fetch is needed here).
    useEffect(() => {
        if (!selectedRunId || !accessToken) setNodeControls({});
    }, [selectedRunId, accessToken]);
    // Poll while a run is SELECTED (active or not) — a paused/skipped node on a
    // finished run still needs its colour. 3s live / 6s idle, through the shared
    // helper (backoff / stop / pause / single-flight).
    usePollingBackoff(
        refreshNodeControls,
        { baseMs: selectedRunActive ? 3000 : 6000, enabled: !!(selectedRunId && accessToken) },
        [selectedRunId, selectedRunActive, accessToken]
    );

    // Stamp control state onto canvas nodes (guarded — only when it changes).
    useEffect(() => {
        setNodes((nds) => {
            let changed = false;
            const next = nds.map((n) => {
                const c = nodeControls[n.id];
                if ((n.data?.control ?? undefined) === (c ?? undefined)) return n;
                changed = true;
                return { ...n, data: { ...n.data, control: c } };
            });
            return changed ? next : nds;
        });
    }, [nodeControls, setNodes]);

    // Cancel the currently-polling active run. Surfaced as a one-click
    // button on the toolbar so the user doesn't have to open the queue
    // dialog or trigger a duplicate-run modal just to stop work.
    // No restart — pure cancel.
    const cancelActiveRun = async () => {
        if (!accessToken || !pollingRunId) return;
        const runId = pollingRunId;
        try {
            const cancelResponse = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${runId}/cancel`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                }
            );
            if (!cancelResponse.ok) {
                const errorData = await cancelResponse.json().catch(() => ({}));
                throw new Error(errorData.detail || "Failed to cancel the active workflow");
            }
            toast({
                title: "Workflow Cancelled",
                description: `Run #${runId} stopped.`,
            });
            // Don't stop polling here — the poller at :763 will clear
            // pollingRunId once the run reaches a terminal state (FAILED).
            // The poller's maxFailures=5 bound also stops it on consecutive
            // HTTP errors; if the backend leaves it CANCELLING for too long,
            // the 30-poll guard below (CAT-371) will.
            cancelRequestedRef.current = true;
            // Clear node statuses so the canvas isn't stuck on "queued"
            setNodes((nds) =>
                nds.map((n) => ({ ...n, data: { ...n.data, status: undefined } }))
            );
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({
                    title: "Cancel Failed",
                    description: (error as Error).message,
                    variant: "destructive",
                });
            }
        }
    };

    // Cancel an active workflow and optionally restart it
    const cancelAndRestartWorkflow = async () => {
        if (!accessToken || !activeRunInfo || !pendingExecuteParams) {
            setConfirmCancelRestartDialogOpen(false);
            return;
        }

        setIsExecuting(true);
        setConfirmCancelRestartDialogOpen(false);

        try {
            // Cancel the active workflow using our cancel endpoint (cancels pending ApiTaskQueue tasks + updates DB)
            const cancelResponse = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${activeRunInfo.workflow_run_id}/cancel`,
                {
                    method: 'POST',
                    headers: { Authorization: `Bearer ${accessToken}` }
                }
            );

            if (!cancelResponse.ok) {
                const errorData = await cancelResponse.json().catch(() => ({}));
                throw new Error(errorData.detail || "Failed to cancel the active workflow");
            }

            toast({
                title: "Workflow Cancelled",
                description: "The previous run has been cancelled. Starting new run..."
            });

            // Wait a moment for the cancellation to propagate
            await new Promise(resolve => setTimeout(resolve, 1000));

            // Now start the new workflow
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: 'queued' } })));

            const runRequestBody = {
                cog_ids: pendingExecuteParams.allCogIds,
                metadata_cog_ids: pendingExecuteParams.allMetadataCogIds.length > 0 ? pendingExecuteParams.allMetadataCogIds : undefined,
                parent_folder_id: undefined,
                refresh_token: refreshToken || undefined
            };

            const runUrl = `${API_URL}/road/workflow-versions/${pendingExecuteParams.latestVersionId}/run`;

            const runResponse = await authenticatedFetch(runUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify(runRequestBody),
            });

            if (await handleRunBlock(runResponse)) return;
            if (!runResponse.ok) {
                throw new Error(`Workflow execution failed: ${await runResponse.text()}`);
            }

            const runResult = await runResponse.json();

            if (runResult.workflow_run_id) {
                setPollingRunId(runResult.workflow_run_id);
                // CAT-371: reset guard for the new run
                cancelRequestedRef.current = false;
                pollCountRef.current = 0;
                // Store run ID globally for output node download functionality
                (window as any).__lastWorkflowRunId = runResult.workflow_run_id;
            }

            toast({
                title: "Workflow Executing",
                description: `Processing ${pendingExecuteParams.allCogIds.length} file(s).`
            });

        } catch (error) {
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Execution Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setIsExecuting(false);
            setActiveRunInfo(null);
            setPendingExecuteParams(null);
        }
    };

    // Airflow-like: start ANOTHER run alongside the one already in progress
    // (the "Execute new run" choice). Sends force_new so the API skips the
    // dedup and creates a concurrent run; the current run keeps going.
    const executeNewRunAnyway = async () => {
        if (!accessToken || !pendingExecuteParams) {
            setConfirmCancelRestartDialogOpen(false);
            return;
        }
        setIsExecuting(true);
        setConfirmCancelRestartDialogOpen(false);
        try {
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: 'queued' } })));
            const runRequestBody = {
                cog_ids: pendingExecuteParams.allCogIds,
                metadata_cog_ids: pendingExecuteParams.allMetadataCogIds.length > 0 ? pendingExecuteParams.allMetadataCogIds : undefined,
                parent_folder_id: undefined,
                refresh_token: refreshToken || undefined,
                force_new: true,
                reuse_cache: !freshRun,
            };
            const runUrl = `${API_URL}/road/workflow-versions/${pendingExecuteParams.latestVersionId}/run`;
            const runResponse = await authenticatedFetch(runUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
                body: JSON.stringify(runRequestBody),
            });
            if (await handleRunBlock(runResponse)) return;
            if (!runResponse.ok) {
                throw new Error(`Workflow execution failed: ${await runResponse.text()}`);
            }
            const runResult = await runResponse.json();
            if (runResult.workflow_run_id) {
                setPollingRunId(runResult.workflow_run_id);
                // CAT-371: reset guard for the new run
                cancelRequestedRef.current = false;
                pollCountRef.current = 0;
                (window as any).__lastWorkflowRunId = runResult.workflow_run_id;
            }
            toast({
                title: "New Run Started",
                description: `Processing ${pendingExecuteParams.allCogIds.length} file(s). The previous run keeps running.`
            });
        } catch (error) {
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Execution Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setIsExecuting(false);
            setActiveRunInfo(null);
            setPendingExecuteParams(null);
        }
    };

    // Stop the current run from the "already running" dialog (cancel only, no
    // restart) — the "Stop current run" choice.
    const stopCurrentRunFromDialog = async () => {
        const runId = activeRunInfo?.workflow_run_id;
        setConfirmCancelRestartDialogOpen(false);
        if (!accessToken || !runId) {
            setActiveRunInfo(null);
            setPendingExecuteParams(null);
            return;
        }
        try {
            const cancelResponse = await authenticatedFetch(
                `${API_URL}/road/workflow-runs/${runId}/cancel`,
                { method: 'POST', headers: { Authorization: `Bearer ${accessToken}` } }
            );
            if (!cancelResponse.ok) {
                const errorData = await cancelResponse.json().catch(() => ({}));
                throw new Error(errorData.detail || "Failed to cancel the active workflow");
            }
            toast({ title: "Workflow Cancelled", description: `Run #${runId} stopped.` });
            if (pollingRunId === runId) {
                setPollingRunId(null);
                setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));
            }
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                toast({ title: "Cancel Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setActiveRunInfo(null);
            setPendingExecuteParams(null);
        }
    };

    // Dismiss the cancel/restart dialog without action
    const dismissCancelRestartDialog = () => {
        setConfirmCancelRestartDialogOpen(false);
        setActiveRunInfo(null);
        setPendingExecuteParams(null);
    };

    const fetchNodeDefinitionsForDialog = useCallback(async () => {
        if (!accessToken) {
            toast({ title: "Authentication Error", description: "Access token not found.", variant: "destructive" });
            return;
        }
        try {
            const response = await authenticatedFetch(`${API_URL}/nodes/?limit=100&offset=0`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });
            if (!response.ok) throw new Error("Failed to fetch node definitions.");
            const data: NodeDefinition[] = await response.json();
            setDeletableNodes(data);
        } catch (error) {
             if ((error as Error).message !== "Session expired") {
                console.error("Error fetching node definitions:", error);
                toast({ title: "Error", description: (error as Error).message, variant: "destructive" });
            }
        }
    }, [accessToken, authenticatedFetch]);

    const handleOpenEditDialog = () => {
        fetchNodeDefinitionsForDialog();
        setEditDialogOpen(true);
    };

    const handleDeleteConfirm = async () => {
        if (!nodeToDelete || !accessToken) {
            toast({ title: "Error", description: "No node selected or missing token.", variant: "destructive" });
            setConfirmDeleteDialogOpen(false);
            return;
        }

        try {
            const response = await authenticatedFetch(`${API_URL}/nodes/${nodeToDelete.id}`, {
                method: "DELETE",
                headers: { Authorization: `Bearer ${accessToken}`, accept: "*/*" },
            });

            if (response.status === 204) {
                toast({ title: "Success", description: `Node "${nodeToDelete.name}" has been deleted.` });
                // Fire the re-fetch event instead of bumping a key. A
                // key bump would unmount the sidebar + everything in it
                // (wizards, dialogs, drag-and-drop state).
                window.dispatchEvent(new Event("mod:nodes:changed"));
                fetchNodeDefinitionsForDialog();
            } else {
                throw new Error(`Failed to delete node. Status: ${response.status}`);
            }
        } catch (error) {
             if ((error as Error).message !== "Session expired") {
                console.error("Error deleting node:", error);
                toast({ title: "Deletion Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setConfirmDeleteDialogOpen(false);
            setNodeToDelete(null);
        }
    };

    const handleRefreshConfirm = async () => {
        if (!accessToken) {
            toast({ title: "Authentication Error", description: "Access token not found.", variant: "destructive" });
            return;
        }

        setIsRefreshing(true);
        try {
            const deletePromises = selectRefreshDeletableNodes(deletableNodes).map((node) =>
                authenticatedFetch(`${API_URL}/nodes/${node.id}`, {
                    method: "DELETE",
                    headers: { Authorization: `Bearer ${accessToken}`, accept: "*/*" },
                })
            );
            await Promise.all(deletePromises);

            const refreshResponse = await authenticatedFetch(`${API_URL}/nodes/refresh`, {
                method: "POST",
                headers: { Authorization: `Bearer ${accessToken}`, accept: "application/json" },
            });
            if (!refreshResponse.ok) throw new Error("Failed to refresh node definitions from source.");

            toast({ title: "Success", description: "All node definitions have been refreshed." });
            // Event-based re-fetch, not a remount.
            window.dispatchEvent(new Event("mod:nodes:changed"));
            await fetchNodeDefinitionsForDialog();
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error refreshing nodes:", error);
                toast({ title: "Refresh Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setIsRefreshing(false);
            setConfirmRefreshDialogOpen(false);
        }
    };

    /** Submit a request to use a workflow the user has no access to.
     * Returns true on success (201) so the dialog can show confirmation. */
    const requestWorkflowAccess = async (workflowId: number): Promise<boolean> => {
        if (!accessToken) return false;
        try {
            const resp = await authenticatedFetch(
                `${API_URL}/road/${workflowId}/request-access`,
                {
                    method: "POST",
                    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                },
            );
            if (resp.ok) {
                toast({ title: "Access requested", description: "The workflow owner has been notified. You'll get access once they approve." });
                return true;
            }
            // 400 = already requested / already has access; treat as non-fatal.
            const err = await resp.json().catch(() => ({} as any));
            toast({ title: "Couldn't request access", description: (err as any)?.detail || `HTTP ${resp.status}` });
            return false;
        } catch (e) {
            toast({ title: "Couldn't request access", description: String(e) });
            return false;
        }
    };

    /** If a run response is a structured access-denied 403 (secrets/files)
     * or a purchase-required pre-flight 402 (GG.UI.6b), surface the
     * "blocked run" dialog and return true (handled). */
    const handleRunBlock = async (runResponse: Response): Promise<boolean> => {
        if (runResponse.status !== 402 && runResponse.status !== 403) return false;
        const body = await runResponse.json().catch(() => ({} as any));
        const d = (body as any)?.detail;

        // GG.UI.6b — entitlement pre-flight refusal: 402 with a structured
        // detail whose items name the paid node/workflow the tenant lacks.
        // (Other 402s — e.g. insufficient credits — have no purchase_required
        // code and fall through to the generic failure toast.)
        if (runResponse.status === 402 && d && typeof d === "object" && d.code === "purchase_required") {
            const { mapPreflightItems } = await import("@/lib/blocked-run");
            const reasons = mapPreflightItems(d.items);
            setRunBlocked({
                message: d.message || "This run needs items you don't own yet.",
                secrets: [],
                cogIds: [],
                purchaseReasons: reasons,
            });
            // Clear any "queued" node statuses the run had optimistically set.
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));
            return true;
        }

        if (runResponse.status !== 403) return false;
        if (!d || typeof d !== "object") return false;
        if (d.code === "secret_access_denied" || d.code === "file_access_denied" || d.code === "workflow_access_denied") {
            setRunBlocked({
                message: d.message || "You don't have access to run this workflow.",
                secrets: Array.isArray(d.missing_secrets) ? d.missing_secrets : [],
                cogIds: Array.isArray(d.missing_cog_ids) ? d.missing_cog_ids : [],
            });
            // Clear any "queued" node statuses the run had optimistically set.
            setNodes((nds) => nds.map(n => ({ ...n, data: { ...n.data, status: undefined } })));
            return true;
        }
        return false;
    };

    /** Request access to a secret by NAME (resolve id via /secrets/, then POST). */
    const requestRunSecretAccess = async (secretName: string): Promise<boolean> => {
        if (!accessToken) return false;
        try {
            const listResp = await authenticatedFetch(`${API_URL}/secrets/?limit=200`, {
                headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            });
            const secrets = listResp.ok ? await listResp.json() : [];
            const match = (secrets as any[]).find(s => s.name === secretName);
            if (!match) return false;
            const resp = await authenticatedFetch(`${API_URL}/secrets/${match.id}/request-access`, {
                method: "POST",
                headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
                body: JSON.stringify({}),
            });
            return resp.ok || resp.status === 400;
        } catch { return false; }
    };

    /** Request access to an input file (cog) via the cog permission-request flow. */
    const requestRunFileAccess = async (cogId: number): Promise<boolean> => {
        if (!accessToken) return false;
        try {
            const resp = await authenticatedFetch(`${API_URL}/permission-requests`, {
                method: "POST",
                headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
                body: JSON.stringify({ cog_id: cogId, requested_level: "viewer" }),
            });
            return resp.ok || resp.status === 400;
        } catch { return false; }
    };

    return {
        runBlocked,
        setRunBlocked,
        handleRunBlock,
        requestRunSecretAccess,
        requestRunFileAccess,
        accessDenied,
        setAccessDenied,
        sealedWorkflowId,
        requestWorkflowAccess,
        accessToken,
        currentWorkflow,
        workflowName,
        currentVersionNumber,
        currentVersionId,
        workflows,
        versions,
        isSaveDialogOpen, setSaveDialogOpen,
        saveDialogMode,
        isLoadDialogOpen, setLoadDialogOpen,
        isEditDialogOpen, setEditDialogOpen,
        isConfirmDeleteDialogOpen, setConfirmDeleteDialogOpen,
        isConfirmRefreshDialogOpen, setConfirmRefreshDialogOpen,
        isMissingFileDialogOpen, setMissingFileDialogOpen,
        isExecuting,
        saveRequiredBeforeRunOpen, setSaveRequiredBeforeRunOpen,
        confirmSaveRequiredBeforeRun: () => {
            // Route into the normal save flow. For a never-saved
            // workflow handleSaveVersion opens the "Save As" dialog so
            // the user can name it; after saving they can hit Execute
            // again to run.
            setSaveRequiredBeforeRunOpen(false);
            handleSaveVersion();
        },
        saveBeforeRunOpen, setSaveBeforeRunOpen,
        confirmSaveAndRun: async () => {
            setSaveBeforeRunOpen(false);
            try {
                // Bypass the sanitization prompt here: running the
                // workflow requires the user's inputs, and double-
                // prompting (save dialog already gone, sanitization
                // dialog popping up before the run) would break the
                // "click save+run, walk away" promise. The next
                // explicit Save click will surface the prompt as
                // designed.
                await _doSaveVersion();
                _skipUnsavedCheck.current = true;
                executeWorkflow();
            } catch {
                toast({ title: "Save Failed", description: "Could not save workflow.", variant: "destructive" });
            }
        },
        pollingRunId,
        selectedRunId,
        selectedRunActive,
        selectRun,
        selectedRunPaused,
        setSelectedRunPaused,
        restartFromNode,
        freshRun,
        setFreshRun,
        pausedNodes,
        setPausedNodes,
        pauseNode,
        resumeNode,
        nodeAction,
        pauseSchedule,
        cancelActiveRun,
        isConfirmCancelRestartDialogOpen,
        activeRunInfo,
        cancelAndRestartWorkflow,
        executeNewRunAnyway,
        stopCurrentRunFromDialog,
        dismissCancelRestartDialog,
        newWorkflowName, setNewWorkflowName,
        dialogView, setDialogView,
        deletableNodes,
        nodeToDelete, setNodeToDelete,
        isRefreshing,
        nodeLibraryKey,
        setNodeLibraryKey,
        missingFileNodeName,
        markDirty,
        handleSaveInitiation,
        handleSaveVersion,
        handleSaveConfirm,
        // Save sanitization prompt (issue #21). Consumer mounts
        // <SaveSanitizationDialog /> with these props.
        isSanitizationDialogOpen,
        sanitizationAffectedCount,
        confirmSaveAsIs,
        confirmSaveCleanCopy,
        cancelSanitization,
        // FORTRESS 5.4 — compliance-violation dialog. Consumer mounts
        // <ViolationList /> with these props.
        complianceViolationsOpen, setComplianceViolationsOpen,
        pendingComplianceViolations,
        handleLoadWorkflowList,
        archivedView,
        toggleArchivedView,
        workflowSearchTerm,
        searchWorkflows,
        handleUnarchiveWorkflow,
        handleWorkflowSelection,
        openWorkflowById,
        handleDeleteWorkflow,
        handleRenameWorkflow,
        handleDuplicateWorkflow,
        handleArchiveWorkflow,
        handleDeleteVersion,
        handleVersionSelection,
        handleWizardApplied,
        watchRun,
        startNewWorkflow,
        executeWorkflow,
        handleOpenEditDialog,
        handleDeleteConfirm,
        handleRefreshConfirm,
        handleDeleteInitiation: (node: NodeDefinition) => { setNodeToDelete(node); setConfirmDeleteDialogOpen(true); },
        handleRefreshAllNodes: () => setConfirmRefreshDialogOpen(true),
        // Export/Import
        isImportDialogOpen, setImportDialogOpen,
        importValidation,
        pendingImportData,
        isImporting,
        handleExportWorkflow,
        handleImportFile,
        handleImportConfirm,
        handleImportCancel,
    };

    // Export workflow as ZIP bundle (workflow.json + metadata files)
    async function handleExportWorkflow() {
        if (!accessToken || !currentWorkflow) {
            toast({ title: "Export Error", description: "No workflow loaded to export.", variant: "destructive" });
            return;
        }

        try {
            const response = await authenticatedFetch(`${API_URL}/road/${currentWorkflow.id}/export`, {
                headers: { Authorization: `Bearer ${accessToken}` },
            });

            if (!response.ok) {
                throw new Error("Failed to export workflow");
            }

            // Download as ZIP binary
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            const safeName = (currentWorkflow.name || 'workflow').replace(/[^a-z0-9]/gi, '_');
            link.download = `${safeName}_workflow.zip`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);

            toast({ title: "Export Successful", description: `Workflow "${currentWorkflow.name}" has been exported as a ZIP bundle.` });
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error exporting workflow:", error);
                toast({ title: "Export Failed", description: (error as Error).message, variant: "destructive" });
            }
        }
    }

    // Handle file selection for import (ZIP or legacy JSON)
    async function handleImportFile(event: React.ChangeEvent<HTMLInputElement>) {
        const file = event.target.files?.[0];
        if (!file || !accessToken) return;

        try {
            const isZip = file.name.endsWith('.zip');

            if (isZip) {
                // ZIP import: validate by uploading the file
                const formData = new FormData();
                formData.append('file', file);

                const response = await authenticatedFetch(`${API_URL}/road/validate-import-zip`, {
                    method: "POST",
                    headers: { Authorization: `Bearer ${accessToken}` },
                    body: formData,
                });

                if (!response.ok) {
                    throw new Error("Failed to validate workflow import");
                }

                const validation: WorkflowValidateResult = await response.json();
                setImportValidation(validation);
                // Store the file for later upload on confirm (use pendingImportData to signal ZIP mode)
                (window as any).__pendingImportZipFile = file;
                setPendingImportData({
                    export_version: "2.0",
                    name: file.name,
                    // ZIP-mode placeholder — graph_json is populated server-
                    // side from the ZIP after the user confirms the import.
                    graph_json: { nodes: [], edges: [] },
                    required_nodes: [],
                });
                setImportDialogOpen(true);
            } else {
                // Legacy JSON import
                const text = await file.text();
                const importData: WorkflowExport = JSON.parse(text);

                if (!importData.export_version || !importData.name || !importData.graph_json) {
                    throw new Error("Invalid workflow file format");
                }

                const response = await authenticatedFetch(`${API_URL}/road/validate-import`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                    body: JSON.stringify(importData),
                });

                if (!response.ok) {
                    throw new Error("Failed to validate workflow import");
                }

                const validation: WorkflowValidateResult = await response.json();
                setImportValidation(validation);
                setPendingImportData(importData);
                setImportDialogOpen(true);
            }
        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error reading import file:", error);
                toast({ title: "Import Failed", description: (error as Error).message, variant: "destructive" });
            }
        }

        // Reset the input so the same file can be selected again
        event.target.value = '';
    }

    // Confirm and execute the import
    async function handleImportConfirm() {
        if (!accessToken || !pendingImportData) return;

        setIsImporting(true);
        try {
            const isZipImport = pendingImportData.export_version === "2.0" && (window as any).__pendingImportZipFile;
            let response: Response;

            if (isZipImport) {
                // ZIP import: upload the file
                const zipFile = (window as any).__pendingImportZipFile as File;
                const formData = new FormData();
                formData.append('file', zipFile);

                response = await authenticatedFetch(`${API_URL}/road/import-zip`, {
                    method: "POST",
                    headers: { Authorization: `Bearer ${accessToken}` },
                    body: formData,
                });
                // Clean up
                delete (window as any).__pendingImportZipFile;
            } else {
                // Legacy JSON import
                response = await authenticatedFetch(`${API_URL}/road/import`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
                    body: JSON.stringify({ workflow_data: pendingImportData }),
                });
            }

            if (!response.ok) {
                throw new Error("Failed to import workflow");
            }

            const result: WorkflowImportResult = await response.json();

            if (result.success && result.workflow_id) {
                // Load the imported workflow
                const workflowResponse = await authenticatedFetch(`${API_URL}/road/${result.workflow_id}`, {
                    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
                });

                if (workflowResponse.ok) {
                    const workflow: WorkflowSummary = await workflowResponse.json();

                    // Fetch versions
                    const versionsResponse = await authenticatedFetch(`${API_URL}/road/${workflow.id}/versions?limit=50&offset=0`, {
                        headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
                    });

                    if (versionsResponse.ok) {
                        const versionsData: WorkflowVersion[] = await versionsResponse.json();
                        const latestVersion = versionsData[versionsData.length - 1];

                        if (latestVersion) {
                            // LDF-4 #2/#3 — enriching helper so
                            // imported graphs get schema fields
                            // populated for handle rendering.
                            const { nodes: safeNodes, edges: safeEdges } = await safeLoadGraphWithEnrichment(
                                latestVersion.graph_json as any, API_URL, accessToken,
                            );
                            setNodes(safeNodes);
                            setEdges(safeEdges);
                            resetHistory();
                            setCurrentWorkflow(workflow);
                            setWorkflowName(workflow.name);
                            setCurrentVersionNumber(latestVersion.version_number);
                            setCurrentVersionId(latestVersion.id);
                            setVersions(versionsData);
                        }
                    }
                }

                toast({ title: "Import Successful", description: result.message });
            } else {
                toast({ title: "Import Completed", description: result.message, variant: result.missing_nodes.length > 0 ? "destructive" : "default" });
            }

        } catch (error) {
            if ((error as Error).message !== "Session expired") {
                console.error("Error importing workflow:", error);
                toast({ title: "Import Failed", description: (error as Error).message, variant: "destructive" });
            }
        } finally {
            setIsImporting(false);
            setImportDialogOpen(false);
            setPendingImportData(null);
            setImportValidation(null);
        }
    }

    // Cancel import
    function handleImportCancel() {
        setImportDialogOpen(false);
        setPendingImportData(null);
        setImportValidation(null);
        delete (window as any).__pendingImportZipFile;
    }
};