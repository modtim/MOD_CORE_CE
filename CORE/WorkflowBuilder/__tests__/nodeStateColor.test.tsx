import React from "react";
import { render } from "@testing-library/react";
import { nodeStateBorderClass } from "@/lib/node-state-style";

// Phase 0 (node-run-controls) — run-state node colours.
// Success criteria: paused=yellow, re-running=blue, draining=amber,
// skipped=gray, force-failed/failed=red, everything else keeps the node's
// default border. Plus: a node actually paints the class on its root div.

describe("nodeStateBorderClass (state → border colour)", () => {
  const FALLBACK = "border-purple-500";

  it("paused wins over status → yellow", () => {
    expect(nodeStateBorderClass({ paused: true, status: "running" }, FALLBACK)).toBe("border-yellow-400");
  });
  it("re-running → blue", () => {
    expect(nodeStateBorderClass({ status: "re-running" }, FALLBACK)).toBe("border-blue-500");
    expect(nodeStateBorderClass({ status: "rerunning" }, FALLBACK)).toBe("border-blue-500");
  });
  it("draining → amber", () => {
    expect(nodeStateBorderClass({ status: "draining" }, FALLBACK)).toBe("border-amber-500");
  });
  it("skipped → gray", () => {
    expect(nodeStateBorderClass({ status: "skipped" }, FALLBACK)).toBe("border-gray-400");
  });
  it("failed / force-failed / failure → red", () => {
    expect(nodeStateBorderClass({ status: "failed" }, FALLBACK)).toBe("border-red-500");
    expect(nodeStateBorderClass({ status: "force_failed" }, FALLBACK)).toBe("border-red-500");
    expect(nodeStateBorderClass({ status: "failure" }, FALLBACK)).toBe("border-red-500");
  });
  it("control state (from /node-controls) wins over execution status", () => {
    expect(nodeStateBorderClass({ control: "paused", status: "running" }, FALLBACK)).toBe("border-yellow-400");
    expect(nodeStateBorderClass({ control: "draining", status: "running" }, FALLBACK)).toBe("border-amber-500");
    expect(nodeStateBorderClass({ control: "skipped", status: "success" }, FALLBACK)).toBe("border-gray-400");
    expect(nodeStateBorderClass({ control: "re-running", status: "running" }, FALLBACK)).toBe("border-blue-500");
    // force-fail control paints red immediately (not dependent on status rollup)
    expect(nodeStateBorderClass({ control: "force_failed", status: "success" }, FALLBACK)).toBe("border-red-500");
    expect(nodeStateBorderClass({ control: "force-failed", status: "running" }, FALLBACK)).toBe("border-red-500");
  });
  it("normal states fall through to the node's default border", () => {
    for (const st of ["running", "success", "queued", "completed", undefined]) {
      expect(nodeStateBorderClass({ status: st as string }, FALLBACK)).toBe(FALLBACK);
    }
    expect(nodeStateBorderClass(undefined, FALLBACK)).toBe(FALLBACK);
  });
});

// The node component actually paints the class on its root div.
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
const rootClass = (data: any): string => {
  const { container } = render(<ProcessNode {...baseProps} data={data} />);
  return (container.firstElementChild as HTMLElement).className;
};

describe("ProcessNode root border reflects run state", () => {
  it("paused node → yellow", () => {
    expect(rootClass({ label: "X", paused: true })).toContain("border-yellow-400");
  });
  it("force-failed node → red", () => {
    expect(rootClass({ label: "X", status: "failed" })).toContain("border-red-500");
  });
  it("normal running node keeps its default purple border", () => {
    expect(rootClass({ label: "X", status: "running" })).toContain("border-purple-500");
  });
});
