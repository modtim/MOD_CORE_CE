/**
 * Task 4.5 (581) — RunRail "Why did this happen?" explain button.
 *
 * Verifies the onExplainRun prop renders a small Info button on terminal
 * runs only, with correct aria-label, and that clicking it calls the
 * callback without selecting the run. Uses the real component with
 * mocked fetch mirroring RunRailPollBackoff.test.tsx setup.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RunRail from "@/components/RunRail";

// Radix DropdownMenu uses pointer/scroll/observer APIs jsdom doesn't
// implement (see __tests__/workflowUnarchive.test.tsx for the same fix).
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

/* ------------------------------------------------------------------ */
/* Helpers — identical to RunRailPollBackoff.test.tsx                  */
/* ------------------------------------------------------------------ */

function makeResponse(status: number, body: unknown = []): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

/*
 * Shared fetch mock that always resolves with the latest value set via
 * setFetchBody(). Each test calls setFetchBody before rendering.
 */
let currentBody: unknown = [];
function setFetchBody(body: unknown): void { currentBody = body; }

function mockFetch(): jest.Mock {
  return jest.fn().mockImplementation(async () => makeResponse(200, currentBody));
}

// Radix DropdownMenu opens on pointer events, not plain fireEvent.click —
// drive it with user-event (same fix as __tests__/workflowUnarchive.test.tsx).
async function openMenu(): Promise<void> {
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  await user.click(screen.getByTitle("View and switch between runs of this workflow"));
}

describe("RunRail explain button", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    currentBody = [];
    fetchMock = mockFetch();
    global.fetch = fetchMock as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("shows a Why did this happen? button on a failed run", async () => {
    setFetchBody([{ id: 1, status: "failed", run_name: "f" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} onExplainRun={() => {}} />,
    );
    // Open dropdown to trigger the refetch that loads runs.
    await openMenu();
    // Wait for the runs list to populate (poller fired, fetch resolved).
    await screen.findByText(/1 recent/);
    // The button should be in the portal DOM.
    const btn = document.querySelector('[aria-label="Explain run 1"]');
    expect(btn).toBeInTheDocument();
  });

  it("shows a Why did this happen? button on a completed run", async () => {
    setFetchBody([{ id: 2, status: "completed", run_name: "c" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} onExplainRun={() => {}} />,
    );
    await openMenu();
    await screen.findByText(/1 recent/);
    const btn = document.querySelector('[aria-label="Explain run 2"]');
    expect(btn).toBeInTheDocument();
  });

  it("does NOT show the button on a running run", async () => {
    setFetchBody([{ id: 3, status: "running", run_name: "r" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} />,
    );
    await openMenu();
    await screen.findByText(/1 recent/);
    const btn = document.querySelector('[aria-label="Explain run 3"]');
    expect(btn).toBeNull();
  });

  it("does NOT show the button when onExplainRun prop is absent", async () => {
    setFetchBody([{ id: 4, status: "failed", run_name: "f" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} onExplainRun={undefined} />,
    );
    await openMenu();
    await screen.findByText(/1 recent/);
    const btn = document.querySelector('[aria-label="Explain run 4"]');
    expect(btn).toBeNull();
  });

  it("clicking the button calls onExplainRun with the run id", async () => {
    const onExplainRun = jest.fn();
    setFetchBody([{ id: 5, status: "failed", run_name: "f" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={() => {}} onExplainRun={onExplainRun} />,
    );
    await openMenu();
    await screen.findByText(/1 recent/);
    const btn = document.querySelector('[aria-label="Explain run 5"]') as HTMLButtonElement;
    fireEvent.click(btn);
    expect(onExplainRun).toHaveBeenCalledWith(5);
  });

  it("clicking the button does NOT call onSelectRun", async () => {
    const onSelectRun = jest.fn();
    setFetchBody([{ id: 6, status: "failed", run_name: "f" }]);
    render(
      <RunRail workflowId={123} accessToken="tok" apiUrl="http://api.test" selectedRunId={null} onSelectRun={onSelectRun} onExplainRun={() => {}} />,
    );
    await openMenu();
    await screen.findByText(/1 recent/);
    const btn = document.querySelector('[aria-label="Explain run 6"]') as HTMLButtonElement;
    fireEvent.click(btn);
    expect(onSelectRun).not.toHaveBeenCalled();
  });
});
