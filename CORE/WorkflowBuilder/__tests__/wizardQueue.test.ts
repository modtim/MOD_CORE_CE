/**
 * GE.WQR.13 — lib/wizard-queue.ts unit tests: the platform-level-only
 * scope filter (`isOwnScope`, defence-in-depth only — the server already
 * scopes via services/wizard_queue.py::scope_query) and the type/status
 * label maps consumed by the row/section components.
 */

import {
  isOwnScope,
  WIZARD_TYPE_LABEL,
  WIZARD_STATUS_LABEL,
  WIZARD_TERMINAL_STATUSES,
  WIZARD_RETRYABLE_STATUSES,
  parseWizardQueueHash,
  wizardArtifactTarget,
  type WizardRun,
} from "@/lib/wizard-queue";

function makeRun(overrides: Partial<WizardRun> = {}): WizardRun {
  return {
    id: "run-1",
    wizard_type: "stack",
    target_name: "demo",
    target_ref_type: null,
    target_ref_id: null,
    status: "running",
    progress: 10,
    started_at: null,
    ended_at: null,
    duration_seconds: null,
    error_message: null,
    owner_user_id: 1,
    tenant_id: 5,
    is_platform_level: false,
    origin: "tenant-user",
    retry_of_id: null,
    parent_run_id: null,
    log_source: null,
    created_at: "2026-09-25T00:00:00Z",
    updated_at: "2026-09-25T00:00:00Z",
    can_cancel: true,
    can_retry: false,
    ...overrides,
  };
}

describe("isOwnScope", () => {
  it("is permissive when no viewer is supplied (trusts server scoping)", () => {
    expect(isOwnScope(makeRun(), undefined)).toBe(true);
    expect(isOwnScope(makeRun(), null)).toBe(true);
  });

  it("platform admin sees ONLY platform-level runs, never a tenant's — even their own tenant", () => {
    const platformRun = makeRun({ is_platform_level: true, tenant_id: null, owner_user_id: null });
    const tenantRun = makeRun({ is_platform_level: false, tenant_id: 5 });
    const admin = { isPlatformAdmin: true, tenantId: 5, userId: 1 };
    expect(isOwnScope(platformRun, admin)).toBe(true);
    expect(isOwnScope(tenantRun, admin)).toBe(false);
  });

  it("non-platform-admin never sees a platform-level run", () => {
    const platformRun = makeRun({ is_platform_level: true });
    expect(isOwnScope(platformRun, { userId: 1, tenantId: 5 })).toBe(false);
  });

  it("tenant admin sees runs scoped to their tenant only", () => {
    const own = makeRun({ tenant_id: 5 });
    const other = makeRun({ tenant_id: 6 });
    const viewer = { tenantId: 5 };
    expect(isOwnScope(own, viewer)).toBe(true);
    expect(isOwnScope(other, viewer)).toBe(false);
  });

  it("plain user falls back to owner_user_id when tenant is unavailable", () => {
    const own = makeRun({ tenant_id: null, owner_user_id: 42 });
    const other = makeRun({ tenant_id: null, owner_user_id: 99 });
    const viewer = { userId: 42 };
    expect(isOwnScope(own, viewer)).toBe(true);
    expect(isOwnScope(other, viewer)).toBe(false);
  });

  it("falls back permissively when neither tenant nor user id can be compared", () => {
    const run = makeRun({ tenant_id: null, owner_user_id: null });
    expect(isOwnScope(run, { isPlatformAdmin: false })).toBe(true);
  });
});

describe("WIZARD_TYPE_LABEL", () => {
  it("has a human label for every wizard type", () => {
    const types: WizardRun["wizard_type"][] = [
      "node",
      "stack",
      "agent-project",
      "build-repair",
      "container-adapter",
    ];
    for (const t of types) {
      expect(WIZARD_TYPE_LABEL[t]).toEqual(expect.any(String));
      expect(WIZARD_TYPE_LABEL[t].length).toBeGreaterThan(0);
    }
  });
});

describe("WIZARD_STATUS_LABEL / terminal / retryable sets", () => {
  it("has a human label for every status", () => {
    const statuses: WizardRun["status"][] = [
      "queued",
      "waiting-for-worker",
      "running",
      "verifying",
      "waiting-for-user",
      "succeeded",
      "failed",
      "canceled",
    ];
    for (const s of statuses) {
      expect(WIZARD_STATUS_LABEL[s]).toEqual(expect.any(String));
    }
  });

  it("terminal statuses match models.WIZARD_TERMINAL_STATUSES", () => {
    expect([...WIZARD_TERMINAL_STATUSES].sort()).toEqual(
      ["succeeded", "failed", "canceled"].sort(),
    );
  });

  it("retryable statuses match models.WIZARD_RETRYABLE_STATUSES", () => {
    expect([...WIZARD_RETRYABLE_STATUSES].sort()).toEqual(["failed", "canceled"].sort());
  });
});

describe("parseWizardQueueHash", () => {
  it("parses a bell deep-link hash into the run id", () => {
    expect(parseWizardQueueHash("#wizard-queue/abc-123")).toBe("abc-123");
  });

  it("tolerates a hash without the leading #", () => {
    expect(parseWizardQueueHash("wizard-queue/abc-123")).toBe("abc-123");
  });

  it("returns null for non-wizard-queue hashes and empty input", () => {
    expect(parseWizardQueueHash("#project/abc")).toBeNull();
    expect(parseWizardQueueHash("#home")).toBeNull();
    expect(parseWizardQueueHash("")).toBeNull();
    // No id after the slash → not a valid deep-link.
    expect(parseWizardQueueHash("#wizard-queue")).toBeNull();
  });
});

describe("wizardArtifactTarget", () => {
  it("maps project and task refs to openable targets", () => {
    // CE: no Project Wizard -> a project ref has no openable view (hides Open) ()
    expect(wizardArtifactTarget("project", "sess-1")).toBeNull();
    expect(wizardArtifactTarget("task", "42")).toEqual({ kind: "worker-task", id: "42" });
  });

  it("returns null when either ref field is missing", () => {
    expect(wizardArtifactTarget("project", null)).toBeNull();
    expect(wizardArtifactTarget(null, "sess-1")).toBeNull();
    expect(wizardArtifactTarget(null, null)).toBeNull();
  });

  it("returns null for ref types with no existing view (button hidden, not no-op)", () => {
    expect(wizardArtifactTarget("build_spec", "12")).toBeNull();
    expect(wizardArtifactTarget("stack-prefill", "x")).toBeNull();
    expect(wizardArtifactTarget("node", "7")).toBeNull();
    expect(wizardArtifactTarget("workflow", "w-1")).toBeNull();
  });
});
