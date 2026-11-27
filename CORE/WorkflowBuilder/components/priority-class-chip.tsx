"use client";

/**
 * UI-PRIORITY-CLASS-BADGE rework — read-only priority-class badge for the
 * Workflow Builder.
 *
 * The NB2 API pair PUT/GET /v1/road/{workflow_id}/priority-class (commit
 * 699f0e1e39) had no Workflow Builder surface (tasks-line clause "show it in
 * the Dashboard AND Workflow Builder"). This chip mirrors the Dashboard Logs
 * chip: the four classes order capacity-queued runs and route interactive
 * tasks through the lighter placement; they do NOT make an ordinary run start
 * faster.
 *
 * FAIL-CLOSED / hidden-by-default: 'normal' is the API default for an unset
 * workflow row, and a missing or unknown class must render NOTHING — only
 * interactive/high/low get a badge. Read-only: no setter lives here (the
 * Dashboard Logs page owns the set control); no cap, no licence gate.
 *
 * React escapes text children, so the class value is HTML-escaped by
 * construction (parity with the Dashboard chip's escapeHtml).
 */
import React from "react";

const CHIP_STYLES: Record<string, string> = {
  interactive: "bg-violet-600 text-white",
  high: "bg-blue-600 text-white",
  low: "bg-gray-300 text-gray-600",
};

export function normalizePriorityClass(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const cls = value.trim().toLowerCase();
  return CHIP_STYLES[cls] ? cls : null;
}

export const PriorityClassChip = ({
  priorityClass,
}: { priorityClass?: string | null }) => {
  const cls = normalizePriorityClass(priorityClass);
  if (!cls) return null;
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded-full ${CHIP_STYLES[cls]}`}
      title={`Priority class: ${cls} — orders capacity-queued runs; does NOT make an ordinary run start faster`}
    >
      {cls}
    </span>
  );
};
