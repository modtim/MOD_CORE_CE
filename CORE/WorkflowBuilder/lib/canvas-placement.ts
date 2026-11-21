/**
 * BUG-1027j — canvas placement for keyboard-added nodes.
 *
 * The palette's Enter-add (`hooks/useWorkflowEvents.ts` `addNodeAtCenter`)
 * used to drop every node on the exact viewport centre, so the second node
 * covered the first and a keyboard user could never reach the earlier ones
 * (the FX.68.p3 a11y path; it also stalled the CE golden-path Playwright
 * spec, which could not click a node buried under another).
 *
 * This module answers "where does the next node go?": starting at the
 * requested point, step RIGHT by a fixed stride until no existing node's
 * position lies inside the overlap box around the candidate. The search is
 * bounded by `maxSteps`; when every slot in the window is taken the last
 * candidate is returned, so the caller always gets a finite, usable position
 * instead of looping forever.
 *
 * Pure — no React, no React Flow — so it is unit-testable without a canvas,
 * in the same style as `lib/canvas-a11y.ts`.
 */

export function nextFreePosition(
    start: { x: number; y: number },
    nodes: Array<{ position: { x: number; y: number } }>,
    opts?: { stepX?: number; boxW?: number; boxH?: number; maxSteps?: number },
): { x: number; y: number } {
    const stepX = opts?.stepX ?? 300;
    const boxW = opts?.boxW ?? 280;
    const boxH = opts?.boxH ?? 160;
    const maxSteps = opts?.maxSteps ?? 20;

    let candidate = { x: start.x, y: start.y };
    for (let step = 0; step < maxSteps; step += 1) {
        const occupied = nodes.some((node) => (
            Math.abs(node.position.x - candidate.x) < boxW &&
            Math.abs(node.position.y - candidate.y) < boxH
        ));
        if (!occupied) return candidate;
        candidate = { x: candidate.x + stepX, y: candidate.y };
    }

    // Every slot in the window is taken — hand back the last candidate rather
    // than looping. It is still finite and still on the row the user is looking
    // at; fitView keeps it in view.
    return candidate;
}
