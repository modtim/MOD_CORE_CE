"use client";

import { useEffect, useRef, useState } from "react";
import { apiUrl } from "@/lib/runtime-config";
import { Button } from "@/components/ui/button";
import { Save, Upload, Play, Pencil, Download, FileUp, Clock, Square, Plus, ChevronDown, FilePlus, Share2, Users, Undo2, Redo2, Pause } from "lucide-react";
import { WorkflowCostDisplay } from "./WorkflowCostDisplay";
import { RunQuoteDisplay } from "./RunQuoteDisplay";
import {
    TriggerCreateDialog,
    type TriggerType,
} from "./TriggerCreateDialog";
import { ShareAppDialog } from "./ShareAppDialog";
import { useEntitlements } from "@/hooks/use-entitlements";
import { WorkflowShareDialog } from "./WorkflowShareDialog";
import BurstEligibilityItem from "./BurstEligibilityItem";
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
import RunRail from "./RunRail";
import GridView from "./GridView";

interface WorkflowToolbarProps {
    /** LDF-3 §2.1 — Start a fresh workflow without reloading the page.
     *  Parent is responsible for the unsaved-changes confirm dialog. */
    onNewWorkflow?: () => void;
    /** Undo/redo the last canvas edit. Buttons are disabled when the
     *  corresponding history stack is empty. */
    onUndo?: () => void;
    onRedo?: () => void;
    canUndo?: boolean;
    canRedo?: boolean;
    onSave: () => void;
    onSaveAs: () => void;
    onLoad: () => void;
    onEdit: () => void;
    onExecute: () => void;
    /** When non-null, the workflow has an active run with this ID. The
     *  Execute button transforms into a Cancel button that calls
     *  `onCancelRun()`. When null, Execute is rendered as usual. */
    activeRunId?: number | null;
    /** Cancel the currently-active run (used when activeRunId is set). */
    onCancelRun?: () => void;
    /** Multi-run switcher: the run the canvas is currently bound to, and a
     *  setter to bind it to any run picked from the run rail. */
    selectedRunId?: number | null;
    onSelectRun?: (runId: number) => void;
    /** WSIP 4.5 — the run rail's "Explain this run" button hands the picked
     *  run id to the parent, which opens the Workflow Wizard in debug mode. */
    onExplainRun?: (runId: number) => void;
    /** "Fresh run" toggle: OFF = reuse unchanged node results (cache); ON =
     *  ignore the cache and re-run everything. */
    freshRun?: boolean;
    onFreshRunChange?: (v: boolean) => void;
    schedulePaused?: boolean;
    onSchedulePauseChange?: (paused: boolean) => void;
    /** Sync the selected run's per-node pause points (from the run rail's poll). */
    onPausedNodesChange?: (nodes: string[]) => void;
    onQueue: () => void;
    onExport: () => void;
    onImportFile: (event: React.ChangeEvent<HTMLInputElement>) => void;
    /** Quick-create a schedule trigger for the current workflow with this
     *  cron expression. Other trigger types open the full dialog instead
     *  and bypass this prop. */
    onSchedule?: (cron: string) => void;
    /** Notification surface for the toolbar to react to a successful
     *  trigger create (any type) — e.g. show a toast. */
    onTriggerCreated?: (triggerType: TriggerType) => void;
    isWorkflowLoaded: boolean;
    workflowName?: string;
    versionNumber?: number;
    /** Id of the version currently loaded on the canvas — forwarded to
     *  ShareAppDialog so "Share as App" pins the app to the viewed version. */
    versionId?: number | null;
    workflowId?: number;
    nodes: any[];
    edges: any[];
    accessToken: string | null;
    fileCount?: number;
}

export const WorkflowToolbar = ({
    onNewWorkflow,
    onUndo,
    onRedo,
    canUndo,
    canRedo,
    onSave,
    onSaveAs,
    onLoad,
    onEdit,
    onExecute,
    activeRunId,
    onCancelRun,
    selectedRunId,
    onSelectRun,
    onExplainRun,
    freshRun,
    onFreshRunChange,
    schedulePaused,
    onSchedulePauseChange,
    onPausedNodesChange,
    onQueue,
    onExport,
    onImportFile,
    onSchedule,
    onTriggerCreated,
    isWorkflowLoaded,
    workflowName,
    versionNumber,
    versionId,
    workflowId,
    nodes,
    edges,
    accessToken,
    fileCount
}: WorkflowToolbarProps) => {
    const importInputRef = useRef<HTMLInputElement>(null);
    const [showScheduleMenu, setShowScheduleMenu] = useState(false);
    const [showToolsMenu, setShowToolsMenu] = useState(false);
    // LDF-3 §2.3 / §2.4 — Save and Actions dropdown state. Outside-click
    // closes both via the document-level handler below.
    const [showSaveMenu, setShowSaveMenu] = useState(false);
    const [showActionsMenu, setShowActionsMenu] = useState(false);
    const saveMenuRef = useRef<HTMLDivElement | null>(null);
    const actionsMenuRef = useRef<HTMLDivElement | null>(null);
    const [dialogOpen, setDialogOpen] = useState(false);
    const [showShareApp, setShowShareApp] = useState(false);
    const [showShareWorkflow, setShowShareWorkflow] = useState(false);
    // 2400UI122e — "Share as App" is PAID: its core endpoint /road/{id}/run-links
    // is denied on CE (run_surface*.py, open-core/manifest.yaml:1545), so the entry
    // must not be offered there. FAIL-OPEN: hidden ONLY on a positively-resolved
    // edition "ce" (unresolved/errored → edition null → paid behaviour untouched);
    // same mechanism as assistant-launcher.tsx:44-49. No fetch is involved here, so
    // no bounded gate watcher is needed — the entry is just not rendered.
    const { edition } = useEntitlements();
    const ceEdition = edition === "ce";
    // Confirm before cancelling an active run — cancelling stops execution and
    // cannot be undone, so gate it behind a confirmation dialog.
    const [showCancelConfirm, setShowCancelConfirm] = useState(false);
    const [dialogInitialType, setDialogInitialType] =
        useState<TriggerType>("schedule");

    // Close the Save / Actions menus when the user clicks outside.
    useEffect(() => {
        if (!showSaveMenu && !showActionsMenu) return;
        const onDocClick = (e: MouseEvent) => {
            const t = e.target as Node;
            if (showSaveMenu && saveMenuRef.current && !saveMenuRef.current.contains(t)) {
                setShowSaveMenu(false);
            }
            if (showActionsMenu && actionsMenuRef.current && !actionsMenuRef.current.contains(t)) {
                setShowActionsMenu(false);
            }
        };
        document.addEventListener("mousedown", onDocClick);
        return () => document.removeEventListener("mousedown", onDocClick);
    }, [showSaveMenu, showActionsMenu]);

    const apiUrlValue = apiUrl();

    function openDialog(type: TriggerType) {
        setDialogInitialType(type);
        setShowScheduleMenu(false);
        setDialogOpen(true);
    }

    // Cross-component bridge: the node library's Functions menu fires
    // mod:open-schedule-dialog when the user picks "Schedule". The
    // dialog state lives in this toolbar (the only owner today), so
    // we react to the event here.
    useEffect(() => {
        const handler = () => openDialog("schedule");
        window.addEventListener("mod:open-schedule-dialog", handler);
        return () => window.removeEventListener("mod:open-schedule-dialog", handler);
    }, []);

    return (
        <div className="flex items-center gap-2">
            {/* Cost Display - LEFT SIDE */}
            <WorkflowCostDisplay
                nodes={nodes}
                edges={edges}
                accessToken={accessToken}
                fileCount={fileCount}
            />

            {/* LDF-3 §2.1 — `+ New` lives left of the workflow-name pill */}
            {onNewWorkflow && (
                <Button
                    onClick={onNewWorkflow}
                    size="sm"
                    variant="outline"
                    title="Start a fresh workflow"
                >
                    <Plus className="h-4 w-4 mr-1" />New
                </Button>
            )}

            {/* Undo / Redo — Ctrl/Cmd+Z and Ctrl/Cmd+Y (or Shift+Z) mirror
                these. Disabled when the matching history stack is empty. */}
            {(onUndo || onRedo) && (
                <div className="inline-flex">
                    <Button
                        onClick={onUndo}
                        disabled={!canUndo}
                        size="sm"
                        variant="outline"
                        className="rounded-r-none border-r-0 px-2"
                        title="Undo (Ctrl+Z)"
                        aria-label="Undo"
                    >
                        <Undo2 className="h-4 w-4" />
                    </Button>
                    <Button
                        onClick={onRedo}
                        disabled={!canRedo}
                        size="sm"
                        variant="outline"
                        className="rounded-l-none px-2"
                        title="Redo (Ctrl+Y)"
                        aria-label="Redo"
                    >
                        <Redo2 className="h-4 w-4" />
                    </Button>
                </div>
            )}

            {/* Workflow-name pill has moved out of this toolbar — see
                workflow-builder.tsx, where it's pinned to the top-left
                of the canvas, next to the node library panel. The
                workflowName / versionNumber props remain on this
                component because the TriggerCreateDialog (below)
                still needs the workflow name to label new triggers
                with the current workflow's name. */}

            {/* LDF-3 §2.3 — Save dropdown. Default click on the trigger
                fires Save (the most-frequent action). The chevron opens
                the menu with Save / Save version / Save as. Existing
                keyboard shortcuts continue to fire the underlying
                handlers directly without opening the menu. */}
            <div className="relative" ref={saveMenuRef}>
                <div className="inline-flex">
                    <Button
                        onClick={isWorkflowLoaded ? onSave : onSaveAs}
                        size="sm"
                        variant="outline"
                        className="rounded-r-none border-r-0 pr-2"
                    >
                        <Save className="h-4 w-4 mr-1" />Save
                    </Button>
                    <Button
                        onClick={() => setShowSaveMenu(v => !v)}
                        size="sm"
                        variant="outline"
                        className="rounded-l-none px-1"
                        aria-label="More save options"
                    >
                        <ChevronDown className="h-4 w-4" />
                    </Button>
                </div>
                {showSaveMenu && (
                    <div className="absolute right-0 top-full mt-1 w-48 bg-white border rounded-lg shadow-lg z-50 py-1">
                        <button
                            onClick={() => { setShowSaveMenu(false); (isWorkflowLoaded ? onSave : onSaveAs)(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <Save className="h-4 w-4 mr-2" />Save
                        </button>
                        {/* No longer disabled on an unsaved workflow: a
                            disabled entry is a dead click, and `onSave` now
                            reroutes to Save As when there is no id to version. */}
                        <button
                            onClick={() => { setShowSaveMenu(false); onSave(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <FilePlus className="h-4 w-4 mr-2" />Save version
                        </button>
                        <button
                            onClick={() => { setShowSaveMenu(false); onSaveAs(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <Save className="h-4 w-4 mr-2" />Save as…
                        </button>
                    </div>
                )}
            </div>

            {/* LDF-3 §2.4 — Actions dropdown collapses Load / Edit /
                Export / Import. */}
            <div className="relative" ref={actionsMenuRef}>
                <Button
                    onClick={() => setShowActionsMenu(v => !v)}
                    size="sm"
                    variant="outline"
                    className="pr-2"
                >
                    Actions<ChevronDown className="h-4 w-4 ml-1" />
                </Button>
                {/* w-64, widened from w-48: the burst entry carries a second
                    line saying what the current state means. */}
                {showActionsMenu && (
                    <div className="absolute right-0 top-full mt-1 w-64 bg-white border rounded-lg shadow-lg z-50 py-1">
                        <button
                            onClick={() => { setShowActionsMenu(false); onLoad(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <Upload className="h-4 w-4 mr-2" />Load
                        </button>
                        <button
                            onClick={() => { setShowActionsMenu(false); onEdit(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <Pencil className="h-4 w-4 mr-2" />Edit
                        </button>
                        <button
                            onClick={() => { setShowActionsMenu(false); onExport(); }}
                            disabled={!isWorkflowLoaded}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed flex items-center"
                        >
                            <Download className="h-4 w-4 mr-2" />Export
                        </button>
                        <button
                            onClick={() => { setShowActionsMenu(false); importInputRef.current?.click(); }}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                        >
                            <FileUp className="h-4 w-4 mr-2" />Import
                        </button>
                        {workflowId && isWorkflowLoaded && (
                            <>
                                <div className="border-t my-1" />
                                <button
                                    onClick={() => { setShowActionsMenu(false); setShowShareWorkflow(true); }}
                                    className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                                    title="Give another user in your organization access to this workflow"
                                >
                                    <Users className="h-4 w-4 mr-2" />Share
                                </button>
                                {!ceEdition && (
                                    <button
                                        onClick={() => { setShowActionsMenu(false); setShowShareApp(true); }}
                                        className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 flex items-center"
                                        title="Turn this workflow into an embeddable run page"
                                    >
                                        <Share2 className="h-4 w-4 mr-2" />Share as App
                                    </button>
                                )}
                                {/* burst-to-saas §3.12 — consent sits with the
                                    workflow, not the tenant. Renders nothing
                                    unless this install is paired with MOD. */}
                                <BurstEligibilityItem
                                    workflowId={workflowId}
                                    apiUrl={apiUrlValue}
                                />
                            </>
                        )}
                    </div>
                )}
            </div>
            <input
                ref={importInputRef}
                type="file"
                accept=".zip,.json"
                onChange={onImportFile}
                className="hidden"
            />

            {/* The Process queue + Schedule entries that used to live as
                standalone toolbar buttons have been moved to the
                Functions dropdown on the node library bar (issue #2/#9).
                The Schedule dropdown panel itself stays here (the dialog
                + preset menu) — it's opened from the node library via
                the mod:open-schedule-dialog window event, AND can also
                be reached directly via the (now hidden) standalone
                button below for keyboard shortcut compatibility. */}
            <div className="relative">
                <Button
                    onClick={() => setShowScheduleMenu(!showScheduleMenu)}
                    size="sm"
                    variant="outline"
                    disabled={!isWorkflowLoaded}
                    className="hidden"
                >
                    <Clock className="h-4 w-4 mr-2" />Schedule
                </Button>
                {showScheduleMenu && (
                    <div className="absolute right-0 top-full mt-1 w-64 bg-white border rounded-lg shadow-lg z-50 py-1 max-h-[80vh] overflow-y-auto">
                        <div className="px-3 pt-2 pb-1 text-xs font-semibold uppercase text-gray-500">Schedule presets</div>
                        <button onClick={() => { onSchedule?.("*/30 * * * *"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Every 30 minutes</button>
                        <button onClick={() => { onSchedule?.("0 * * * *"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Every hour</button>
                        <button onClick={() => { onSchedule?.("0 */6 * * *"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Every 6 hours</button>
                        <button onClick={() => { onSchedule?.("0 9 * * *"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Daily at 9am</button>
                        <button onClick={() => { onSchedule?.("0 9 * * 1"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Weekly (Monday 9am)</button>
                        <button onClick={() => { onSchedule?.("0 0 1 * *"); setShowScheduleMenu(false); }} className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100">Monthly (1st at midnight)</button>
                        <button
                            onClick={() => openDialog("schedule")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 text-indigo-600"
                        >
                            Custom cron...
                        </button>

                        <div className="border-t my-1"></div>
                        <div className="px-3 pt-2 pb-1 text-xs font-semibold uppercase text-gray-500">Other trigger types</div>
                        <button
                            onClick={() => openDialog("workflow_completion")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                        >
                            Workflow Completion
                        </button>
                        <button
                            onClick={() => openDialog("webhook")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                        >
                            Webhook
                        </button>
                        <button
                            onClick={() => openDialog("file_event")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                        >
                            File Event
                        </button>
                        <button
                            onClick={() => openDialog("log_pattern_alert")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                        >
                            Log Pattern Alert
                        </button>
                        <button
                            onClick={() => openDialog("queue_empty")}
                            className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100"
                        >
                            Queue Empty
                        </button>
                    </div>
                )}
            </div>

            {/* Run rail: switch the canvas between this workflow's runs (live or
                past) — the multi-run switcher. */}
            {workflowId != null && onSelectRun && (
                <RunRail
                    workflowId={workflowId}
                    accessToken={accessToken}
                    apiUrl={apiUrlValue}
                    selectedRunId={selectedRunId ?? null}
                    onSelectRun={onSelectRun}
                    onExplainRun={onExplainRun}
                    onPausedNodesChange={onPausedNodesChange}
                />
            )}
            {onFreshRunChange && (
                <Button
                    size="sm"
                    variant={freshRun ? "default" : "outline"}
                    onClick={() => onFreshRunChange(!freshRun)}
                    title={freshRun
                        ? "Fresh run: every node re-runs (cache ignored). Click to reuse unchanged results instead."
                        : "Reusing unchanged results (skips work that didn't change). Click for a fresh full re-run."}
                >
                    {freshRun ? "Fresh run" : "Reuse ✓"}
                </Button>
            )}
            {onSchedulePauseChange && workflowId != null && (
                <Button
                    size="sm"
                    variant={schedulePaused ? "default" : "outline"}
                    onClick={() => onSchedulePauseChange(!schedulePaused)}
                    title={schedulePaused
                        ? "Schedule paused: no new runs start from schedules/triggers. Click to resume."
                        : "Pause the schedule: stop new runs from starting (in-progress runs keep going)."}
                >
                    {schedulePaused ? "Schedule paused" : "Pause schedule"}
                </Button>
            )}
            {workflowId != null && onSelectRun && (
                <GridView
                    workflowId={workflowId}
                    accessToken={accessToken}
                    apiUrl={apiUrlValue}
                    selectedRunId={selectedRunId ?? null}
                    onSelectRun={onSelectRun}
                />
            )}
            {/* GX.sz-tcp.25 — locked per-run price, fetched from the same
                quote endpoint that the charge path uses, shown right next to
                Execute so the user sees it before starting a run. Never
                blocks Execute (loading/error states are non-blocking). */}
            <RunQuoteDisplay workflowId={workflowId ?? null} accessToken={accessToken} />
            {/* Execute ALWAYS stays available (Airflow-like: you can launch
                another run while one is in progress — Execute prompts to start a
                concurrent run or stop the current one). When a run is active a
                Stop button appears alongside it that stops the run in view. */}
            <Button
                onClick={onExecute}
                size="sm"
                variant="default"
                title={isWorkflowLoaded
                    ? "Execute workflow"
                    : "Save your workflow first — clicking will prompt you to save before it can run."}
            >
                <Play className="h-4 w-4 mr-2" />Execute
            </Button>
            {activeRunId != null && (
                <Button
                    onClick={() => setShowCancelConfirm(true)}
                    size="sm"
                    variant="destructive"
                    disabled={!isWorkflowLoaded || !onCancelRun}
                    title={`Stop run #${activeRunId}`}
                >
                    <Square className="h-4 w-4 mr-2" />Stop
                </Button>
            )}

            {/* Centralized trigger creation dialog. Mounted once at toolbar
                level — workflow_completion / webhook / file_event /
                log_pattern_alert / queue_empty all open it. The schedule
                presets above bypass it because their config is just a cron
                string and the toolbar already has that wired through
                onSchedule. The "Custom cron..." entry routes into the
                dialog so the user can paste an arbitrary cron without a
                browser prompt(). */}
            {workflowId && (
                <TriggerCreateDialog
                    open={dialogOpen}
                    onOpenChange={setDialogOpen}
                    initialType={dialogInitialType}
                    workflowId={workflowId}
                    workflowName={workflowName}
                    accessToken={accessToken}
                    apiUrl={apiUrlValue}
                    onCreated={onTriggerCreated}
                />
            )}
            {workflowId && (
                <ShareAppDialog
                    open={showShareApp}
                    onOpenChange={setShowShareApp}
                    workflowId={workflowId}
                    workflowName={workflowName}
                    versionId={versionId}
                    versionNumber={versionNumber}
                    accessToken={accessToken}
                    apiUrl={apiUrlValue}
                />
            )}
            {workflowId && (
                <WorkflowShareDialog
                    open={showShareWorkflow}
                    onOpenChange={setShowShareWorkflow}
                    workflowId={workflowId}
                    workflowName={workflowName}
                    accessToken={accessToken}
                    apiUrl={apiUrlValue}
                />
            )}

            {/* Confirm before cancelling an active run. */}
            <AlertDialog open={showCancelConfirm} onOpenChange={setShowCancelConfirm}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Cancel this run?</AlertDialogTitle>
                    </AlertDialogHeader>
                    <AlertDialogDescription>
                        This will stop execution.
                    </AlertDialogDescription>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Keep running</AlertDialogCancel>
                        <AlertDialogAction
                            onClick={() => { setShowCancelConfirm(false); onCancelRun?.(); }}
                        >
                            Cancel run
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
};
