/**
 * Q9=B cold deep link — the PURE decision layer + sessionStorage plumbing.
 *
 * No mocks of the modules under test: these are the real exports of
 * lib/api-client.ts (parseWorkflowIdParam / decideColdLinkOutcome / the
 * pending-id stash) and lib/silent-sso.ts (the silent-SSO context + the
 * one-shot loop guard). Nothing here touches a token: the only values ever
 * stored are the bare integer workflow id and two booleans.
 */

import {
  IDENTITY_LOGGED_OUT,
  parseWorkflowIdParam,
  decideColdLinkOutcome,
  stashPendingWorkflowId,
  peekPendingWorkflowId,
  consumePendingWorkflowId,
} from "@/lib/api-client";
import {
  SSO_RETURN_TO_STORAGE_KEY,
  SSO_PRIOR_LOGIN_STORAGE_KEY,
  consumeSsoContext,
  markSsoAttempted,
  consumeSsoAttempted,
} from "@/lib/silent-sso";

beforeEach(() => {
  sessionStorage.clear();
});

describe("parseWorkflowIdParam (strict id parse)", () => {
  it("accepts a bare positive integer", () => {
    expect(parseWorkflowIdParam("?workflow=42")).toBe(42);
  });

  it("rejects every junk value", () => {
    expect(parseWorkflowIdParam("?workflow=0")).toBeNull();
    expect(parseWorkflowIdParam("?workflow=-1")).toBeNull();
    expect(parseWorkflowIdParam("?workflow=1e3")).toBeNull();
    expect(parseWorkflowIdParam("?workflow= 7")).toBeNull();
    expect(parseWorkflowIdParam("?workflow=0x10")).toBeNull();
    expect(parseWorkflowIdParam("?workflow=7abc")).toBeNull();
    expect(parseWorkflowIdParam("")).toBeNull();
    // beyond Number.isSafeInteger
    expect(parseWorkflowIdParam("?workflow=99999999999999999999")).toBeNull();
  });

  it("no workflow param at all -> null", () => {
    expect(parseWorkflowIdParam("?token=irrelevant")).toBeNull();
  });

  it("duplicated param: URLSearchParams.get gives the FIRST value", () => {
    expect(parseWorkflowIdParam("?workflow=7&workflow=8")).toBe(7);
  });
});

describe("decideColdLinkOutcome truth table", () => {
  it("no id -> login regardless of the rest", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: null, priorLogin: false, identityCookie: null })).toBe("login");
    expect(decideColdLinkOutcome({ pendingWorkflowId: null, priorLogin: true, identityCookie: "sub-a" })).toBe("login");
  });

  it("id + explicit logout sentinel + priorLogin -> login (logout is NOT stale)", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: 5, priorLogin: true, identityCookie: IDENTITY_LOGGED_OUT })).toBe("login");
  });

  it("id + priorLogin true + cookie absent -> denied (stale tab token)", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: 5, priorLogin: true, identityCookie: null })).toBe("denied");
  });

  it("id + priorLogin false + cookie present -> denied (browser logged in before)", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: 5, priorLogin: false, identityCookie: "some-sub" })).toBe("denied");
  });

  it("id + priorLogin false + cookie absent -> login (never-logged-in visitor)", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: 5, priorLogin: false, identityCookie: null })).toBe("login");
  });

  it("id + priorLogin false + empty cookie -> login (no identity signal)", () => {
    expect(decideColdLinkOutcome({ pendingWorkflowId: 5, priorLogin: false, identityCookie: "" })).toBe("login");
  });
});

describe("pending deep-link id stash", () => {
  it("stash then peek keeps the id; consume returns it once", async () => {
    stashPendingWorkflowId(7);
    expect(peekPendingWorkflowId()).toBe(7);
    expect(peekPendingWorkflowId()).toBe(7);
    expect(consumePendingWorkflowId()).toBe(7);
    expect(peekPendingWorkflowId()).toBeNull();
  });

  it("a hand-poisoned value is re-validated on read -> null", () => {
    sessionStorage.setItem("workflow_pending_open_id", "7; rm");
    expect(peekPendingWorkflowId()).toBeNull();
  });

  it("the stored value is the bare integer string (no token, no URL)", () => {
    stashPendingWorkflowId(7);
    expect(sessionStorage.getItem("workflow_pending_open_id")).toBe("7");
  });
});

describe("silent-SSO context + one-shot loop guard", () => {
  it("markSsoAttempted(true) -> consumeSsoAttempted gives { priorLogin: true } once", () => {
    markSsoAttempted(true);
    expect(consumeSsoAttempted()).toEqual({ priorLogin: true });
    expect(consumeSsoAttempted()).toBeNull();
  });

  it("consumeSsoContext reads + clears BOTH context keys", () => {
    sessionStorage.setItem(SSO_RETURN_TO_STORAGE_KEY, "/?workflow=9");
    sessionStorage.setItem(SSO_PRIOR_LOGIN_STORAGE_KEY, "1");
    expect(consumeSsoContext()).toEqual({ returnTo: "/?workflow=9", priorLogin: true });
    expect(sessionStorage.getItem(SSO_RETURN_TO_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(SSO_PRIOR_LOGIN_STORAGE_KEY)).toBeNull();
  });
});

describe("security pin: nothing token-shaped is ever stored", () => {
  it("after every stash/mark call, sessionStorage holds no 'token' substring", () => {
    stashPendingWorkflowId(7);
    markSsoAttempted(true);
    sessionStorage.setItem(SSO_RETURN_TO_STORAGE_KEY, "/?workflow=9");
    sessionStorage.setItem(SSO_PRIOR_LOGIN_STORAGE_KEY, "1");
    const dump = JSON.stringify(Object.entries(sessionStorage));
    expect(dump).not.toContain("token");
    expect(dump).not.toContain("Bearer");
  });
});
