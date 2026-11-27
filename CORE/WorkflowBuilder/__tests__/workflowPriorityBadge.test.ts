/**
 * UI-PRIORITY-CLASS-BADGE rework — the Workflow Builder shows the workflow's
 * priority class READ-ONLY (tasks-line EARLY.WORKFLOW-PRIORITY-CLASS-SETTABLE
 * clause "show it in the Dashboard AND Workflow Builder"; the Dashboard half
 * was green at 798ffb41b7, the WB half was unmet — closer F-2).
 *
 * Contract:
 *  - PriorityClassChip returns null for 'normal' (the API GET default),
 *    unknown, null, undefined and non-string values — an ordinary run carries
 *    no badge; it returns a styled span ONLY for interactive/high/low.
 *  - useWorkflowApi reads GET /road/{id}/priority-class (the NB2 route,
 *    commit 699f0e1e39), fail-tolerant, and exports priorityClass.
 *  - workflow-builder.tsx renders the chip in the workflow-name pill.
 *  - No settable priority UI, no cap, no licence gate in the WB half.
 *
 * The chip is asserted on the component's return value directly (null vs a
 * React element descriptor), so no DOM helper import is needed.
 */
import { PriorityClassChip, normalizePriorityClass } from "@/components/priority-class-chip";
import fs from "fs";
import path from "path";

const HOOK_SRC = fs.readFileSync(path.resolve(__dirname, "../hooks/useWorkflowApi.ts"), "utf-8");
const BUILDER_SRC = fs.readFileSync(path.resolve(__dirname, "../components/workflow-builder.tsx"), "utf-8");

function renderChip(priorityClass: unknown) {
  return PriorityClassChip({ priorityClass } as any);
}

describe("PriorityClassChip render contract", () => {
  it("returns a violet span for interactive", () => {
    const node = renderChip("interactive");
    expect(node).notToBeNull;
    const dump = JSON.stringify(node);
    expect(dump).toContain("bg-violet-600");
    expect(dump).toContain("interactive");
  });
  it("returns a span for high and low", () => {
    for (const cls of ["high", "low"]) {
      const node = renderChip(cls);
      expect(node).notToBeNull;
      expect(JSON.stringify(node)).toContain(cls);
    }
  });
  it("returns null for normal (the API default), unknown, null, undefined", () => {
    for (const v of ["normal", "NORMAL ", "bogus", null, undefined]) {
      expect(renderChip(v)).toBeNull();
    }
  });
  it("normalizePriorityClass whitelists the three badged classes, case-insensitive", () => {
    expect(normalizePriorityClass(" Interactive ")).toBe("interactive");
    expect(normalizePriorityClass("normal")).toBeNull();
    expect(normalizePriorityClass(42)).toBeNull();
  });
  it("title text says the classes do NOT make an ordinary run start faster", () => {
    const node = renderChip("high");
    expect(JSON.stringify(node)).toMatch(/does NOT make an ordinary run start faster/);
  });
});

describe("WB wiring (static)", () => {
  it("useWorkflowApi reads the NB2 GET route and exports priorityClass", () => {
    expect(HOOK_SRC).toMatch(/UI-PRIORITY-CLASS-BADGE rework/);
    expect(HOOK_SRC).toMatch(/const \[priorityClass, setPriorityClass\] = useState<string \| null>\(null\);/);
    expect(HOOK_SRC).toMatch(/await fetch\(\s*`\$\{API_URL\}\/road\/\$\{wfId\}\/priority-class`/);
    expect(HOOK_SRC).toMatch(/setPriorityClass\(null\); return;/);
    expect(HOOK_SRC).toMatch(/catch \{ setPriorityClass\(null\); \}/);
    expect(HOOK_SRC).toMatch(/sealedWorkflowId,\s*priorityClass,/);
  });
  it("workflow-builder.tsx renders the chip in the workflow-name pill", () => {
    expect(BUILDER_SRC).toMatch(/import \{ PriorityClassChip \} from "\.\/priority-class-chip";/);
    expect(BUILDER_SRC).toMatch(/<PriorityClassChip priorityClass=\{api\.priorityClass\} \/>/);
  });
  it("no settable priority UI in the WB half (read-only)", () => {
    expect(!/priority-class[^]{0,120}method:\s*"PUT"/.test(HOOK_SRC)).toBe(true);
    expect(!/priorityClass[^]{0,80}PUT/.test(BUILDER_SRC)).toBe(true);
  });
});

describe("CPU4-UI-REPRO 11-25: priority-class lookup must never end the session", () => {
  const start = HOOK_SRC.indexOf("const [priorityClass, setPriorityClass]");
  const block = HOOK_SRC.slice(start, HOOK_SRC.indexOf("// Fetch output folders after workflow completion"));
  it("sends the Bearer token (a header-less call 401s on every workflow open)", () => {
    expect(block).toMatch(/Authorization: `Bearer \$\{accessToken\}`/);
  });
  it("waits for the access token before fetching", () => {
    expect(block).toMatch(/if \(!wfId \|\| !accessToken\)/);
  });
  it("does not use authenticatedFetch (its 401 path hard-redirects to the Dashboard)", () => {
    expect(block).not.toMatch(/authenticatedFetch/);
  });
});
