import React from "react";
import { render } from "@testing-library/react";

// Mock reactflow's Handle/Position so the nodes render at the render layer
// without a ReactFlowProvider.
jest.mock("reactflow", () => ({
  Handle: (props: any) => (
    <div data-testid="handle" data-handle-id={props.id} style={props.style} />
  ),
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  // output-node.tsx now reads the graph through the zustand hooks (FX.72 p4b),
  // so the mock must hand back arrays or the node throws on mount.
  useNodes: () => [],
  useEdges: () => [],
}));

// Stub heavy / irrelevant children.
jest.mock("@/components/nodes/asset-preview", () => ({ AssetPreview: () => null }));
jest.mock("@/components/model3d-thumbnail", () => ({ Model3DViewerModal: () => null }));
jest.mock("@/components/node-chips", () => ({ NodeChips: () => null }));
jest.mock("@/components/ui/use-toast", () => ({ toast: () => {} }));

import { InputNode } from "@/components/nodes/input-node";
import { OutputNode } from "@/components/nodes/output-node";

const inputProps: any = {
  data: { label: "Image Input", output_schema: { output_type: "image" } },
  isConnectable: true,
  id: "in1",
  selected: false,
  type: "input",
  zIndex: 1,
  dragging: false,
  xPos: 0,
  yPos: 0,
};

const outputProps: any = {
  data: { label: "Export", input_schema: { input_type: "image" } },
  isConnectable: true,
  id: "out1",
  selected: false,
  type: "output",
  zIndex: 1,
  dragging: false,
  xPos: 0,
  yPos: 0,
};

// ReactFlow's own stylesheet paints `.react-flow__node-input` /
// `.react-flow__node-output` (these node types) with color:#222, which every
// uncoloured label inherits — black-on-black once the card goes dark in dark
// mode. The fix sets an explicit theme-aware `text-foreground` on the node
// root so nothing inherits that #222. JSDOM can't evaluate the cascade, so we
// assert the guard class is present at the render layer; real contrast is
// covered by the in-browser manual test.

test("input node root carries an explicit theme-aware text colour (not inherited #222)", () => {
  const { container } = render(<InputNode {...inputProps} />);
  const root = container.firstElementChild as HTMLElement;
  expect(root.className).toContain("text-foreground");
});

test("output node root carries an explicit theme-aware text colour (not inherited #222)", () => {
  const { container } = render(<OutputNode {...outputProps} />);
  const root = container.firstElementChild as HTMLElement;
  expect(root.className).toContain("text-foreground");
});

test("input node title still renders", () => {
  const { getByText } = render(<InputNode {...inputProps} />);
  expect(getByText("Image Input")).toBeTruthy();
});

test("output node title still renders", () => {
  const { getByText } = render(<OutputNode {...outputProps} />);
  expect(getByText("Export")).toBeTruthy();
});
