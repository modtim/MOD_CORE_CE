/**
 * Tests for the WorkflowBuilder undo/redo history stack (lib/workflow-history).
 *
 * Covers the pure reducer that backs the canvas history:
 *   - commit pushes the pre-mutation snapshot and clears the redo branch
 *   - undo/redo move snapshots between past and future correctly
 *   - depth is capped (oldest dropped) at HISTORY_LIMIT
 *   - snapshots are cloned so stored history is insulated from later mutation
 *   - samePositions detects a no-op drag
 */

import type { Node, Edge } from "reactflow";
import {
    createHistory,
    commitHistory,
    undo,
    redo,
    canUndo,
    canRedo,
    cloneSnapshot,
    samePositions,
    HISTORY_LIMIT,
    type GraphSnapshot,
} from "../lib/workflow-history";

const node = (id: string, x = 0, y = 0, data: Record<string, unknown> = {}): Node => ({
    id,
    position: { x, y },
    data,
    type: "process",
});

const edge = (id: string, source: string, target: string): Edge => ({
    id,
    source,
    target,
});

const snap = (nodes: Node[], edges: Edge[] = []): GraphSnapshot => ({ nodes, edges });

describe("createHistory", () => {
    it("starts empty with nothing to undo or redo", () => {
        const h = createHistory();
        expect(h.past).toEqual([]);
        expect(h.future).toEqual([]);
        expect(canUndo(h)).toBe(false);
        expect(canRedo(h)).toBe(false);
    });
});

describe("commitHistory", () => {
    it("pushes the pre-mutation snapshot onto past and flags undo available", () => {
        const s0 = snap([node("a")]);
        const h = commitHistory(createHistory(), s0);
        expect(h.past).toHaveLength(1);
        expect(canUndo(h)).toBe(true);
        expect(canRedo(h)).toBe(false);
    });

    it("clears the redo branch when a new commit is recorded", () => {
        // Build a history with a redo entry, then commit again.
        let h = commitHistory(createHistory(), snap([node("a")]));
        const afterUndo = undo(h, snap([node("a"), node("b")]));
        expect(afterUndo).not.toBeNull();
        h = afterUndo!.history;
        expect(canRedo(h)).toBe(true);

        h = commitHistory(h, snap([node("a")]));
        expect(canRedo(h)).toBe(false);
        expect(h.future).toEqual([]);
    });

    it("does not mutate the input history (pure)", () => {
        const h0 = createHistory();
        const h1 = commitHistory(h0, snap([node("a")]));
        expect(h0.past).toHaveLength(0);
        expect(h1).not.toBe(h0);
    });

    it("caps depth at HISTORY_LIMIT, dropping the oldest entries", () => {
        let h = createHistory();
        for (let i = 0; i < HISTORY_LIMIT + 10; i++) {
            h = commitHistory(h, snap([node(`n${i}`)]));
        }
        expect(h.past).toHaveLength(HISTORY_LIMIT);
        // Oldest surviving entry is n10 (the first 10 were dropped).
        expect(h.past[0].nodes[0].id).toBe("n10");
        expect(h.past[h.past.length - 1].nodes[0].id).toBe(`n${HISTORY_LIMIT + 9}`);
    });

    it("respects a custom limit", () => {
        let h = createHistory();
        for (let i = 0; i < 5; i++) h = commitHistory(h, snap([node(`n${i}`)]), 3);
        expect(h.past).toHaveLength(3);
        expect(h.past[0].nodes[0].id).toBe("n2");
    });
});

describe("undo / redo", () => {
    it("undo returns null when there is nothing to undo", () => {
        expect(undo(createHistory(), snap([]))).toBeNull();
    });

    it("redo returns null when there is nothing to redo", () => {
        expect(redo(createHistory(), snap([]))).toBeNull();
    });

    it("undo restores the previous state and makes the current state redoable", () => {
        // Model an add: pre-state s0 = [a]; after add live = [a, b].
        const s0 = snap([node("a")]);
        const live = snap([node("a"), node("b")]);
        const h = commitHistory(createHistory(), s0);

        const u = undo(h, live);
        expect(u).not.toBeNull();
        expect(u!.snapshot.nodes.map((n) => n.id)).toEqual(["a"]); // add undone
        expect(canUndo(u!.history)).toBe(false);
        expect(canRedo(u!.history)).toBe(true);

        // Redo re-applies the add: current (post-undo) live = [a].
        const r = redo(u!.history, s0);
        expect(r).not.toBeNull();
        expect(r!.snapshot.nodes.map((n) => n.id)).toEqual(["a", "b"]);
        expect(canRedo(r!.history)).toBe(false);
        expect(canUndo(r!.history)).toBe(true);
    });

    it("supports multi-level undo then redo in LIFO order", () => {
        // Three sequential edits. present after each commit is the live state.
        const states = [
            snap([node("a")]),                                   // s0 (before edit 1)
            snap([node("a"), node("b")]),                        // s1 (before edit 2)
            snap([node("a"), node("b"), node("c")]),             // s2 (before edit 3)
        ];
        const live = snap([node("a"), node("b"), node("c"), node("d")]); // s3 current

        let h = createHistory();
        h = commitHistory(h, states[0]);
        h = commitHistory(h, states[1]);
        h = commitHistory(h, states[2]);

        // Undo three times: d, c, b removed → back to [a].
        let cur = live;
        const seen: string[][] = [];
        for (let i = 0; i < 3; i++) {
            const u = undo(h, cur)!;
            h = u.history;
            cur = u.snapshot;
            seen.push(cur.nodes.map((n) => n.id));
        }
        expect(seen).toEqual([
            ["a", "b", "c"],
            ["a", "b"],
            ["a"],
        ]);
        expect(canUndo(h)).toBe(false);
        expect(canRedo(h)).toBe(true);

        // Redo three times climbs back to s3.
        const redone: string[][] = [];
        for (let i = 0; i < 3; i++) {
            const r = redo(h, cur)!;
            h = r.history;
            cur = r.snapshot;
            redone.push(cur.nodes.map((n) => n.id));
        }
        expect(redone).toEqual([
            ["a", "b"],
            ["a", "b", "c"],
            ["a", "b", "c", "d"],
        ]);
        expect(canRedo(h)).toBe(false);
    });

    it("models deletion: undo restores the deleted node and its edge", () => {
        // Pre-delete state has node b and edge a->b; after delete live has only a.
        const preDelete = snap([node("a"), node("b")], [edge("e1", "a", "b")]);
        const live = snap([node("a")], []);
        const h = commitHistory(createHistory(), preDelete);

        const u = undo(h, live)!;
        expect(u.snapshot.nodes.map((n) => n.id)).toEqual(["a", "b"]);
        expect(u.snapshot.edges.map((e) => e.id)).toEqual(["e1"]);
    });
});

describe("cloneSnapshot", () => {
    it("isolates stored snapshots from later mutation of node position/data", () => {
        const original = snap([node("a", 1, 2, { count: 1 })]);
        const h = commitHistory(createHistory(), original);

        // Mutate the ORIGINAL live objects after committing.
        original.nodes[0].position.x = 999;
        (original.nodes[0].data as { count: number }).count = 42;

        expect(h.past[0].nodes[0].position.x).toBe(1);
        expect((h.past[0].nodes[0].data as { count: number }).count).toBe(1);
    });

    it("produces new array and object references", () => {
        const s = snap([node("a")], [edge("e1", "a", "a")]);
        const c = cloneSnapshot(s);
        expect(c.nodes).not.toBe(s.nodes);
        expect(c.nodes[0]).not.toBe(s.nodes[0]);
        expect(c.edges[0]).not.toBe(s.edges[0]);
        expect(c.nodes[0].id).toBe("a");
    });
});

describe("samePositions", () => {
    it("is true when nothing moved (a click, not a drag)", () => {
        const before = [node("a", 10, 20), node("b", 30, 40)];
        const after = [node("a", 10, 20), node("b", 30, 40)];
        expect(samePositions(before, after)).toBe(true);
    });

    it("is false when a node moved", () => {
        const before = [node("a", 10, 20)];
        const after = [node("a", 11, 20)];
        expect(samePositions(before, after)).toBe(false);
    });

    it("is false when the node set changed", () => {
        expect(samePositions([node("a")], [node("a"), node("b")])).toBe(false);
    });
});
