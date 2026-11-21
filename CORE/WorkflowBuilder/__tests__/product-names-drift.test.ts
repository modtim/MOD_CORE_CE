/**
 * OZ-E1/E2 drift guard — the WorkflowBuilder copy of the Oz product-name map
 * (CORE/WorkflowBuilder/lib/product-names.ts) must stay equal to the source of
 * truth, CORE/Dashboard/js/product-names.js.
 *
 * Why: the WB app can only resolve imports inside CORE/WorkflowBuilder
 * (`@/*` -> `CORE/WorkflowBuilder/*` in tsconfig.json and the jest moduleNameMapper),
 * so the Dashboard file never reaches the WB image (CORE/WorkflowBuilder/Dockerfile
 * COPYs only the WB tree); the WB tree carries a verbatim data COPY. This test reads
 * the Dashboard map as TEXT (fs) and the WB copy as a module, and asserts the same
 * key set with identical codename + subtitle strings. It goes RED when either file
 * changes alone (mutation proof: change one codename in the WB copy).
 *
 * Also asserts OFF behaviour is unchanged: with the theme switch off
 * (MOD_THEME_NAMES = false / '0' / 'off' / 'no') themeLabel returns the
 * today's-literal it was passed, byte for byte; and with the switch unset an
 * unknown key still returns the passed literal (never blank).
 *
 * Mount note (harness): `r9700-run.sh jest WorkflowBuilder` binds ONLY
 * CORE/WorkflowBuilder as /app, so CORE/Dashboard is not in that tree at all.
 * The test walks up for CORE/Dashboard first (full-tree runs: the laptop clone,
 * an IDE, a whole-repo jest run) and otherwise falls back to the committed
 * snapshot fixture below, which is a byte copy of the Dashboard file and is
 * refreshed by the fix lane whenever the Dashboard map changes.
 */
import * as fs from "fs";
import * as path from "path";

import { PRODUCT_NAMES as WB_PRODUCT_NAMES } from "@/lib/product-names";
import { themeEnabled, themeLabel, WB_NAV_LABELS, wbNavLabel } from "@/lib/product-names";

const FIXTURE = path.resolve(
  __dirname,
  "fixtures",
  "dashboard-product-names.snapshot.js"
);

/**
 * Walk up from this test file until we find the repo root: the first ancestor
 * that contains CORE/Dashboard. Works from the laptop clone and from any
 * full-tree jest run; returns null in a WB-only mount.
 */
function findDashboardMapFile(start: string): string | null {
  let dir = start;
  for (let i = 0; i < 12; i += 1) {
    const candidate = path.join(dir, "CORE", "Dashboard", "js", "product-names.js");
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

/**
 * Extract the map entries from the Dashboard file as TEXT: key -> {codename,
 * subtitle}. Single- and double-quoted strings both appear (the `burst`
 * subtitle contains an apostrophe).
 */
function parseDashboardMap(text: string) {
  const start = text.indexOf("PRODUCT_NAMES");
  const open = text.indexOf("{", start);
  const close = text.indexOf("};", open);
  if (start === -1 || open === -1 || close === -1) {
    throw new Error("product-names-drift: no PRODUCT_NAMES map found");
  }
  const body = text.slice(open, close);
  const entries: Record<string, { codename: string; subtitle: string }> = {};
  const re = /'([a-z0-9_-]+)'\s*:\s*\{\s*codename:\s*(?:"([^"]*)"|'([^']*)')\s*,\s*label:\s*(?:"([^"]*)"|'([^']*)')\s*,\s*subtitle:\s*(?:"([^"]*)"|'([^']*)')/g;
  let m: RegExp.ExecResult | null;
  while ((m = re.exec(body)) !== null) {
    const key = m[1];
    const codename = m[2] !== undefined ? m[2] : m[3];
    const subtitle = m[6] !== undefined ? m[6] : m[7];
    entries[key] = { codename, subtitle };
  }
  if (Object.keys(entries).length === 0) {
    throw new Error("product-names-drift: regex parsed no entries");
  }
  return entries;
}

const LIVE_DASHBOARD = findDashboardMapFile(__dirname);
const MAP_SOURCE = LIVE_DASHBOARD !== null ? LIVE_DASHBOARD : FIXTURE;

describe("OZ product-names drift — WB copy vs Dashboard source of truth", () => {
  it("the WB copy carries the full map (>= 28 entries, all fields set)", () => {
    const keys = Object.keys(WB_PRODUCT_NAMES);
    expect(keys.length).toBeGreaterThanOrEqual(28);
    const incomplete = keys.filter(
      (k) => {
        const e = WB_PRODUCT_NAMES[k];
        return !e.codename || !e.label || !e.subtitle;
      }
    );
    expect(incomplete).toEqual([]);
  });

  it("a map source is reachable (live CORE/Dashboard or the snapshot fixture)", () => {
    expect(fs.existsSync(MAP_SOURCE)).toBe(true);
    if (LIVE_DASHBOARD === null) {
      console.warn(
        "product-names-drift: CORE/Dashboard not in this mount - comparing the " +
          "WB copy against the committed snapshot fixture"
      );
    }
  });

  it("the WB copy has the same key set as the Dashboard map", () => {
    const dashboard = parseDashboardMap(fs.readFileSync(MAP_SOURCE, "utf-8"));
    const wbKeys = Object.keys(WB_PRODUCT_NAMES).sort();
    const dashKeys = Object.keys(dashboard).sort();
    expect(dashKeys.length).toBeGreaterThan(20);
    expect(wbKeys).toEqual(dashKeys);
  });

  it("every WB entry matches the Dashboard codename and subtitle exactly", () => {
    const dashboard = parseDashboardMap(fs.readFileSync(MAP_SOURCE, "utf-8"));
    const mismatches: string[] = [];
    for (const key of Object.keys(dashboard)) {
      const want = dashboard[key];
      const got = WB_PRODUCT_NAMES[key];
      if (!got) {
        mismatches.push(key + ": missing in WB copy");
        continue;
      }
      if (got.codename !== want.codename) {
        mismatches.push(
          key + ": WB codename " + got.codename + " != Dashboard " + want.codename
        );
      }
      if (got.subtitle !== want.subtitle) {
        mismatches.push(
          key + ": WB subtitle " + got.subtitle + " != Dashboard " + want.subtitle
        );
      }
    }
    expect(mismatches).toEqual([]);
  });

  it("theme OFF returns the passed today's literal byte for byte", () => {
    for (const off of [false, "false", "0", "off", "no", "OFF "]) {
      expect(themeEnabled(off)).toBe(false);
      expect(themeLabel("workflow-builder", "Workflow Wizard", off)).toBe(
        "Workflow Wizard"
      );
      expect(themeLabel("marketplace", "Marketplace", off)).toBe("Marketplace");
    }
  });

  it("theme unset on an unknown key still returns the passed literal", () => {
    // jsdom window exists but MOD_THEME_NAMES is not set here -> default ON;
    // an unknown key must fall back to today's literal, never blank.
    expect(themeEnabled(undefined)).toBe(true);
    expect(themeLabel("no-such-key-today", "Workflow Wizard")).toBe(
      "Workflow Wizard"
    );
  });

  it("theme ON renders the map label for a WB key", () => {
    expect(themeLabel("workflow-builder", "Workflow Wizard")).toBe(
      "Toto - Workflows"
    );
    expect(themeLabel("marketplace", "Marketplace")).toBe("Kansas - Marketplace");
  });
});

describe("WB navigation + popup labels (wb-nav-* family, owner 11-17)", () => {
  const EXPECTED: Record<string, string> = {
    "wb-nav-dashboard": "O.Z. - Dashboard",
    "wb-nav-wizard-nodes": "Wizard - Nodes",
    "wb-nav-wizard-flow": "Wizard - Workflows",
    "wb-nav-wizard-stacks": "Wizard - Stacks",
    "wb-nav-wizard-projects": "Wizard - Projects",
    "wb-nav-process-queue": "The Crossroads - Process Queue",
    "wb-nav-approvals": "Guardian - Approvals",
    "wb-nav-wizard-queue": "Behind The Curtain - Wizard Queue",
    "wb-nav-triggers": "The Flying Monkeys - Triggers",
    "wb-nav-node-library": "The Bricks - Node Library",
    "wb-nav-assistant": "Glinda - Mod Assistant",
  };

  it("carries exactly the eleven builder labels (nine nav entries + the node-library heading + the assistant panel, owner 11-17 D75)", () => {
    expect(WB_NAV_LABELS).toEqual(EXPECTED);
  });

  it("does not collide with the Dashboard-copy keys (shared labels untouched)", () => {
    const shared = Object.keys(WB_NAV_LABELS).filter((k) => k in WB_PRODUCT_NAMES);
    expect(shared).toEqual([]);
    expect(Object.keys(WB_NAV_LABELS).every((k) => k.startsWith("wb-nav-"))).toBe(true);
    // the approved Dashboard sidebar entries keep their wording
    expect(WB_PRODUCT_NAMES["workflow-builder"].label).toBe("Toto - Workflows");
    expect(WB_PRODUCT_NAMES["triggers"].label).toBe("Monkeys - Triggers");
  });

  it("theme ON returns the label, theme OFF returns today's literal", () => {
    for (const k of Object.keys(EXPECTED)) {
      expect(wbNavLabel(k, "today")).toBe(EXPECTED[k]);
      expect(wbNavLabel(k, "today", "off")).toBe("today");
    }
    expect(wbNavLabel("wb-nav-no-such-key", "today")).toBe("today");
  });

  // owner 11-17 (): the builder's
  // left-side heading (workflow-builder.tsx:1996) reads its text from this family, not
  // from the Dashboard copy: theme ON -> the new codename line, theme OFF -> today's
  // literal "Node Library" (the heading is never blank). The Dashboard key
  // 'workflow-builder' (label 'Toto - Workflows') is untouched (see the drift tests
  // above), so the Dashboard sidebar and the builder heading no longer share a name.
  it("node-library heading: theme ON -> 'The Bricks - Node Library', OFF -> the literal", () => {
    expect(wbNavLabel("wb-nav-node-library", "Node Library")).toBe(
      "The Bricks - Node Library"
    );
    expect(wbNavLabel("wb-nav-node-library", "Node Library", "off")).toBe("Node Library");
    expect(WB_PRODUCT_NAMES["workflow-builder"].label).toBe("Toto - Workflows");
  });
});
