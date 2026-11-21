/**
 * BUG-1027j — canvas placement for keyboard-added nodes (lib/canvas-placement.ts
 * `nextFreePosition`, used by hooks/useWorkflowEvents.ts `addNodeAtCenter`).
 *
 * A palette Enter-add used to land on the exact viewport centre every time, so
 * the 2nd node covered the 1st. These tests pin the new rule: walk RIGHT from
 * the requested point by a fixed stride until the overlap box around the
 * candidate holds no existing node, with a bounded search.
 */

import { nextFreePosition } from "@/lib/canvas-placement";

const at = (x: number, y: number = 0) => ({ position: { x, y } });

describe("nextFreePosition (BUG-1027j)", () => {
    it("P1 empty canvas — the start is returned unchanged", () => {
        expect(nextFreePosition({ x: 120, y: 40 }, [])).toEqual({ x: 120, y: 40 });
    });

    it("P2 one node on the start — step right by the default stride, same y", () => {
        expect(
            nextFreePosition({ x: 0, y: 0 }, [at(0)])
        ).toEqual({ x: 300, y: 0 });
    });

    it("P3 nodes at the start, +300 and +600 — land on +900", () => {
        const nodes = [at(0), at(300), at(600)];
        expect(nextFreePosition({ x: 0, y: 0 }, nodes)).toEqual({ x: 900, y: 0 });
    });

    it("P4 a node far away (y + 500) — the start is unchanged", () => {
        expect(
            nextFreePosition({ x: 0, y: 0 }, [at(0, 500)])
        ).toEqual({ x: 0, y: 0 });
    });

    it("P5 every slot occupied — bounded search, finite result, no hang", () => {
        const nodes = Array.from({ length: 20 }, (_, i) => at(i * 300));
        const result = nextFreePosition({ x: 0, y: 0 }, nodes);
        expect(Number.isFinite(result.x)).toBe(true);
        expect(Number.isFinite(result.y)).toBe(true);
        expect(result).toEqual({ x: 6000, y: 0 });
    });

    it("P6 the overlap box is honoured — 279 blocks, 281 does not", () => {
        // |279| < boxW(280) -> the start is occupied, and +300 is only 21 away
        // from the node, so the first free slot is +600.
        expect(nextFreePosition({ x: 0, y: 0 }, [at(279)])).toEqual({ x: 600, y: 0 });
        // |281| >= boxW -> outside the box, the start is still free.
        expect(nextFreePosition({ x: 0, y: 0 }, [at(281)])).toEqual({ x: 0, y: 0 });
    });
});
