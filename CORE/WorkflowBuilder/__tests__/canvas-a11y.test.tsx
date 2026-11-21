/**
 * FX.20 R-038 — canvas accessibility:
 *  - the live-region announcement helpers (lib/canvas-a11y.ts) produce the
 *    right sentences for run lifecycle, node status, add/delete, connect;
 *  - a custom canvas node renders role="group" with aria-label
 *    "<node type>: <node label>" so screen readers announce each node.
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import {
  WB_CANVAS_LIVE_REGION_ID,
  describeRunStatusChange,
  describeNodeStatusChange,
  describeNodeAdded,
  describeNodesDeleted,
  describeEdgeConnected,
  describeTerminalRunStatus,
  diffStatuses,
  nodeAriaLabel,
} from "@/lib/canvas-a11y";
import * as fs from "fs";
import * as path from "path";
import { CodeNode } from "@/components/nodes/code-node";
import { ConditionalNode } from "@/components/nodes/conditional-node";
import { IfElseNode } from "@/components/nodes/if-else-node";
import { InputNode } from "@/components/nodes/input-node";
import { OutputNode } from "@/components/nodes/output-node";
import { ProcessNode } from "@/components/nodes/process-node";
import { SplitNode } from "@/components/nodes/split-node";
import { SwitchNode } from "@/components/nodes/switch-node";
import { ReactFlowProvider } from "reactflow";
import type { NodeProps } from "reactflow";

// Output/Process nodes import AssetPreview, which pulls Three.js ESM that
// jest (CommonJS) can't parse — mock the heavy WebGL deps, same as
// asset-preview.test.tsx does.
jest.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: any) => <div data-testid="canvas">{children}</div>,
  useLoader: () => ({}),
}));
jest.mock('@react-three/drei', () => ({
  OrbitControls: () => null,
  Center: ({ children }: any) => <div>{children}</div>,
  useGLTF: () => ({ scene: { clone: () => ({}) } }),
  Environment: () => null,
}));
jest.mock('three', () => ({
  DoubleSide: 2,
}));
jest.mock('three/examples/jsm/loaders/OBJLoader.js', () => ({ OBJLoader: class {} }));
jest.mock('three/examples/jsm/loaders/FBXLoader.js', () => ({ FBXLoader: class {} }));
jest.mock('three/examples/jsm/loaders/PLYLoader.js', () => ({ PLYLoader: class {} }));
jest.mock('three/examples/jsm/loaders/STLLoader.js', () => ({ STLLoader: class {} }));

describe("canvas-a11y announcement helpers", () => {
  it("exposes a stable live-region id", () => {
    expect(WB_CANVAS_LIVE_REGION_ID).toBeTruthy();
  });

  it("announces run lifecycle changes", () => {
    expect(describeRunStatusChange("queued")).toBe("Run started, queued");
    expect(describeRunStatusChange("running")).toBe("Run started");
    expect(describeRunStatusChange("success")).toBe("Run finished successfully");
    expect(describeRunStatusChange("failure")).toBe("Run failed");
    expect(describeRunStatusChange("cancelled")).toBe("Run cancelled");
    expect(describeRunStatusChange("totally-unknown")).toBe("Run status: totally-unknown");
    expect(describeRunStatusChange(null)).toBe("Run state cleared");
  });

  it("announces per-node status changes", () => {
    expect(describeNodeStatusChange("Upscale", "success")).toBe("Upscale: success");
    expect(describeNodeStatusChange(undefined, "failure")).toBe("Node: failure");
  });

  it("announces node add/delete/connect", () => {
    expect(describeNodeAdded("Upscale")).toBe("Added node: Upscale");
    expect(describeNodeAdded(undefined)).toBe("Added node: node");
    expect(describeNodesDeleted(1)).toBe("Deleted 1 node");
    expect(describeNodesDeleted(3)).toBe("Deleted 3 nodes");
    expect(describeEdgeConnected("A", "B")).toBe("Connected A to B");
  });

  it("diffStatuses emits only newly-appearing status transitions", () => {
    const prev = new Map<string, string>([
      ["n1", "queued"],
      ["n2", "running"],
    ]);
    const next = [
      { id: "n1", label: "One", status: "success" }, // changed
      { id: "n2", label: "Two", status: "running" }, // unchanged
      { id: "n3", label: "Three", status: "failure" }, // new
    ];
    const changes = diffStatuses(prev, next);
    expect(changes).toEqual(["One: success", "Three: failure"]);
  });

  it("diffStatuses with no prior state announces every current status", () => {
    const changes = diffStatuses(undefined, [
      { id: "n1", label: "One", status: "running" },
    ]);
    expect(changes).toEqual(["One: running"]);
  });
});

describe("node a11y label", () => {
  it("builds '<type>: <label>'", () => {
    expect(nodeAriaLabel("code", "Upscale")).toBe("code: Upscale");
    expect(nodeAriaLabel("code", undefined)).toBe("code: code");
  });
});

// Minimal NodeProps — CodeNode only reads `data` (and isConnectable, optional).
function codeNodeProps(label: string): NodeProps {
  return {
    id: "test-node-1",
    type: "code",
    data: { label } as any,
    position: { x: 0, y: 0 },
    selected: false,
    dragging: false,
    isConnectable: true,
  } as unknown as NodeProps;
}

// CodeNode renders <Handle> which needs React Flow's store context, so wrap
// in ReactFlowProvider (the node component itself is provider-agnostic).
function renderCodeNode(props: NodeProps) {
  return render(
    <ReactFlowProvider>
      <CodeNode {...props} />
    </ReactFlowProvider>,
  );
}

// Generic props factory + render wrapper for every custom node type (same
// pattern as the CodeNode test above: React Flow's <Handle> needs the store
// context, so each node is wrapped in ReactFlowProvider).
function nodeProps(type: string, label: string): NodeProps {
  return {
    id: "test-node-1",
    type,
    data: { label } as any,
    position: { x: 0, y: 0 },
    selected: false,
    dragging: false,
    isConnectable: true,
  } as unknown as NodeProps;
}

function renderNode(node: React.ReactElement) {
  return render(
    <ReactFlowProvider>
      {node}
    </ReactFlowProvider>,
  );
}

describe("CodeNode a11y rendering", () => {
  it("renders role=group with aria-label '<type>: <label>'", () => {
    renderCodeNode(codeNodeProps("Upscale"));
    const group = screen.getByRole("group", { name: "code: Upscale" });
    expect(group).toHaveAttribute("aria-label", "code: Upscale");
  });

  it("falls back to the node type when no label is set", () => {
    renderCodeNode(codeNodeProps(""));
    expect(screen.getByRole("group", { name: "code: code" })).toBeInTheDocument();
  });
});

// FX.20 R-038 follow-up — EVERY custom node type renders role="group" +
// aria-label="<type>: <label>" on its card root (mirrors CodeNode).
describe("all custom node types render a11y group label", () => {
  const cases: Array<[string, (p: NodeProps) => React.ReactElement]> = [
    ["code", (p) => <CodeNode {...p} />],
    ["conditional", (p) => <ConditionalNode {...p} />],
    ["if-else", (p) => <IfElseNode {...p} />],
    ["input", (p) => <InputNode {...p} />],
    ["output", (p) => <OutputNode {...p} />],
    ["process", (p) => <ProcessNode {...p} />],
    ["split", (p) => <SplitNode {...p} />],
    ["switch", (p) => <SwitchNode {...p} />],
  ];

  for (const [type, renderEl] of cases) {
    it(`${type} node renders role=group with aria-label '${type}: <label>'`, () => {
      renderNode(renderEl(nodeProps(type, "My Label")));
      const group = screen.getByRole("group", { name: `${type}: My Label` });
      expect(group).toHaveAttribute("aria-label", `${type}: My Label`);
    });

    it(`${type} node falls back to the node type when no label is set`, () => {
      renderNode(renderEl(nodeProps(type, "")));
      expect(screen.getByRole("group", { name: `${type}: ${type}` })).toBeInTheDocument();
    });
  }
});

// RC20 product red — the canvas live region must announce a node's transition
// INTO a terminal state and the run's final sentence (CORE/e2e/tests/
// wb-node-catalogue-audit.spec.ts:371-372 asserts both after a cancelled/failed
// run). The builder's node-diff effect was keyed on api.pollingRunId, which the
// poller clears in the same tick it writes the terminal statuses, so the final
// transition never announced anything.
describe("terminal run announcements (RC20 a11y red)", () => {
  it("describeTerminalRunStatus stays silent while nodes are still running", () => {
    expect(describeTerminalRunStatus(["running", "queued"], "running")).toBe("");
    expect(describeTerminalRunStatus([], null)).toBe("");
  });

  it("running -> failed emits the node sentence and 'Run finished with status failed'", () => {
    const prev = new Map<string, string>([["n1", "running"], ["n2", "running"]]);
    const next = [
      { id: "n1", label: "Image Input", status: "success" },
      { id: "n2", label: "Return Assets Export", status: "failure" },
    ];
    const changes = diffStatuses(prev, next);
    expect(changes).toEqual(["Image Input: success", "Return Assets Export: failure"]);
    const sentence = describeTerminalRunStatus(next.map((s) => s.status), "failure");
    expect(sentence).toBe("Run finished with status failed");
  });

  it("cancelled run says cancelled even though its nodes land on failure", () => {
    const next = [{ id: "n1", label: "Return Assets Export", status: "failure" }];
    expect(describeTerminalRunStatus(next.map((s) => s.status), "cancelled"))
      .toBe("Run finished with status cancelled");
    expect(describeTerminalRunStatus(next.map((s) => s.status), null))
      .toBe("Run finished with status failed");
  });

  it("all-success settles as 'Run finished successfully'", () => {
    expect(describeTerminalRunStatus(["success", "completed"], "completed"))
      .toBe("Run finished successfully");
  });

  it("the builder's node-diff effect is keyed on the canvas-bound run, not the poll alias", () => {
    const lines = fs.readFileSync(
      path.resolve(__dirname, "../components/workflow-builder.tsx"),
      "utf-8",
    ).split("\n");
    const start = lines.findIndex((l) => l.includes("const lastNodeStatuses = useRef"));
    const effect = lines.slice(start, start + 40).join("\n");
    expect(start).toBeGreaterThan(-1);
    expect(effect).toContain("const runId = api.selectedRunId;");
    expect(effect.includes("const runId = api.pollingRunId;")).toBe(false);
  });
});
