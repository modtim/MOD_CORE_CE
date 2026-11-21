import React from "react";
import { render, screen } from "@testing-library/react";
import { describeNodeStatusChange } from "@/lib/canvas-a11y";

// node-catalogue-audit ("COMPLETED lies") moved from the route-stubbed E2E to jest.
// (1) ProcessNode with status 'failure' + error_message renders the visible
//     "Failed" block and the error text (process-node.tsx isFailed branch);
//     with status 'success' the "Failed" text is absent.
// (2) The canvas live-region sentence from describeNodeStatusChange.
// (3) The run-stamp guard (process-node.tsx:211-230): a status is stamped with
//     the run id it FIRST arrived under, and isFailed only holds while that
//     stamp equals window.__lastWorkflowRunId (the newest run the app started).
//     Every case below sets/clears that global explicitly and restores it in
//     afterEach, so none of them passes because latestRunId happens to be null.

jest.mock("reactflow", () => ({
  Handle: () => <div data-testid="handle" />,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));
jest.mock("@/components/nodes/asset-preview", () => ({ AssetPreview: () => null }));
jest.mock("@/components/node-chips", () => ({ NodeChips: () => null, LifecycleBadge: () => null }));
jest.mock("@/components/model3d-thumbnail", () => ({ Model3DViewerModal: () => null }));

import { ProcessNode } from "@/components/nodes/process-node";

const baseProps: any = {
  isConnectable: true, id: "n1", selected: false, type: "process",
  zIndex: 1, dragging: false, xPos: 0, yPos: 0,
};
const renderNode = (data: any) => render(<ProcessNode {...baseProps} data={data} />);

// The stamp guard reads window.__lastWorkflowRunId on EVERY render. Tests that
// need the same mounted component (so its useState stamp survives) mutate one
// shared `data` object and re-render it with RTL `rerender` — exactly what the
// canvas poller does to a reactflow node's data object.
const initialRunId = (window as any).__lastWorkflowRunId;
const setRunId = (runId: number | null) => { (window as any).__lastWorkflowRunId = runId; };
const clearRunId = () => { delete (window as any).__lastWorkflowRunId; };

afterEach(() => {
  if (initialRunId === undefined) clearRunId();
  else setRunId(initialRunId);
});

describe("ProcessNode failed render", () => {
  it("status failure + error_message under the current run shows the Failed block and the error text", () => {
    setRunId(71);
    renderNode({ label: "Upscale", status: "failure", error_message: "zero outputs produced" });
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("zero outputs produced")).toBeInTheDocument();
  });

  it("status failure without error_message under the current run still shows Failed, no error text", () => {
    setRunId(71);
    renderNode({ label: "Upscale", status: "failure" });
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.queryByText("zero outputs produced")).not.toBeInTheDocument();
  });

  it("status success hides the Failed text (re-poll flips the node back)", () => {
    setRunId(71);
    renderNode({ label: "Upscale", status: "success", error_message: "zero outputs produced" });
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("zero outputs produced")).not.toBeInTheDocument();
  });

  it("stale failure clears when a newer run starts (run-stamp guard)", () => {
    const node = (status: string) => <ProcessNode {...baseProps} data={{ label: "Upscale", status, error_message: "zero outputs produced" }} />;
    setRunId(55);
    const mounted = render(node("failure"));
    expect(screen.getByText("Failed")).toBeInTheDocument();

    // reuse_cache re-run: no NodeRun row for this node, so the poller never
    // rewrites data.status — only the global run id moves. The stamp (55) no
    // longer matches, so the latched "Failed" must clear.
    setRunId(60);
    mounted.rerender(node("failure"));
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("zero outputs produced")).not.toBeInTheDocument();
  });

  it("failure first arriving while the global is a newer run shows Failed", () => {
    setRunId(60);
    renderNode({ label: "Upscale", status: "failure", error_message: "zero outputs produced" });
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("zero outputs produced")).toBeInTheDocument();
  });

  it("no run id on the page (reload of a past failed run) still shows Failed", () => {
    clearRunId();
    renderNode({ label: "Upscale", status: "failure", error_message: "zero outputs produced" });
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("zero outputs produced")).toBeInTheDocument();
  });

  it("failure -> success -> failure under one run re-stamps and shows Failed again", () => {
    setRunId(60);
    const node = (status: string) => <ProcessNode {...baseProps} data={{ label: "Upscale", status }} />;
    const mounted = render(node("failure"));
    expect(screen.getByText("Failed")).toBeInTheDocument();

    mounted.rerender(node("success"));
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();

    mounted.rerender(node("failure"));
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("stale failure, then the poller writes a fresh failure for the new run, shows Failed", () => {
    const node = (status: string) => <ProcessNode {...baseProps} data={{ label: "Upscale", status, error_message: "zero outputs produced" }} />;
    setRunId(55);
    const mounted = render(node("failure"));
    expect(screen.getByText("Failed")).toBeInTheDocument();

    setRunId(60);
    mounted.rerender(node("failure"));
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();

    mounted.rerender(node("running"));
    mounted.rerender(node("failure"));
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });
});

describe("describeNodeStatusChange (canvas live-region text)", () => {
  it("announces '<label>: failure' then '<label>: success' on a re-poll", () => {
    expect(describeNodeStatusChange("Upscale", "failure")).toBe("Upscale: failure");
    expect(describeNodeStatusChange("Upscale", "success")).toBe("Upscale: success");
  });
  it("falls back to 'Node' when the label is missing", () => {
    expect(describeNodeStatusChange(undefined, "failure")).toBe("Node: failure");
  });
});
