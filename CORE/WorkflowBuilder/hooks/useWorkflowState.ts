"use client";

import { useCallback, useRef, useState } from "react";
import { useNodesState, useEdgesState, type Node, type Edge } from "reactflow";
import {
    createHistory,
    commitHistory,
    undo as undoHistory,
    redo as redoHistory,
    cloneSnapshot,
    samePositions,
    type GraphSnapshot,
    type WorkflowHistory,
} from "@/lib/workflow-history";

/**
 * Coalescing window for config/param edits committed from panels. A run of
 * keystrokes in a text field (or slider drags) fires `commitConfig` many times;
 * only the first of a run for a given node records a history entry, so undo
 * reverts the whole run at once rather than one character at a time.
 */
const CONFIG_BURST_MS = 700;

/**
 * useWorkflowState — owns the canvas node/edge state AND an undo/redo history
 * stack.
 *
 * ReactFlow's `nodes`/`edges` remain plain controlled state driven by
 * `onNodesChange`/`onEdgesChange` (position/selection/dimension churn). History
 * is captured only at explicit COMMIT POINTS, so the redo branch and the depth
 * cap are never flooded by per-frame drag ticks:
 *
 *   - `commit()`          — a discrete mutation (node add, node/edge delete,
 *                           connect). Coalesced within one event tick so a node
 *                           delete that also removes edges records ONE entry.
 *   - `beginNodeDrag()` / `endNodeDrag()` — a drag records a single entry using
 *                           the pre-drag positions, and only if anything moved.
 *   - `commitConfig(id)`  — a burst of panel edits on a node records one entry.
 *   - `resetHistory()`    — clears history at a new baseline (new / load / import).
 *
 * `undo`/`redo` restore snapshots via `setNodes`/`setEdges` (never through
 * `onNodesChange`), so they don't re-enter the commit path or desync ReactFlow's
 * controlled state.
 */
export const useWorkflowState = () => {
    const [nodes, setNodes, onNodesChange] = useNodesState([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState([]);
    const [selectedNode, setSelectedNode] = useState<Node | null>(null);

    // Live mirrors so the history callbacks can read the current state without
    // being recreated (and re-subscribed by the keydown effect) on every change.
    const nodesRef = useRef<Node[]>(nodes);
    nodesRef.current = nodes;
    const edgesRef = useRef<Edge[]>(edges);
    edgesRef.current = edges;

    const historyRef = useRef<WorkflowHistory>(createHistory());
    const [canUndo, setCanUndo] = useState(false);
    const [canRedo, setCanRedo] = useState(false);
    const syncFlags = useCallback(() => {
        setCanUndo(historyRef.current.past.length > 0);
        setCanRedo(historyRef.current.future.length > 0);
    }, []);

    const present = useCallback(
        (): GraphSnapshot => ({ nodes: nodesRef.current, edges: edgesRef.current }),
        [],
    );

    const pushSnapshot = useCallback(
        (snapshot: GraphSnapshot) => {
            historyRef.current = commitHistory(historyRef.current, snapshot);
            syncFlags();
        },
        [syncFlags],
    );

    // --- Discrete commit points (add / delete / connect) -------------------
    // Coalesced within a single event tick: a node deletion that also removes
    // connected edges fires both onNodesDelete and onEdgesDelete synchronously,
    // and we want exactly one undo entry for the whole gesture. Because React
    // hasn't re-rendered yet, every call in the tick reads the same pre-mutation
    // `present()`.
    const tickGuard = useRef(false);
    const commit = useCallback(() => {
        if (tickGuard.current) return;
        tickGuard.current = true;
        queueMicrotask(() => {
            tickGuard.current = false;
        });
        pushSnapshot(present());
    }, [present, pushSnapshot]);

    // --- Drag commit point --------------------------------------------------
    const dragSnapshot = useRef<GraphSnapshot | null>(null);
    const beginNodeDrag = useCallback(() => {
        dragSnapshot.current = cloneSnapshot(present());
    }, [present]);
    const endNodeDrag = useCallback(() => {
        const snap = dragSnapshot.current;
        dragSnapshot.current = null;
        if (!snap) return;
        // A drag that ended where it started (or a bare click) records nothing.
        if (samePositions(snap.nodes, nodesRef.current)) return;
        pushSnapshot(snap);
    }, [pushSnapshot]);

    // --- Config-edit commit point (burst-coalesced per node) ---------------
    const configBurstNode = useRef<string | null>(null);
    const configBurstTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const commitConfig = useCallback(
        (nodeId: string) => {
            const sameBurst =
                configBurstNode.current === nodeId && configBurstTimer.current !== null;
            if (configBurstTimer.current) clearTimeout(configBurstTimer.current);
            configBurstNode.current = nodeId;
            configBurstTimer.current = setTimeout(() => {
                configBurstTimer.current = null;
                configBurstNode.current = null;
            }, CONFIG_BURST_MS);
            // Only the FIRST edit of a burst records the pre-edit state; later
            // keystrokes just advance the live state within the same entry.
            if (!sameBurst) pushSnapshot(present());
        },
        [present, pushSnapshot],
    );

    // --- Apply a restored snapshot -----------------------------------------
    const applySnapshot = useCallback(
        (snapshot: GraphSnapshot) => {
            const fresh = cloneSnapshot(snapshot);
            setNodes(fresh.nodes);
            setEdges(fresh.edges);
            // Keep the config panel coherent: drop or refresh the selection so it
            // never points at a node that undo/redo just removed or replaced.
            setSelectedNode((prev) => {
                if (!prev) return prev;
                return fresh.nodes.find((n) => n.id === prev.id) ?? null;
            });
        },
        [setNodes, setEdges],
    );

    const undo = useCallback(() => {
        const result = undoHistory(historyRef.current, present());
        if (!result) return;
        historyRef.current = result.history;
        applySnapshot(result.snapshot);
        syncFlags();
    }, [present, applySnapshot, syncFlags]);

    const redo = useCallback(() => {
        const result = redoHistory(historyRef.current, present());
        if (!result) return;
        historyRef.current = result.history;
        applySnapshot(result.snapshot);
        syncFlags();
    }, [present, applySnapshot, syncFlags]);

    /** Clear history at a new baseline (new workflow, load, import). */
    const resetHistory = useCallback(() => {
        if (configBurstTimer.current) clearTimeout(configBurstTimer.current);
        configBurstTimer.current = null;
        configBurstNode.current = null;
        dragSnapshot.current = null;
        historyRef.current = createHistory();
        syncFlags();
    }, [syncFlags]);

    return {
        nodes,
        setNodes,
        onNodesChange,
        edges,
        setEdges,
        onEdgesChange,
        selectedNode,
        setSelectedNode,
        // history
        commit,
        commitConfig,
        beginNodeDrag,
        endNodeDrag,
        undo,
        redo,
        canUndo,
        canRedo,
        resetHistory,
    };
};
