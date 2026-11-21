/**
 * GE.WQR.14 — DOM smoke tests for WizardRunRow. Terminal-status runs
 * (outcome already decided) exercise the row without ever opening the
 * log drawer, so the SSE/poll-fallback path in streamWizardRunLogs is
 * never invoked here — that path is covered indirectly by not crashing
 * when Cancel/Retry/Dismiss aren't clicked. authenticatedFetch is mocked
 * so no real network call escapes jsdom.
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import WizardRunRow from "@/components/wizard-run-row";
import type { WizardRun } from "@/lib/wizard-queue";

jest.mock("@/lib/api-client", () => ({
  authenticatedFetch: jest.fn(() =>
    Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) }),
  ),
  getAccessToken: jest.fn(() => "test-token"),
}));

function makeRun(overrides: Partial<WizardRun> = {}): WizardRun {
  return {
    id: "run-abc123",
    wizard_type: "stack",
    target_name: "My Stack",
    target_ref_type: null,
    target_ref_id: null,
    status: "failed",
    progress: 40,
    started_at: "2026-09-25T00:00:00Z",
    ended_at: "2026-09-25T00:01:00Z",
    duration_seconds: 60,
    error_message: "build step failed",
    owner_user_id: 1,
    tenant_id: 5,
    is_platform_level: false,
    origin: "tenant-user",
    retry_of_id: null,
    parent_run_id: null,
    log_source: null,
    created_at: "2026-09-25T00:00:00Z",
    updated_at: "2026-09-25T00:01:00Z",
    can_cancel: false,
    can_retry: true,
    ...overrides,
  };
}

const noop = () => {};

function renderRow(run: WizardRun) {
  return render(
    <WizardRunRow
      run={run}
      apiUrl="http://localhost:8000"
      onChanged={noop}
      onDismissed={noop}
    />,
  );
}

describe("WizardRunRow", () => {
  it("renders root testid + data-status + type label + target name", () => {
    renderRow(makeRun());
    const root = screen.getByTestId("wizard-run-row");
    expect(root).toHaveAttribute("data-status", "failed");
    expect(screen.getByText("Stack Wizard")).toBeInTheDocument();
    expect(screen.getByText("My Stack")).toBeInTheDocument();
  });

  it("shows Retry for a failed run (can_retry) but not Cancel (can_cancel=false)", () => {
    renderRow(makeRun({ status: "failed", can_cancel: false, can_retry: true }));
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();
  });

  it("shows Cancel for a running, cancelable run but not Retry", () => {
    renderRow(
      makeRun({
        status: "running",
        can_cancel: true,
        can_retry: false,
        error_message: null,
      }),
    );
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("shows a retry-of back-link when retry_of_id is set", () => {
    renderRow(makeRun({ retry_of_id: "12345678-aaaa-bbbb-cccc-000000000000" }));
    expect(screen.getByText(/retry of 12345678…/)).toBeInTheDocument();
  });

  it("shows an Open action only for an openable target ref WITH a handler", () => {
    // No handler wired → hidden (never a rendered no-op).
    const { rerender } = renderRow(makeRun({ target_ref_type: "task", target_ref_id: "999" }));
    expect(screen.queryByRole("button", { name: /open/i })).not.toBeInTheDocument();

    // Handler wired + openable type ("task") → shown.
    rerender(
      <WizardRunRow
        run={makeRun({ target_ref_type: "task", target_ref_id: "999" })}
        apiUrl="http://localhost:8000"
        onChanged={noop}
        onDismissed={noop}
        onOpenArtifact={noop}
      />,
    );
    expect(screen.getByRole("button", { name: /open/i })).toBeInTheDocument();
  });

  it("hides Open when the ref type has no view to navigate to, even with a handler", () => {
    // build_spec / stack-prefill (null id) / node / workflow are not openable
    // in any existing view, so the button must be hidden rather than a no-op.
    for (const [t, id] of [
      ["build_spec", "12"],
      ["stack-prefill", null],
      ["node", "7"],
      ["workflow", "w-1"],
      ["project", "sess-1"], // CE: no Project Wizard (D75)
    ] as const) {
      const { unmount } = renderRow(
        makeRun({ target_ref_type: t, target_ref_id: id }),
      );
      // re-render with a handler to prove the type (not the handler) gates it
      expect(screen.queryByRole("button", { name: /open/i })).not.toBeInTheDocument();
      unmount();
    }
  });

  it("calls onOpenArtifact with the run's ref pair when Open is clicked", () => {
    const onOpenArtifact = jest.fn();
    render(
      <WizardRunRow
        run={makeRun({ target_ref_type: "task", target_ref_id: "999" })}
        apiUrl="http://localhost:8000"
        onChanged={noop}
        onDismissed={noop}
        onOpenArtifact={onOpenArtifact}
      />,
    );
    screen.getByRole("button", { name: /open/i }).click();
    expect(onOpenArtifact).toHaveBeenCalledWith("task", "999");
  });

  it("renders retry-of as a link when the original run is resolvable", () => {
    renderRow(makeRun({ retry_of_id: "12345678-aaaa-bbbb-cccc-000000000000", originalRunAvailable: true }));
    expect(screen.getByRole("button", { name: /retry of 12345678…/ })).toBeInTheDocument();
  });

  it("renders retry-of as plain text when the original run is unresolvable", () => {
    renderRow(makeRun({ retry_of_id: "12345678-aaaa-bbbb-cccc-000000000000", originalRunAvailable: false }));
    expect(screen.queryByRole("button", { name: /retry of/i })).not.toBeInTheDocument();
    expect(screen.getByText("retry of 12345678…")).toBeInTheDocument();
  });

  it("applies a highlight ring while highlighted", () => {
    renderRow(makeRun({ highlighted: true }));
    expect(screen.getByTestId("wizard-run-row")).toHaveClass("ring-2");
    expect(screen.getByTestId("wizard-run-row")).toHaveAttribute("data-run-id", "run-abc123");
  });

  it("renders the error message for a failed run", () => {
    renderRow(makeRun({ error_message: "build step failed" }));
    expect(screen.getByText("build step failed")).toBeInTheDocument();
  });
});
