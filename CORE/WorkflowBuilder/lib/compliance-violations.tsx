/**
 * Compliance-violation parsing & display for WorkflowBuilder save errors.
 *
 * Parses the 422 response body from the compose-time policy-evaluation gate
 * (`workflow_cud.py` FORTRESS 5.2 block) and renders a structured ViolationList.
 *
 * Server violation shape (compliance_policy_evaluator.evaluate_policies):
 *   { policy_key?, version?, framework?, rule, message, node_id?, task_id? }
 *
 * Possible `rule` values:
 *   - actual policy rules (e.g. "egress-must-be-declared")
 *   - "gate_error"          — rego compile / evaluation failure
 *   - "engine_unavailable"  — regopy import failed (one-shot)
 */

"use client";

import * as React from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ComplianceViolation {
  policy_key: string | null;
  version: number | null;
  framework: string | null;
  rule: string;
  message: string;
  node_id: string | null;
  task_id: number | null;
}

// Internal payload shape from the 422 detail body.
interface ComplianceErrorBody {
  error: string;
  violations: Record<string, unknown>[];
  engine_unavailable?: boolean;
}

// ---------------------------------------------------------------------------
// Pure parser
// ---------------------------------------------------------------------------

/**
 * Parse the HTTP error body into a list of ComplianceViolations.
 *
 * Returns `null` when the response body does not match the expected
 * compliance-policy error structure (other 422s, 500s, malformed JSON).
 *
 * Also handles the engine-unavailable variant where `engine_unavailable`
 * is set to true but the violations array may be empty (the single
 * engine_unavailable violation was emitted server-side).
 */
export function parseComplianceViolationError(
  status: number,
  body: unknown,
): ComplianceViolation[] | null {
  if (status !== 422) return null;
  if (!body || typeof body !== "object") return null;

  // FastAPI's default HTTPException handler (no custom handler is installed
  // for this router — see api_conventions.install_error_handlers, which is
  // a scaffold not yet wired into main.py) wraps a dict `detail=` under a
  // top-level "detail" key: the real wire shape is
  // `{"detail": {"error": "compliance_policy_eval_failed", "violations": [...]}}`.
  // Unwrap it here; also accept the bare inner shape defensively.
  const outer = body as { detail?: unknown };
  const inner =
    outer.detail && typeof outer.detail === "object" ? outer.detail : body;
  const payload = inner as ComplianceErrorBody;

  // The compose-time gate uses error === "compliance_policy_eval_failed".
  if (payload.error !== "compliance_policy_eval_failed") return null;

  if (!Array.isArray(payload.violations)) return null;

  // If the engine was unavailable and violations are empty, emit the
  // single synthetic violation so the user still sees *something*.
  const violations: Record<string, unknown>[] = payload.violations;
  if (
    payload.engine_unavailable &&
    violations.length === 0
  ) {
    return [
      {
        policy_key: null,
        version: null,
        framework: null,
        rule: "engine_unavailable",
        message:
          "Policy evaluation engine is unavailable. Compliance checks are running in shadow mode.",
        node_id: null,
        task_id: null,
      },
    ];
  }

  const result: ComplianceViolation[] = [];

  for (const raw of violations) {
    if (!raw || typeof raw !== "object") continue;

    result.push({
      policy_key: _str(raw, "policy_key"),
      version: _num(raw, "version"),
      framework: _str(raw, "framework"),
      rule: _str(raw, "rule") || "unknown",
      message: _str(raw, "message") || "",
      node_id: _str(raw, "node_id"),
      task_id: _num(raw, "task_id"),
    });
  }

  return result;
}

// ---------------------------------------------------------------------------
// Tiny helpers
// ---------------------------------------------------------------------------

function _str(o: Record<string, unknown>, key: string): string | null {
  const v = o[key];
  if (v === undefined || v === null) return null;
  return typeof v === "string" ? v : String(v);
}

function _num(o: Record<string, unknown>, key: string): number | null {
  const v = o[key];
  if (v === undefined || v === null) return null;
  return typeof v === "number" ? v : null;
}

// ---------------------------------------------------------------------------
// ViolationList component
// ---------------------------------------------------------------------------

interface ViolationListProps {
  violations: ComplianceViolation[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Renders a modal dialog with one row per violation.
 *
 * Columns: framework/policy_key, rule, message (with node_id / task_id
 * suffixes when present).
 */
export function ViolationList({
  violations,
  open,
  onOpenChange,
}: ViolationListProps) {
  const items = violations ?? [];
  const policyCount = new Set(
    items
      .filter((v) => v.policy_key)
      .map((v) => v.policy_key!),
  ).size;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Compliance Violations ({items.length})</DialogTitle>
          <DialogDescription>
            {items.length} violation{items.length !== 1 ? "s" : ""} from{" "}
            {policyCount} polic{items.length === 1 ? "y" : "ies"}.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-2">
          {items.map((v, idx) => (
            <ViolationRow key={idx} violation={v} />
          ))}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Single violation row
// ---------------------------------------------------------------------------

interface ViolationRowProps {
  violation: ComplianceViolation;
}

function ViolationRow({ violation }: ViolationRowProps) {
  const { rule, message, framework, policy_key, node_id, task_id } = violation;

  // Badge colour by rule type.
  let badgeVariant: "default" | "destructive" | "secondary" | null = "destructive";
  if (rule === "gate_error") {
    badgeVariant = "secondary";
  }
  if (rule === "engine_unavailable") {
    badgeVariant = "secondary";
  }

  const location = [node_id, task_id != null ? `task ${task_id}` : null]
    .filter(Boolean)
    .join(": ");

  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
      <div className="flex items-center gap-2 mb-1">
        {framework && (
          <Badge variant="outline" className="text-xs">
            {framework}
          </Badge>
        )}
        {policy_key && (
          <span className="text-xs text-muted-foreground truncate max-w-[200px]">
            {policy_key}
          </span>
        )}
        {badgeVariant && (
          <Badge variant={badgeVariant} className="text-xs">
            {rule}
          </Badge>
        )}
      </div>
      <p className="text-sm text-red-900">{message}</p>
      {location && (
        <p className="text-xs text-muted-foreground mt-1">{location}</p>
      )}
    </div>
  );
}
