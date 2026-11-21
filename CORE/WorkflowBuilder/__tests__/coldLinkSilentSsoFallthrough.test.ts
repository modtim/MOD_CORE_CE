/**
 * FIX-WB-COLD-LINK-DENIED-BEFORE-SILENT-SSO (Q9=B follow-up).
 *
 * `startSilentSso` (lib/silent-sso.ts) returns true as soon as the auth URL
 * BUILDS - it never proves a token landed for this principal. The old
 * unconditional `if (started) return;` in hooks/useWorkflowApi.ts initializeAuth
 * made the cold-link decide block (and the access-denied dialog) unreachable
 * whenever the round-trip never landed, so a stale-login cold deep link painted
 * a blank canvas instead of the denial (api.accessDenied stayed null, so the
 * dialog at components/workflow-builder.tsx never opened).
 *
 * Source-pin pattern (no mocks of the module under test, no DB, no mount - the
 * real WB component cannot render in jsdom; see coldLinkRenderGate.test.ts and
 * deniedScreenSignInAgain.test.ts): read hooks/useWorkflowApi.ts with fs and
 * assert on the control flow of the `back === null` branch.
 */

import * as fs from "fs";
import * as path from "path";

const SRC = path.resolve(__dirname, "../hooks/useWorkflowApi.ts");
const src = fs.readFileSync(SRC, "utf-8");

// The branch under test: from the loop guard to the Dashboard bounce.
const branchStart = src.indexOf("const back = consumeSsoAttempted();");
const branchEnd = src.indexOf("window.location.href = DASHBOARD_URL;", branchStart);
const branch = branchStart >= 0 && branchEnd > branchStart ? src.slice(branchStart, branchEnd) : "";

const startedCallIdx = src.indexOf("await startSilentSso(returnTo, { priorLogin })");
const decideIdx = src.indexOf("const outcome = decideColdLinkOutcome({");
const denyIdx = src.indexOf("setAccessDenied({ workflowId: pendingId");
const landedReturnIdx = src.indexOf("if (landed) return;");
const restoreIdx = src.indexOf("window.location.href = wbOrigin + returnTo;");
const originIdx = src.indexOf("const wbOrigin = window.location.origin;");
const loopIdx = src.indexOf("while (Date.now() < landingDeadline)");
const tokenProbeIdx = src.indexOf("if (getAccessToken()) { landed = true; break; }");

describe("cold link - a started-but-unlanded silent SSO falls through to the denial", () => {
  it("(1) the unconditional `if (started) return;` is gone and the decide block is reachable", () => {
    expect(branchStart).toBeGreaterThan(0);
    expect(branchEnd).toBeGreaterThan(branchStart);
    expect(startedCallIdx).toBeGreaterThan(0);
    expect(decideIdx).toBeGreaterThan(startedCallIdx);
    // The old shape is gone verbatim.
    expect(src).not.toContain("if (started) return;");
    // Between the round-trip and the decide, the ONLY exit is the guarded one
    // (a token actually landed). A bare `return;` here re-opens the hole.
    const span = src.slice(startedCallIdx, decideIdx);
    const returns = span.split("return;").length - 1;
    expect(returns).toBe(1);
    expect(span).toContain("if (landed) return;");
    expect(span).toContain("} else {");
  });

  it("(2) the denial still names the workflow GENERICALLY (`workflow ${pendingId}`)", () => {
    expect(denyIdx).toBeGreaterThan(0);
    expect(src).toContain("setAccessDenied({ workflowId: pendingId, workflowName: `workflow ${pendingId}` })");
    // No real name on the token-less path: the denial block must not reach a
    // workflow_name / real-name field.
    const denialBlock = src.slice(denyIdx, denyIdx + 160);
    expect(denialBlock).not.toContain("workflow_name");
    expect(denialBlock).not.toContain("workflowName: workflowName");
    expect(denialBlock).not.toContain("realName");
  });

  it("(3) the owner path still returns after a LANDED round-trip (no deny-before-token regression)", () => {
    expect(tokenProbeIdx).toBeGreaterThan(startedCallIdx);
    expect(landedReturnIdx).toBeGreaterThan(tokenProbeIdx);
    // The owner wins before any denial is possible: the landed return precedes
    // the setAccessDenied call, and no denial happens before the round-trip
    // has landed or timed out.
    expect(landedReturnIdx).toBeLessThan(denyIdx);
    expect(startedCallIdx).toBeLessThan(denyIdx);
    // Bounded wait, not an unbounded one.
    expect(loopIdx).toBeGreaterThan(startedCallIdx);
    expect(loopIdx).toBeLessThan(tokenProbeIdx);
    expect(src).toContain("SILENT_SSO_LANDING_BUDGET_MS");
    expect(src).toContain("const SILENT_SSO_LANDING_BUDGET_MS =");
  });

  it("(4) the WB origin plus returnTo is restored before the fall-through decide", () => {
    expect(originIdx).toBeGreaterThan(0);
    expect(originIdx).toBeLessThan(startedCallIdx);
    expect(restoreIdx).toBeGreaterThan(tokenProbeIdx);
    expect(restoreIdx).toBeLessThan(decideIdx);
    // The link the visitor aimed at is what gets restored (never a bare origin,
    // never the Dashboard).
    expect(src).toContain("window.location.href = wbOrigin + returnTo;");
    expect(src).toContain("const returnTo = window.location.pathname + window.location.search;");
  });

  it("(5) the pendingId parse and the cookie read still feed decideColdLinkOutcome", () => {
    const pendingIdIdx = src.indexOf("const pendingId = parseWorkflowIdParam(window.location.search);");
    expect(pendingIdIdx).toBeGreaterThan(0);
    expect(pendingIdIdx).toBeLessThan(startedCallIdx);
    const decideBlock = src.slice(decideIdx, decideIdx + 200);
    expect(decideBlock).toContain("pendingWorkflowId: pendingId,");
    expect(decideBlock).toContain("priorLogin,");
    expect(decideBlock).toContain("identityCookie: readActiveIdentityCookie(),");
    expect(denyIdx).toBeGreaterThan(decideIdx);
    expect(src).toContain('if (outcome === "denied" && pendingId !== null)');
  });

  it("(6) the loop guard is unchanged: silent SSO is never started a second time", () => {
    expect(branchStart).toBeGreaterThan(0);
    expect(startedCallIdx).toBeGreaterThan(branchStart);
    expect(src).toContain("const back = consumeSsoAttempted();");
    expect(src).toContain("priorLogin = back.priorLogin;");
    // The guard branch still skips startSilentSso entirely.
    const guardIdx = src.indexOf("if (back === null) {");
    expect(guardIdx).toBeGreaterThan(0);
    expect(guardIdx).toBeLessThan(startedCallIdx);
  });

  it("(7) the change is additive: existing denial call sites and dialog wiring unchanged", () => {
    // The denial reuses the existing setAccessDenied state pair (cold-link call
    // + the three API 403 paths) and the existing dialog - no new key, no new
    // dialog, no shape change.
    const setAccessDeniedCount = src.split("setAccessDenied(").length - 1;
    expect(setAccessDeniedCount).toBe(4);
    expect(src).toContain("const [accessDenied, setAccessDenied] = useState<");
    expect(src).not.toContain("accessDeniedDialog");
    // No new sessionStorage key invented for the fall-through: the branch only
    // reads window.location and the token accessors.
    const branchSpan = src.slice(branchStart, branchEnd);
    expect(branchSpan).not.toContain("sessionStorage");
    expect(branchSpan).not.toContain("localStorage");
  });
});
