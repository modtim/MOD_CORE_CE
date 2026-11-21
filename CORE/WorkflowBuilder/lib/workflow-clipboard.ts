/**
 * workflow-clipboard — pure graph logic for copy / paste / duplicate of canvas
 * nodes.
 *
 * Kept free of React and ReactFlow runtime state so the id-remapping and
 * edge-reconstruction rules can be unit-tested without mounting the builder.
 * The component layer (workflow-builder.tsx) owns the actual clipboard ref,
 * `setNodes`/`setEdges`, and the undo `commit()`; this module only computes the
 * new nodes/edges.
 *
 *   - copySelection()  — snapshot the selected nodes + the edges that run
 *                        BETWEEN them (edges to non-copied nodes are dropped).
 *   - buildPaste()     — clone that snapshot with fresh, collision-free ids, a
 *                        small position offset, and edges rewired onto the new
 *                        ids. Node data is deep-cloned so the paste never shares
 *                        mutable config / selectedFiles arrays with the original.
 */

import type { Edge, Node } from "reactflow";

export interface ClipboardSnapshot {
    nodes: Node[];
    edges: Edge[];
}

export interface PasteResult {
    nodes: Node[];
    edges: Edge[];
    /** old graph-node id -> new graph-node id, for callbacks (e.g. fileStore). */
    idMap: Record<string, string>;
}

/** Default paste/duplicate offset in flow units — enough to reveal the copy. */
export const PASTE_OFFSET = 40;

/**
 * Deep-clone a value, preferring the structured clone algorithm and falling
 * back to JSON for environments (or payloads) it can't handle. Node data here
 * is plain (labels, schema objects, selectedFiles metadata) — File objects live
 * in the separate fileStore, not in node.data — so JSON is a safe fallback.
 */
function deepClone<T>(value: T): T {
    try {
        if (typeof structuredClone === "function") return structuredClone(value);
    } catch {
        /* fall through to JSON */
    }
    try {
        return JSON.parse(JSON.stringify(value));
    } catch {
        return value;
    }
}

/**
 * Build the copy snapshot from the current graph: the selected nodes and only
 * the edges whose BOTH ends are selected. Returns null when nothing is selected
 * so callers can leave the browser's native copy untouched.
 */
export function copySelection(nodes: Node[], edges: Edge[]): ClipboardSnapshot | null {
    const selected = nodes.filter((n) => n.selected);
    if (selected.length === 0) return null;
    const selectedIds = new Set(selected.map((n) => n.id));
    const internalEdges = edges.filter(
        (e) => selectedIds.has(e.source) && selectedIds.has(e.target),
    );
    return {
        nodes: selected.map((n) => ({ ...n, data: deepClone(n.data) })),
        edges: internalEdges.map((e) => ({ ...e })),
    };
}

/**
 * Generate a graph-node id that collides with neither the existing graph nor
 * ids already minted in this same paste. Readable prefix from the node type,
 * with a short random suffix for guaranteed uniqueness across loaded workflows
 * (whose ids are arbitrary strings) and repeated pastes.
 */
function freshId(type: string | undefined, taken: Set<string>): string {
    const base = (type || "node").toString();
    let id = "";
    do {
        const rand = Math.random().toString(36).slice(2, 8);
        id = `${base}-copy-${rand}`;
    } while (taken.has(id));
    taken.add(id);
    return id;
}

/**
 * Clone a clipboard snapshot into a fresh set of nodes/edges ready to add to the
 * graph: new ids, offset position, all pasted nodes selected (existing selection
 * is cleared by the caller), and only the internal edges rewired onto the new
 * ids. `existingIds` are the graph-node ids already on the canvas, so pasted ids
 * never collide.
 */
export function buildPaste(
    clipboard: ClipboardSnapshot | null,
    existingIds: Iterable<string>,
    offset: number = PASTE_OFFSET,
): PasteResult | null {
    if (!clipboard || clipboard.nodes.length === 0) return null;

    const taken = new Set<string>(existingIds);
    const idMap: Record<string, string> = {};

    const nodes: Node[] = clipboard.nodes.map((n) => {
        const newId = freshId(n.type, taken);
        idMap[n.id] = newId;
        return {
            ...n,
            id: newId,
            position: {
                x: (n.position?.x ?? 0) + offset,
                y: (n.position?.y ?? 0) + offset,
            },
            // A fresh paste is the new selection; drop any stale drag/measure
            // internals that shouldn't carry over to a brand-new node.
            selected: true,
            dragging: false,
            data: deepClone(n.data),
        };
    });

    const edges: Edge[] = clipboard.edges
        .filter((e) => idMap[e.source] && idMap[e.target])
        .map((e) => {
            const source = idMap[e.source];
            const target = idMap[e.target];
            const rand = Math.random().toString(36).slice(2, 8);
            return {
                ...e,
                id: `e-${source}-${target}-${rand}`,
                source,
                target,
                selected: false,
            };
        });

    return { nodes, edges, idMap };
}
