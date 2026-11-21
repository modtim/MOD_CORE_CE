/**
 * Tests for the commit-point behavior of the useWorkflowState history stack.
 *
 * The pure reducer (lib/workflow-history) is covered by workflowHistory.test.ts.
 * This suite exercises the React hook wiring that decides WHEN a snapshot is
 * taken — the part that must not flood history:
 *   - a discrete commit + undo/redo round-trips nodes
 *   - a drag that doesn't move records nothing (samePositions guard)
 *   - a drag that moves records exactly one entry and undo restores positions
 *   - a burst of config edits on one node coalesces to a single undo entry
 *   - back-to-back commit() calls in one tick coalesce (delete of node + edges)
 *   - resetHistory clears both stacks
 *
 * useNodesState/useEdgesState are plain useState wrappers, so the hook renders
 * without a ReactFlowProvider.
 */

import { renderHook, act } from "@testing-library/react";
import type { Node } from "reactflow";
import { useWorkflowState } from "../hooks/useWorkflowState";

const node = (id: string, x = 0, y = 0, data: Record<string, unknown> = {}): Node => ({
    id,
    position: { x, y },
    data,
    type: "process",
});

// Flush the microtask queue so the per-tick commit dedup guard resets.
const flushMicrotasks = () => act(async () => { await Promise.resolve(); });

describe("useWorkflowState history — commit points", () => {
    it("commit + undo/redo round-trips an add", () => {
        const { result } = renderHook(() => useWorkflowState());

        act(() => { result.current.setNodes([node("a")]); });
        expect(result.current.canUndo).toBe(false);

        // Simulate an add: commit the pre-mutation state, then mutate.
        act(() => {
            result.current.commit();
            result.current.setNodes([node("a"), node("b")]);
        });
        expect(result.current.canUndo).toBe(true);
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a", "b"]);

        act(() => { result.current.undo(); });
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a"]);
        expect(result.current.canUndo).toBe(false);
        expect(result.current.canRedo).toBe(true);

        act(() => { result.current.redo(); });
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a", "b"]);
        expect(result.current.canRedo).toBe(false);
    });

    it("records nothing for a drag that ends where it started", () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => { result.current.setNodes([node("a", 5, 5)]); });

        act(() => { result.current.beginNodeDrag(); });
        act(() => { result.current.endNodeDrag(); });

        expect(result.current.canUndo).toBe(false);
    });

    it("records one entry for a real drag and undo restores the position", () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => { result.current.setNodes([node("a", 0, 0)]); });

        act(() => { result.current.beginNodeDrag(); });
        // ReactFlow would move the node via onNodesChange during the drag.
        act(() => { result.current.setNodes([node("a", 120, 80)]); });
        act(() => { result.current.endNodeDrag(); });

        expect(result.current.canUndo).toBe(true);
        act(() => { result.current.undo(); });
        expect(result.current.nodes[0].position).toEqual({ x: 0, y: 0 });
        expect(result.current.canUndo).toBe(false);
    });

    it("coalesces a burst of config edits on one node into a single undo entry", () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => { result.current.setNodes([node("a", 0, 0, { name: "" })]); });

        // Three keystrokes on the same node in one burst.
        act(() => {
            result.current.commitConfig("a");
            result.current.setNodes([node("a", 0, 0, { name: "h" })]);
        });
        act(() => {
            result.current.commitConfig("a");
            result.current.setNodes([node("a", 0, 0, { name: "hi" })]);
        });
        act(() => {
            result.current.commitConfig("a");
            result.current.setNodes([node("a", 0, 0, { name: "hey" })]);
        });

        expect(result.current.nodes[0].data).toEqual({ name: "hey" });

        // A single undo reverts the whole burst to the pre-edit value.
        act(() => { result.current.undo(); });
        expect(result.current.nodes[0].data).toEqual({ name: "" });
        expect(result.current.canUndo).toBe(false);
    });

    it("coalesces back-to-back commit() calls in one tick (node + edge delete)", () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => {
            result.current.setNodes([node("a"), node("b")]);
            result.current.setEdges([{ id: "e1", source: "a", target: "b" }]);
        });

        // A node delete fires onNodesDelete AND onEdgesDelete synchronously;
        // both call commit(), but only one entry should be recorded.
        act(() => {
            result.current.commit();
            result.current.commit();
            result.current.setNodes([node("a")]);
            result.current.setEdges([]);
        });

        expect(result.current.canUndo).toBe(true);
        act(() => { result.current.undo(); });
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a", "b"]);
        expect(result.current.edges.map((e) => e.id)).toEqual(["e1"]);
        // Only one entry existed.
        expect(result.current.canUndo).toBe(false);
    });

    it("separately-ticked commit() calls each record an entry", async () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => { result.current.setNodes([node("a")]); });

        act(() => {
            result.current.commit();
            result.current.setNodes([node("a"), node("b")]);
        });
        await flushMicrotasks();
        act(() => {
            result.current.commit();
            result.current.setNodes([node("a"), node("b"), node("c")]);
        });

        act(() => { result.current.undo(); });
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a", "b"]);
        act(() => { result.current.undo(); });
        expect(result.current.nodes.map((n) => n.id)).toEqual(["a"]);
        expect(result.current.canUndo).toBe(false);
    });

    it("resetHistory clears both stacks", () => {
        const { result } = renderHook(() => useWorkflowState());
        act(() => { result.current.setNodes([node("a")]); });
        act(() => {
            result.current.commit();
            result.current.setNodes([node("a"), node("b")]);
        });
        expect(result.current.canUndo).toBe(true);

        act(() => { result.current.resetHistory(); });
        expect(result.current.canUndo).toBe(false);
        expect(result.current.canRedo).toBe(false);
    });
});
