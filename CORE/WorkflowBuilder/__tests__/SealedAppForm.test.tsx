/**
 * GG.8 — the Workflow Builder "app form" for SEALED workflows.
 *
 * Covers: the form renders one field per sealed input, Run posts the saved
 * values through the SAME run endpoint every workflow uses, and a 403
 * "sealed" response (e.g. from a stale/racy graph-read call) never crashes
 * the page — it degrades to a friendly message instead.
 */

import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import SealedAppForm from "@/components/workflow/SealedAppForm";

// Heavy/real components this form reuses are replaced with light stand-ins —
// SealedAppForm's own contract (fields render, Run posts, 403 doesn't throw)
// is what's under test here, not WorkflowProgress/AssetPreview internals
// (those own their own test files and are on other agents' file list).
jest.mock("@/components/workflow-progress", () => ({
  WorkflowProgress: ({ workflowRunId }: { workflowRunId: number }) => (
    <div data-testid="mock-progress">run {workflowRunId}</div>
  ),
}));
jest.mock("@/components/nodes/asset-preview", () => ({
  AssetPreview: () => <div data-testid="mock-asset-preview" />,
}));

const API_URL = "http://api.localhost";
const TOKEN = "test-token";

type MockResponse = { ok: boolean; status: number; json: () => Promise<any>; text: () => Promise<string> };

function jsonResponse(body: unknown, status = 200): MockResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function mockFetchSequence(handlers: Record<string, () => MockResponse>) {
  (global as any).fetch = jest.fn((url: string, _opts?: RequestInit) => {
    for (const [pattern, handler] of Object.entries(handlers)) {
      if (url.includes(pattern)) return Promise.resolve(handler());
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
}

const SEALED_FORM = {
  workflow_id: 42,
  name: "Retouch App",
  sealed: true,
  version_id: 7,
  sealed_inputs: [
    { node: "in1", configuration: { strength: 0.5, sharpen: true, prompt_file: "default.txt" } },
  ],
};

describe("SealedAppForm", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("renders one field per sealed input, type-appropriate", async () => {
    mockFetchSequence({
      "/sealed-form": () => jsonResponse(SEALED_FORM),
    });

    render(<SealedAppForm workflowId={42} apiUrl={API_URL} accessToken={TOKEN} />);

    expect(await screen.findByText("Retouch App")).toBeInTheDocument();
    expect(screen.getByLabelText("strength")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("sharpen")).toHaveAttribute("role", "checkbox");
    // File-ish key name gets a friendlier label but stays a plain field.
    expect(screen.getByLabelText("prompt_file (file reference)")).toHaveAttribute("type", "text");
  });

  it("Run posts the (saved) input values and starts the same run endpoint", async () => {
    let runBody: any = null;
    mockFetchSequence({
      "/sealed-inputs": () =>
        jsonResponse({
          sealed_inputs: [
            { node: "in1", configuration: { strength: 0.9, sharpen: false, prompt_file: "default.txt" } },
          ],
        }),
      "/run": () => {
        return jsonResponse({ workflow_run_id: 555 });
      },
      "/sealed-form": () => jsonResponse(SEALED_FORM),
    });
    // Capture the run POST body specifically (mockFetchSequence's handler
    // signature doesn't see the request init).
    const realFetch = global.fetch as jest.Mock;
    (global.fetch as jest.Mock) = jest.fn((url: string, opts?: RequestInit) => {
      if (url.includes("/run") && opts?.body) runBody = JSON.parse(opts.body as string);
      return realFetch(url, opts);
    });

    render(<SealedAppForm workflowId={42} apiUrl={API_URL} accessToken={TOKEN} />);
    await screen.findByText("Retouch App");

    fireEvent.change(screen.getByLabelText("strength"), { target: { value: "0.9" } });
    fireEvent.click(screen.getByLabelText("sharpen"));
    fireEvent.click(screen.getByRole("button", { name: /run/i }));

    await waitFor(() => expect(screen.getByTestId("mock-progress")).toHaveTextContent("run 555"));

    // The run endpoint hit is the SAME one every workflow uses.
    const ranUrl = (global.fetch as jest.Mock).mock.calls.find((c) => String(c[0]).includes("/run"))?.[0];
    expect(ranUrl).toBe(`${API_URL}/road/workflow-versions/7/run`);
    expect(runBody).toEqual({ cog_ids: [] });

    // The edited values were saved (PUT) before running.
    const putCall = (global.fetch as jest.Mock).mock.calls.find((c) => String(c[0]).includes("/sealed-inputs"));
    expect(putCall).toBeTruthy();
    const savedBody = JSON.parse((putCall![1] as RequestInit).body as string);
    expect(savedBody.sealed_inputs[0].configuration.strength).toBe(0.9);
    expect(savedBody.sealed_inputs[0].configuration.sharpen).toBe(false);
  });

  it("does not crash when the API returns 403 'sealed' instead of the form", async () => {
    mockFetchSequence({
      "/sealed-form": () => jsonResponse({ detail: "sealed" }, 403),
    });

    render(<SealedAppForm workflowId={42} apiUrl={API_URL} accessToken={TOKEN} />);

    expect(await screen.findByText(/could not load this app/i)).toBeInTheDocument();
    // No uncaught exception reached the test (render() above would have
    // thrown/failed the test if it had) and no field ever renders.
    expect(screen.queryByTestId("sealed-app-form")).not.toBeInTheDocument();
  });
});
