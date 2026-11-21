/**
 * Compliance-violation parser — unit tests.
 *
 * Tests parseComplianceViolationError and the ComplianceViolation interface
 * shape. No React rendering is tested here (that's covered by
 * Integration/WorkflowDialogs tests).
 */

import {
  parseComplianceViolationError,
  type ComplianceViolation,
} from '@/lib/compliance-violations';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeViolation(
  overrides: Partial<ComplianceViolation> = {},
): ComplianceViolation {
  return {
    policy_key: "data.security.network_egress",
    version: 3,
    framework: "NIST-800-53",
    rule: "egress-must-be-declared",
    message: "Node video-render has undetermined egress",
    node_id: "video-render",
    task_id: 42,
    ...overrides,
  };
}

function compliantBody(violations: ComplianceViolation[] | Record<string, unknown>[]): unknown {
  return {
    error: "compliance_policy_eval_failed",
    violations: violations.map((v) => v as unknown as Record<string, unknown>),
  };
}

// ---------------------------------------------------------------------------
// parseComplianceViolationError — happy path
// ---------------------------------------------------------------------------

describe("parseComplianceViolationError", () => {
  it("returns null for non-422 status", () => {
    expect(parseComplianceViolationError(400, {})).toBeNull();
    expect(parseComplianceViolationError(500, {})).toBeNull();
    expect(parseComplianceViolationError(200, {})).toBeNull();
  });

  it("returns null when error field does not match", () => {
    const body = { error: "validation_failed", violations: [] };
    expect(parseComplianceViolationError(422, body)).toBeNull();
  });

  it("returns null when violations is not an array", () => {
    const body = { error: "compliance_policy_eval_failed", violations: "nope" };
    expect(parseComplianceViolationError(422, body)).toBeNull();
  });

  it("returns null for null body", () => {
    expect(parseComplianceViolationError(422, null)).toBeNull();
  });

  it("returns null for non-object body", () => {
    expect(parseComplianceViolationError(422, "string")).toBeNull();
  });

  // ── matching 422 ──────────────────────────────────────────────────

  it("parses a single violation", () => {
    const v = makeViolation({ rule: "egress-must-be-declared" });
    const body = compliantBody([v]);
    const result = parseComplianceViolationError(422, body);

    expect(result).toHaveLength(1);
    expect(result![0]).toEqual(v);
  });

  it("parses a violation wrapped under FastAPI's default {\"detail\": ...} envelope (the real wire shape — no custom HTTPException handler is installed for this router)", () => {
    const v = makeViolation({ rule: "egress-must-be-declared" });
    const wireBody = { detail: compliantBody([v]) };
    const result = parseComplianceViolationError(422, wireBody);

    expect(result).toHaveLength(1);
    expect(result![0]).toEqual(v);
  });

  it("parses multiple violations", () => {
    const v1 = makeViolation({ rule: "rule-a", message: "msg-a", node_id: "n1" });
    const v2 = makeViolation({ rule: "rule-b", message: "msg-b", node_id: "n2" });
    const body = compliantBody([v1, v2]);

    const result = parseComplianceViolationError(422, body);

    expect(result).toHaveLength(2);
    expect(result!.map((r) => r.rule)).toEqual(["rule-a", "rule-b"]);
  });

  it("drops non-object items from the violations array", () => {
    const body: unknown = {
      error: "compliance_policy_eval_failed",
      violations: [
        { rule: "valid" },
        null,
        "string",
        { rule: "also-valid" },
      ],
    };
    const result = parseComplianceViolationError(422, body);
    expect(result).toHaveLength(2);
    expect(result!.map((r) => r.rule)).toEqual(["valid", "also-valid"]);
  });

  it("coerces rule to 'unknown' when missing", () => {
    const body: unknown = {
      error: "compliance_policy_eval_failed",
      violations: [{ rule: undefined }],
    };
    const result = parseComplianceViolationError(422, body);
    expect(result).toHaveLength(1);
    expect(result![0].rule).toBe("unknown");
  });

  it("normalises framework / node_id / task_id to null when absent", () => {
    const body: unknown = {
      error: "compliance_policy_eval_failed",
      violations: [{ rule: "x" }],
    };
    const result = parseComplianceViolationError(422, body);
    expect(result).toHaveLength(1);
    expect(result![0].policy_key).toBeNull();
    expect(result![0].version).toBeNull();
    expect(result![0].framework).toBeNull();
    expect(result![0].node_id).toBeNull();
    expect(result![0].task_id).toBeNull();
  });

  // ── engine_unavailable ────────────────────────────────────────────

  it("emits synthetic violation when engine_unavailable + empty array", () => {
    const body = {
      error: "compliance_policy_eval_failed",
      violations: [],
      engine_unavailable: true,
    };
    const result = parseComplianceViolationError(422, body);
    expect(result).toHaveLength(1);
    expect(result![0].rule).toBe("engine_unavailable");
    expect(result![0].message).toContain("unavailable");
  });

  it("does NOT emit synthetic violation when engine_unavailable but violations present", () => {
    const body = {
      error: "compliance_policy_eval_failed",
      violations: [makeViolation()],
      engine_unavailable: true,
    };
    const result = parseComplianceViolationError(422, body);
    expect(result).toHaveLength(1);
    expect(result![0].rule).toBe("egress-must-be-declared");
  });

  // ── empty violations (success case masquerading as 422) ─────────

  it("returns empty array for 422 with empty violations and no engine_unavailable", () => {
    const body = { error: "compliance_policy_eval_failed", violations: [] };
    const result = parseComplianceViolationError(422, body);
    expect(result).toEqual([]);
  });

  // ── other 422 codes return null ───────────────────────────────────

  it("returns null for generic 422 with different error code", () => {
    const body = { error: "validation_failed", violations: [] };
    expect(parseComplianceViolationError(422, body)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ComplianceViolation interface shape
// ---------------------------------------------------------------------------

describe("ComplianceViolation interface", () => {
  it("allows all fields to be null except rule", () => {
    const v: ComplianceViolation = {
      policy_key: null,
      version: null,
      framework: null,
      rule: "anything",
      message: "",
      node_id: null,
      task_id: null,
    };
    expect(v.rule).toBe("anything");
  });
});
