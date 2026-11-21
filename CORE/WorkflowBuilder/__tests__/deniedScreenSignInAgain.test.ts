/**
 * Q41 (Q9=B follow-up, owner 11-05): on the Workflow Builder's access-denied
 * screen, a user with NO valid login (a saved login went stale and they
 * opened a `/?workflow=<id>` link cold) must see "Sign in again" instead of
 * "Request access" — requestWorkflowAccess (hooks/useWorkflowApi.ts) returns
 * false without a token, so "Request access" would do nothing.
 *
 * Two layers are pinned:
 *  - the DIALOG BRANCH in components/workflow-builder.tsx (source pin — the
 *    real WB component cannot be rendered in jsdom; this is the established
 *    pattern in this repo, see newNodeWizardNotConfiguredBanner.test.ts /
 *    bug-b61-no-jsx-text-comment.test.ts), and
 *  - the CLICK BEHAVIOUR of lib/sign-in-again.ts (real module + the real
 *    parseWorkflowIdParam validator from lib/api-client; only the Keycloak
 *    redirect itself is mocked).
 *
 * The "no valid login" predicate is the SAME token gate the Q9 code uses:
 * `accessToken` from hooks/useWorkflowApi.ts (the gate `if (!accessToken)
 * return;` at useOpenWorkflowFromQuery.ts:51 and initializeAuth's token
 * checks). Nothing here invents a new auth check.
 */

import * as fs from "fs";
import * as path from "path";

const startSilentSsoMock = jest.fn();
jest.mock("@/lib/silent-sso", () => ({
  startSilentSso: (...args: unknown[]) => startSilentSsoMock(...args),
}));
const getDashboardUrlMock = jest.fn(() => "https://dashboard.test");
jest.mock("@/lib/urls", () => ({
  getDashboardUrl: (...args: unknown[]) => getDashboardUrlMock(...args),
}));

import {
  validatedWorkflowReturnTarget,
  startSignInAgain,
} from "@/lib/sign-in-again";
import { peekPendingWorkflowId } from "@/lib/api-client";

const SOURCE_FILE = path.resolve(__dirname, "../components/workflow-builder.tsx");
const source = fs.readFileSync(SOURCE_FILE, "utf-8");

function deniedDialogBlock(): string {
  const start = source.indexOf("{/* No-access to workflow");
  const end = source.indexOf("{/* Run blocked");
  if (start === -1 || end === -1 || end < start) return "";
  return source.slice(start, end);
}

beforeEach(() => {
  sessionStorage.clear();
  startSilentSsoMock.mockReset();
  startSilentSsoMock.mockReturnValue(true);
});

describe("denied screen — Sign in again when there is no valid login", () => {
  it("(1) no token -> 'Sign in again' shown, 'Request access' not shown", () => {
    const block = deniedDialogBlock();
    expect(block).not.toBe("");
    expect(block).toContain("api.accessToken == null");
    expect(block).toContain("Sign in again");
    expect(block).toContain("Request access");
    // The no-token branch (between `? (` and `) : (`) is the ONLY place
    // "Sign in again" appears, and it must not offer Request access.
    const guard = block.indexOf("api.accessToken == null");
    const noTokenBranch = block.slice(guard, block.indexOf(") : (", guard));
    expect(noTokenBranch).toContain("Sign in again");
    expect(noTokenBranch).not.toContain("Request access");
    expect(noTokenBranch).toContain("startSignInAgain(wfId)");
    expect(source).toContain('import { startSignInAgain } from "@/lib/sign-in-again"');
  });

  it("(1b) the click starts the Q9 login redirect carrying the original workflow link", async () => {
    const started = await startSignInAgain(42);
    expect(started).toBe(true);
    expect(startSilentSsoMock).toHaveBeenCalledTimes(1);
    const [returnTo, opts] = startSilentSsoMock.mock.calls[0];
    // Same return-target form Q9 passes to startSilentSso (useWorkflowApi.ts:563),
    // rebuilt from the validated integer so the user lands back on the link.
    expect(returnTo).toBe("/?workflow=42");
    expect(opts).toEqual({ priorLogin: true });
    // Loop guard: the second argument must be exactly { priorLogin: true }.
    expect(startSilentSsoMock).toHaveBeenCalledWith("/?workflow=42", { priorLogin: true });
    // Security: no token/code/verifier/state in the navigation target.
    expect(String(returnTo)).not.toMatch(/token|Bearer|code|verifier|state/i);
  });

  it("(1c) silent SSO unavailable -> Q9 cold-link fallback: id stashed, Dashboard login", async () => {
    startSilentSsoMock.mockReturnValue(false);
    const started = await startSignInAgain(42);
    expect(started).toBe(true);
    // The bare integer (never a token, never a URL) survives the login round trip.
    expect(peekPendingWorkflowId()).toBe(42);
    // jsdom does not navigate cross-origin on `location.href` assignment, so
    // the Dashboard navigation is pinned on the seam (the same URL Q9's
    // initializeAuth uses: window.location.href = DASHBOARD_URL).
    expect(getDashboardUrlMock).toHaveBeenCalledTimes(1);
  });

  it("(2) valid token -> 'Request access' shown, no 'Sign in again'", () => {
    const block = deniedDialogBlock();
    const guard = block.indexOf("api.accessToken == null");
    expect(guard).toBeGreaterThan(-1);
    const tokenBranch = block.slice(block.indexOf(") : (", guard));
    expect(tokenBranch).toContain("Request access");
    expect(tokenBranch).toContain("api.requestWorkflowAccess(wfId)");
    expect(tokenBranch).not.toContain("Sign in again");
  });

  it("(3) return target that is not same-origin is refused (Q9 validator + origin guard)", async () => {
    expect(validatedWorkflowReturnTarget("//evil.example/?workflow=42")).toBeNull();
    expect(validatedWorkflowReturnTarget("https://evil.example/?workflow=42")).toBeNull();
    expect(validatedWorkflowReturnTarget("http://evil/redirect?workflow=7")).toBeNull();
    expect(validatedWorkflowReturnTarget("javascript:alert(1)?workflow=7")).toBeNull();
    // Junk ids are refused by the Q9 validator itself (lib/api-client.ts).
    expect(validatedWorkflowReturnTarget("/?workflow=0")).toBeNull();
    expect(validatedWorkflowReturnTarget("/?workflow=1e3")).toBeNull();
    expect(validatedWorkflowReturnTarget("/?workflow=7abc")).toBeNull();
    expect(validatedWorkflowReturnTarget(null)).toBeNull();

    const started = await startSignInAgain(null);
    expect(started).toBe(false);
    expect(startSilentSsoMock).not.toHaveBeenCalled();
    expect(peekPendingWorkflowId()).toBeNull();
  });
});
