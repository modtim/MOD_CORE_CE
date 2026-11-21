/**
 * rc20-golden — the node context menu (right-click popover) is `fixed` at the
 * raw right-click point. On a node near the canvas edge the deep run-control
 * items — the block ends with "Output versions…" — render past the bottom /
 * right edge, so Playwright reports the button "outside of the viewport" and
 * the click never lands (wb-ce-golden.spec.ts:122, CE room cr14).
 *
 * Witness: the menu anchor must be clamped back inside the viewport. This test
 * fails on the pre-fix source (no shift state, no clamp, raw top/left only).
 */

import * as fs from "fs";
import * as path from "path";

const SRC = path.resolve(__dirname, "../components/workflow-builder.tsx");
const src = fs.readFileSync(SRC, "utf-8");

describe("node context menu stays inside the viewport", () => {
  test("the menu carries a shift state distinct from the raw anchor", () => {
    expect(src).toMatch(/const \[nodeCtxMenuShift, setNodeCtxMenuShift\] = useState\(\{ dx: 0, dy: 0 \}\)/);
  });

  test("the shift is measured from the rendered menu box", () => {
    expect(src).toMatch(/nodeCtxMenuContainerRef\.current[\s\S]{0,400}?getBoundingClientRect\(\)/);
  });

  test("the clamp reads the viewport size", () => {
    expect(src).toMatch(/window\.innerWidth/);
    expect(src).toMatch(/window\.innerHeight/);
  });

  test("both overflow directions are clamped (bottom and right)", () => {
    expect(src).toMatch(/if \(by \+ bh > vh - EDGE\) dy = vh - EDGE - \(by \+ bh\);/);
    expect(src).toMatch(/if \(bx \+ bw > vw - EDGE\) dx = vw - EDGE - \(bx \+ bw\);/);
  });

  test("the top/left underflow is clamped too (menu never hangs off the top)", () => {
    expect(src).toMatch(/if \(by < EDGE\) dy = Math\.max\(dy, EDGE - by\);/);
    expect(src).toMatch(/if \(bx < EDGE\) dx = Math\.max\(dx, EDGE - bx\);/);
  });

  test("the shift converges: it is measured against the UNSHIFTED box", () => {
    expect(src).toMatch(/const bx = rect\.left - nodeCtxMenuShift\.dx;/);
    expect(src).toMatch(/const by = rect\.top - nodeCtxMenuShift\.dy;/);
  });

  test("the menu style applies the shift, so the raw anchor alone is not the position", () => {
    expect(src).toMatch(/transform: `translate\(\$\{nodeCtxMenuShift\.dx\}px, \$\{nodeCtxMenuShift\.dy\}px\)`,/);
  });

  test("the shift is reset when the menu closes (no stale offset on the next open)", () => {
    expect(src).toMatch(/setNodeCtxMenuShift\(\{ dx: 0, dy: 0 \}\);/);
  });

  test("the clamp effect re-runs on shift change (one extra render, then stable)", () => {
    expect(src).toMatch(/\}, \[nodeCtxMenu, nodeCtxMenuShift\]\);/);
  });
});
