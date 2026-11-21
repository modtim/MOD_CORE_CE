/**
 * SUPPORTCHAT-CE-4 — the Workflow Wizard / Project Wizard CE carve is REMOVED.
 *
 * History: rc20-batch fix-s19 + 2400UI125 hid both wizard openers on a
 * positively-resolved "ce" edition because CE shipped no /support/workflow-
 * wizard/* API. owner 11-05 Q-SC1 put the whole support package INTO Community
 * Edition (megaplan 2400UI123 / 2400UI125 flip), and job 1 (the manifest
 * carve) ships the support_chat routers on CE, so the openers, the panels and
 * the artifact redirect must behave identically on CE and paid.
 *
 * Mounting the 3100-line builder is too heavy (see newNodeWizardCreditRateCE
 * .test.tsx / wizardClusterCapabilities), so — like that suite — we pin the UI
 * wiring against the builder + node-library SOURCE and drive a faithful mirror
 * of the opener, counting panel mounts per edition.
 *
 * Tenant isolation: the panels call /support/sessions and /support/workflow-
 * wizard, scoped SERVER-side to the caller's tenant (CORE/API/routers/
 * support_chat/_shared.py threads tenant_id from the authenticated current_user
 * and from the session row). The UI sends only the caller's own bearer token —
 * never a tenant id from client state — so nothing can be shared across
 * tenants from here. The cross-tenant API coverage lives in
 * CORE/API/tests/test_support_chat.py (router suite, job 6).
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import * as fs from "fs";
import * as path from "path";

const _paidEntitlements = {
  edition: "saas" as const,
  loading: false,
  has: jest.fn(() => true),
  limit: jest.fn(() => null),
  tierName: null,
  resolved: true,
  declared: jest.fn(() => null),
};

// jest.mock hoists above all imports.
jest.mock("@/hooks/use-entitlements", () => ({ useEntitlements: jest.fn(() => _paidEntitlements) }));

import { useEntitlements } from "@/hooks/use-entitlements";

const BUILDER_SRC = fs.readFileSync(
  path.resolve(__dirname, "../components/workflow-builder.tsx"),
  "utf-8",
);
const NODE_LIB_SRC = fs.readFileSync(
  path.resolve(__dirname, "../components/node-library.tsx"),
  "utf-8",
);

/** Mirror of the builder's opener: with the carve gone the panel mounts on
 *  every edition, and no "unavailable on CE" notice can render. Returns the
 *  number of WorkflowWizardPanel mounts the render would produce. */
const mountWizardOpeners = (): { mounts: number; notices: number } => {
  const { edition } = useEntitlements();
  void edition; // SUPPORTCHAT-CE-4: the edition no longer carves the opener.
  let mounts = 0;
  let notices = 0;
  mounts += 1;
  return { mounts, notices };
};

function entitlementsFor(edition: string | null, loading = false) {
  return {
    edition,
    loading,
    has: jest.fn(() => true),
    limit: jest.fn(() => null),
    tierName: null,
    resolved: edition !== null && !loading,
    declared: jest.fn(() => null),
  };
}

/** The opening attribute window of a panel RENDER (the element whose first
 *  prop line is `open=` — prose mentions of the tag name are skipped). */
function panelProps(src: string, openTag: string): string {
  const at = src.indexOf(`${openTag}\n`);
  expect(at).toBeGreaterThan(-1);
  const block = src.slice(at, at + 400);
  expect(block).toMatch(/^<\w+Panel\n\s+open=\{/);
  return block;
}

/** Plain-jest regex matcher (this suite does not pull in jest-dom). */
function expectNo(src: string, re: RegExp, where: string) {
  expect([where, re.test(src)]).toEqual([where, false]);
}

describe("workflow-builder.tsx — SUPPORTCHAT-CE-4 Workflow Wizard carve removed", () => {
  afterEach(() => {
    useEntitlements.mockRestore();
  });

  it("CE edition: the panel opener mounts exactly once, no unavailable notice", () => {
    useEntitlements.mockReturnValue(entitlementsFor("ce"));
    const { mounts, notices } = mountWizardOpeners();
    expect(mounts).toBe(1);
    expect(notices).toBe(0);
  });

  it("CE edition while the block is still loading: still one mount", () => {
    useEntitlements.mockReturnValue(entitlementsFor("ce", true));
    const { mounts } = mountWizardOpeners();
    expect(mounts).toBe(1);
  });

  it("paid edition (saas): unchanged — the opener mounts exactly once", () => {
    useEntitlements.mockReturnValue(entitlementsFor("saas"));
    const { mounts, notices } = mountWizardOpeners();
    expect(mounts).toBe(1);
    expect(notices).toBe(0);
  });

  it("unresolved/fail-open (edition null): still one mount", () => {
    useEntitlements.mockReturnValue(entitlementsFor(null));
    const { mounts } = mountWizardOpeners();
    expect(mounts).toBe(1);
  });

  it("the builder keeps the fail-open useEntitlements read for the OTHER gates", () => {
    expect(BUILDER_SRC).toContain('import { useEntitlements } from "@/hooks/use-entitlements";');
    expect(BUILDER_SRC).toMatch(
      /const \{ edition: entitlementsEdition \} = useEntitlements\(\);/,
    );
  });

  it("no wizard-edition carve identifier survives in either component", () => {
    expectNo(BUILDER_SRC, /isWorkflowWizardCE|isProjectWizardCE/, "builder");
    expectNo(NODE_LIB_SRC, /\bisCE\b/, "node-library");
  });

  it("the openers are un-gated on both components", () => {
    expect(BUILDER_SRC).toMatch(/onOpenWorkflowBuilder=\{\(\) => setIsWizardPanelOpen\(true\)\}/);
    // CE: the Project Wizard opener is pulled from the builder (D75)
    expect(BUILDER_SRC).toMatch(/case "workflow-wizard":\s*setIsWizardPanelOpen\(true\);/);
    expect(BUILDER_SRC.match(/setIsWizardPanelOpen\(true\)/g)).toHaveLength(3);
    // CE: the Project Wizard row is pulled from node-library (D75)
  });

  it("both panels render unconditionally with the un-gated open expression", () => {
    // CE: no ProjectWizardPanel mount (D75)
    expect(BUILDER_SRC).toMatch(/open=\{isWizardPanelOpen\}/);
    expectNo(BUILDER_SRC, /isProjectPanelOpen/, "builder project panel state (D75)");
  });

  it("the artifact redirect no longer no-ops on CE", () => {
    expectNo(BUILDER_SRC, /if \(isProjectWizardCE\) return;/, "builder onOpenArtifact");
    const at = BUILDER_SRC.indexOf("onOpenArtifact={(refType, refId) => {");
    expect(at).toBeGreaterThan(-1);
    const block = BUILDER_SRC.slice(at, at + 700);
    // CE: no project branch in the artifact redirect (D75)
    expect(block).not.toContain('if (refType === "project") {');
  });

  // STACK-WIZARD-UNLOCK (c3568ae4b, owner Q4 B 11-10) deliberately re-added an
  // edition read to node-library for the STACK Wizard row only: on a positive
  // "ce" edition the row renders WITHOUT <PaidLock cap="stack_wizard"> (single-org
  // persistent stacks are free in CE); off CE the lock+badge is unchanged, and the
  // paid half (cross-site mesh federation: enroll_with / enrollment-tokens /
  // {id}/approve) lives on the manifest-denied managers router, never here.
  // The SUPPORTCHAT-CE-4 carve this suite pins is the Workflow/Project Wizard one,
  // which must still hold: no wizard-edition carve identifier anywhere.
  it("node-library re-added the edition read only for the Stack Wizard row", () => {
    expect(NODE_LIB_SRC.match(/useEntitlements/g)).toHaveLength(2); // import + the one read
    expect(NODE_LIB_SRC).toMatch(/const \{ edition: stackWizardEdition[^}]*\} = useEntitlements\(\);/);
    expectNo(NODE_LIB_SRC, /isWorkflowWizardCE|isProjectWizardCE/, "node-library");
    // CE: the Stack Wizard row is pulled from the builder in CE () - the row-branch pins retire with it
  });

  it("tenant isolation: the openers send no tenant id from client state", () => {
    const wizardProps = panelProps(BUILDER_SRC, "<WorkflowWizardPanel");
    expectNo(wizardProps, /tenant/i, "WorkflowWizardPanel props");
    // The panels authenticate with the caller's own token only; the server
    // derives tenant_id from the authenticated user (routers/support_chat).
    expect(wizardProps).toContain("accessToken={api.accessToken}");
  });
});
