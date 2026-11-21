/**
 * Tests for the pure copy/paste graph logic (lib/workflow-clipboard).
 *
 * Covers the contract the canvas relies on:
 *   - copySelection snapshots only selected nodes + edges BETWEEN them
 *   - buildPaste mints fresh, collision-free ids, offsets position, selects the
 *     paste, and rewires internal edges onto the new ids
 *   - node.data is deep-cloned so a paste never shares mutable config/files
 */

import type { Edge, Node } from "reactflow";
import { copySelection, buildPaste, PASTE_OFFSET } from "../lib/workflow-clipboard";

function node(id: string, selected: boolean, extra: Partial<Node> = {}): Node {
    return {
        id,
        type: "process",
        position: { x: 100, y: 200 },
        data: { label: id, selectedFiles: [{ name: `${id}.png` }] },
        selected,
        ...extra,
    } as Node;
}

function edge(id: string, source: string, target: string): Edge {
    return { id, source, target, type: "custom" } as Edge;
}

describe("copySelection", () => {
    it("returns null when nothing is selected (native copy left alone)", () => {
        const nodes = [node("a", false), node("b", false)];
        expect(copySelection(nodes, [])).toBeNull();
    });

    it("snapshots only selected nodes", () => {
        const nodes = [node("a", true), node("b", false), node("c", true)];
        const snap = copySelection(nodes, []);
        expect(snap?.nodes.map((n) => n.id)).toEqual(["a", "c"]);
    });

    it("keeps only edges whose BOTH ends are selected", () => {
        const nodes = [node("a", true), node("b", true), node("c", false)];
        const edges = [
            edge("a-b", "a", "b"), // internal -> kept
            edge("b-c", "b", "c"), // to non-copied -> dropped
            edge("c-a", "c", "a"), // from non-copied -> dropped
        ];
        const snap = copySelection(nodes, edges);
        expect(snap?.edges.map((e) => e.id)).toEqual(["a-b"]);
    });

    it("deep-clones node data so the snapshot doesn't alias the live node", () => {
        const nodes = [node("a", true)];
        const snap = copySelection(nodes, [])!;
        (snap.nodes[0].data as any).selectedFiles[0].name = "mutated.png";
        expect((nodes[0].data as any).selectedFiles[0].name).toBe("a.png");
    });
});

describe("buildPaste", () => {
    it("returns null for an empty/absent clipboard", () => {
        expect(buildPaste(null, [])).toBeNull();
        expect(buildPaste({ nodes: [], edges: [] }, [])).toBeNull();
    });

    it("mints new ids, offsets position, and selects the paste", () => {
        const clip = { nodes: [node("a", true)], edges: [] };
        const result = buildPaste(clip, ["a"])!;
        expect(result.nodes).toHaveLength(1);
        const pasted = result.nodes[0];
        expect(pasted.id).not.toBe("a");
        expect(result.idMap["a"]).toBe(pasted.id);
        expect(pasted.position).toEqual({ x: 100 + PASTE_OFFSET, y: 200 + PASTE_OFFSET });
        expect(pasted.selected).toBe(true);
    });

    it("never collides with existing graph ids", () => {
        const clip = { nodes: [node("a", true), node("b", true)], edges: [] };
        const existing = ["a", "b", "process-copy-fixed"];
        const result = buildPaste(clip, existing)!;
        const newIds = result.nodes.map((n) => n.id);
        for (const id of newIds) expect(existing).not.toContain(id);
        expect(new Set(newIds).size).toBe(newIds.length); // unique among themselves
    });

    it("rewires internal edges onto the new node ids", () => {
        const clip = {
            nodes: [node("a", true), node("b", true)],
            edges: [edge("a-b", "a", "b")],
        };
        const result = buildPaste(clip, ["a", "b"])!;
        expect(result.edges).toHaveLength(1);
        const e = result.edges[0];
        expect(e.source).toBe(result.idMap["a"]);
        expect(e.target).toBe(result.idMap["b"]);
        expect(e.id).not.toBe("a-b");
        expect(e.type).toBe("custom"); // edge shape preserved
    });

    it("drops edges whose endpoints aren't both in the paste", () => {
        const clip = {
            nodes: [node("a", true)],
            edges: [edge("a-x", "a", "x")],
        };
        const result = buildPaste(clip, ["a"])!;
        expect(result.edges).toHaveLength(0);
    });

    it("deep-clones data so pasted nodes don't share config with the source", () => {
        const clip = copySelection([node("a", true)], [])!;
        const result = buildPaste(clip, ["a"])!;
        (result.nodes[0].data as any).selectedFiles[0].name = "changed.png";
        expect((clip.nodes[0].data as any).selectedFiles[0].name).toBe("a.png");
    });
});
