/**
 * workflow-history — a pure, framework-agnostic undo/redo history stack for the
 * WorkflowBuilder canvas.
 *
 * The canvas state is a pair of arrays: `nodes` and `edges` (ReactFlow controlled
 * state). This module snapshots that pair at explicit COMMIT POINTS — node add /
 * delete, edge connect / delete, a completed node drag, and a burst of config
 * edits from a panel — NOT on every ReactFlow change tick. Keeping the reducer
 * logic here (rather than inline in the React hook) means the push / undo / redo /
 * cap / clear-future semantics can be unit-tested without mounting the builder,
 * exactly like `workflow-hotkeys.ts`.
 *
 * Model (two-stack, "record the pre-mutation state"):
 *   - `commitHistory(h, present)` pushes the state *before* a mutation onto `past`
 *     and clears `future` (a new edit invalidates the redo branch).
 *   - `undo(h, present)` returns the top of `past` to apply and moves the current
 *     `present` (the post-mutation live state) onto `future` so it can be redone.
 *   - `redo(h, present)` is the mirror image.
 *
 * Snapshots are deep-ish cloned on the way in and (by the caller) on the way out
 * so that ReactFlow's later immutable updates can never mutate a stored snapshot,
 * and applying a snapshot always hands ReactFlow fresh object references.
 */

import type { Node, Edge } from "reactflow";

export interface GraphSnapshot {
    nodes: Node[];
    edges: Edge[];
}

export interface WorkflowHistory {
    /** States we can return to, oldest first. The last entry is the most recent. */
    past: GraphSnapshot[];
    /** States we can redo into. The last entry is the next redo target. */
    future: GraphSnapshot[];
}

/** Maximum number of undo steps retained. Older entries are dropped. */
export const HISTORY_LIMIT = 50;

export function createHistory(): WorkflowHistory {
    return { past: [], future: [] };
}

export function canUndo(history: WorkflowHistory): boolean {
    return history.past.length > 0;
}

export function canRedo(history: WorkflowHistory): boolean {
    return history.future.length > 0;
}

/**
 * Deep-ish clone of a snapshot. Clones the array containers plus each node's
 * `position` and `data` (the fields the canvas mutates) and each edge object, so
 * a stored snapshot is insulated from later ReactFlow updates without paying for
 * a full structuredClone of large `data` payloads.
 */
export function cloneSnapshot(snapshot: GraphSnapshot): GraphSnapshot {
    return {
        nodes: snapshot.nodes.map((n) => ({
            ...n,
            position: n.position ? { ...n.position } : n.position,
            data: n.data && typeof n.data === "object" ? { ...n.data } : n.data,
        })),
        edges: snapshot.edges.map((e) => ({ ...e })),
    };
}

/**
 * Record a commit point: push the pre-mutation `present` snapshot onto `past`,
 * clear the redo branch, and cap the depth. Returns a new history (never mutates).
 */
export function commitHistory(
    history: WorkflowHistory,
    present: GraphSnapshot,
    limit: number = HISTORY_LIMIT,
): WorkflowHistory {
    const past = [...history.past, cloneSnapshot(present)];
    // Cap: drop the oldest entries beyond the limit.
    while (past.length > limit) past.shift();
    return { past, future: [] };
}

/**
 * Undo: returns the snapshot to apply and the next history, or null when there is
 * nothing to undo. `present` is the current (post-mutation) live state, which is
 * pushed onto `future` so a subsequent redo can restore it.
 */
export function undo(
    history: WorkflowHistory,
    present: GraphSnapshot,
): { history: WorkflowHistory; snapshot: GraphSnapshot } | null {
    if (history.past.length === 0) return null;
    const snapshot = history.past[history.past.length - 1];
    return {
        history: {
            past: history.past.slice(0, -1),
            future: [...history.future, cloneSnapshot(present)],
        },
        snapshot,
    };
}

/**
 * Redo: mirror of {@link undo}. Returns the snapshot to apply and the next
 * history, or null when the redo branch is empty.
 */
export function redo(
    history: WorkflowHistory,
    present: GraphSnapshot,
): { history: WorkflowHistory; snapshot: GraphSnapshot } | null {
    if (history.future.length === 0) return null;
    const snapshot = history.future[history.future.length - 1];
    return {
        history: {
            past: [...history.past, cloneSnapshot(present)],
            future: history.future.slice(0, -1),
        },
        snapshot,
    };
}

/**
 * True when two node arrays describe the same set of nodes in the same positions.
 * Used to skip recording a "drag" that ended exactly where it started (a click),
 * so no empty history entry is created.
 */
export function samePositions(a: Node[], b: Node[]): boolean {
    if (a.length !== b.length) return false;
    const byId = new Map(b.map((n) => [n.id, n]));
    for (const n of a) {
        const other = byId.get(n.id);
        if (!other) return false;
        const ap = n.position || { x: 0, y: 0 };
        const bp = other.position || { x: 0, y: 0 };
        if (ap.x !== bp.x || ap.y !== bp.y) return false;
    }
    return true;
}
