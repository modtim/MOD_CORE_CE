/**
 * GE.WQR.15 — WizardRunsSection: self-fetches via usePollingBackoff (no
 * setInterval — see lib/poller.ts), then applies two client-side filters
 * on top of the already-server-scoped list: drop `wizard_type === "node"`
 * (those render via BuildQueueRow, not here) and drop anything failing
 * `isOwnScope` (defence-in-depth; the real gate is server-side).
 *
 * `listWizardRuns` is mocked directly (rather than mocking
 * authenticatedFetch) so this test exercises the section's own filtering
 * logic without re-testing lib/wizard-queue.ts's HTTP plumbing.
 */

import React from "react";
import { act, render, screen } from "@testing-library/react";
import WizardRunsSection from "@/components/wizard-runs-section";
import type { WizardRun } from "@/lib/wizard-queue";

jest.mock("@/lib/wizard-queue", () => {
  const actual = jest.requireActual("@/lib/wizard-queue");
  return {
    ...actual,
    listWizardRuns: jest.fn(),
    getWizardRun: jest.fn(),
  };
});

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { listWizardRuns, getWizardRun } = jest.requireMock("@/lib/wizard-queue") as {
  listWizardRuns: jest.Mock;
  getWizardRun: jest.Mock;
};

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

describe("WizardRunsSection", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    listWizardRuns.mockReset();
    getWizardRun.mockReset();
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("renders the section testid and an 'Other wizard runs' subheading", async () => {
    listWizardRuns.mockResolvedValue({ runs: [], total_count: 0 });
    render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("wizard-runs-section")).toBeInTheDocument();
    expect(screen.getByText("Other wizard runs")).toBeInTheDocument();
  });

  it("shows the empty state when no in-scope runs are returned", async () => {
    listWizardRuns.mockResolvedValue({ runs: [], total_count: 0 });
    render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("No wizard runs.")).toBeInTheDocument();
  });

  it("filters out wizard_type=node rows (those render via BuildQueueRow instead)", async () => {
    listWizardRuns.mockResolvedValue({
      runs: [
        makeRun({ id: "node-run", wizard_type: "node", target_name: "node-one" }),
        makeRun({ id: "stack-run", wizard_type: "stack", target_name: "stack-one" }),
      ],
      total_count: 2,
    });
    render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(screen.queryByText("node-one")).not.toBeInTheDocument();
    expect(screen.getByText("stack-one")).toBeInTheDocument();
  });

  it("filters out rows failing isOwnScope for the supplied viewer", async () => {
    listWizardRuns.mockResolvedValue({
      runs: [
        makeRun({ id: "mine", wizard_type: "stack", target_name: "mine-run", tenant_id: 5 }),
        makeRun({ id: "theirs", wizard_type: "stack", target_name: "theirs-run", tenant_id: 6 }),
      ],
      total_count: 2,
    });
    render(
      <WizardRunsSection
        accessToken="tok"
        apiUrl="http://localhost:8000"
        viewer={{ tenantId: 5 }}
      />,
    );
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("mine-run")).toBeInTheDocument();
    expect(screen.queryByText("theirs-run")).not.toBeInTheDocument();
  });

  it("does not poll when accessToken is absent", async () => {
    listWizardRuns.mockResolvedValue({ runs: [], total_count: 0 });
    render(<WizardRunsSection accessToken={null} apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });
    expect(listWizardRuns).not.toHaveBeenCalled();
  });

  // ----------------------------------------------------------------
  // Bell deep-link focus (#wizard-queue/<id>) + retry-of reveal
  // ----------------------------------------------------------------

  function scrollTarget(rowId: string) {
    return document.querySelector<HTMLElement>(`[data-run-id="${rowId}"]`);
  }

  it("focusRunId scrolls to and highlights an in-list run", async () => {
    listWizardRuns.mockResolvedValue({
      runs: [makeRun({ id: "focus-me", target_name: "focused" })],
      total_count: 1,
    });
    jest.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    const { rerender } = render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(scrollTarget("focus-me")).toBeTruthy();

    rerender(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" focusRunId="focus-me" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(100);
    });
    expect(scrollTarget("focus-me")).toHaveClass("ring-2");
  });

  it("focusRunId fetches + prepends a run not in the list via the single-run endpoint", async () => {
    listWizardRuns.mockResolvedValue({ runs: [], total_count: 0 });
    getWizardRun.mockResolvedValue(makeRun({ id: "fetched-1", target_name: "fetched" }));
    jest.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    render(
      <WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" focusRunId="fetched-1" />,
    );
    await act(async () => {
      await jest.advanceTimersByTimeAsync(200);
    });
    expect(getWizardRun).toHaveBeenCalledWith("fetched-1", "http://localhost:8000");
    expect(screen.getByText("fetched")).toBeInTheDocument();
    expect(scrollTarget("fetched-1")).toHaveClass("ring-2");
  });

  it("focusRunId leaves the list untouched when the single-run endpoint 404s", async () => {
    listWizardRuns.mockResolvedValue({ runs: [], total_count: 0 });
    getWizardRun.mockRejectedValue(new Error("fetch run failed (404)"));
    jest.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    render(
      <WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" focusRunId="gone" />,
    );
    await act(async () => {
      await jest.advanceTimersByTimeAsync(200);
    });
    expect(getWizardRun).toHaveBeenCalledWith("gone", "http://localhost:8000");
    expect(screen.queryByText(/fetched|gone/)).not.toBeInTheDocument();
    expect(screen.getByText("No wizard runs.")).toBeInTheDocument();
  });

  it("retry-of back-link fetches the original run when it is not in the list and highlights it", async () => {
    listWizardRuns.mockResolvedValue({
      runs: [makeRun({ id: "child", retry_of_id: "orig-9", target_name: "child-run" })],
      total_count: 1,
    });
    getWizardRun.mockResolvedValue(makeRun({ id: "orig-9", target_name: "orig-run" }));
    const scrollSpy = jest.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      screen.getByRole("button", { name: /retry of/ }).click();
      await jest.advanceTimersByTimeAsync(200);
    });
    expect(getWizardRun).toHaveBeenCalledWith("orig-9", "http://localhost:8000");
    expect(screen.getByText("orig-run")).toBeInTheDocument();
    expect(scrollSpy).toHaveBeenCalled();
  });

  it("retry-of back-link downgrades to plain text when the original run 404s", async () => {
    listWizardRuns.mockResolvedValue({
      runs: [makeRun({ id: "child", retry_of_id: "gone-9", target_name: "child-run" })],
      total_count: 1,
    });
    getWizardRun.mockRejectedValue(new Error("fetch run failed (404)"));
    jest.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    render(<WizardRunsSection accessToken="tok" apiUrl="http://localhost:8000" />);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      screen.getByRole("button", { name: /retry of/ }).click();
      await jest.advanceTimersByTimeAsync(50);
    });
    // After the failed fetch the back-link must be plain text, not a button.
    expect(screen.queryByRole("button", { name: /retry of/ })).not.toBeInTheDocument();
    expect(screen.getByText(/retry of gone/)).toBeInTheDocument();
  });
});
