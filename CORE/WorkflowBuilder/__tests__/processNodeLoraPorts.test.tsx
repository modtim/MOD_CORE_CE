import React from "react";
import { render } from "@testing-library/react";

// Mock reactflow's Handle so we can count the rendered ports and inspect their
// type/id/position — this exercises the real port-derivation + labeled-port
// rendering logic in ProcessNode at the render layer.
jest.mock("reactflow", () => ({
  Handle: (props: any) => (
    <div
      data-testid="handle"
      data-handle-type={props.type}
      data-handle-id={props.id}
      data-handle-position={props.position}
      data-handle-class={props.className}
      title={props.title}
      style={props.style}
    >
      {props.children}
    </div>
  ),
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));

// Stub heavy child modules that pull in three.js (ESM) / previews — irrelevant
// to port derivation and otherwise break the jest transform.
jest.mock("@/components/nodes/asset-preview", () => ({ AssetPreview: () => null }));
jest.mock("@/components/model3d-thumbnail", () => ({ Model3DViewerModal: () => null }));
jest.mock("@/components/node-chips", () => ({ NodeChips: () => null, LifecycleBadge: () => null }));

import { ProcessNode } from "@/components/nodes/process-node";

// The canonical SDXL Character LoRA Train schema (object-form named ports with
// human labels + the new text Captions channel).
const loraData: any = {
  label: "SDXL Character LoRA Train",
  input_schema: {
    input: { type: "image", file_mode: "multiple", label: "Image in" },
    reg_images: { type: "image", file_mode: "multiple", label: "Regularization images" },
    avoid_images: { type: "image", file_mode: "multiple", label: "Avoid / Negative images" },
    captions: { type: "text", file_mode: "multiple", label: "Captions / words (optional)" },
  },
  output_schema: { lora_export: { type: "lora", label: "Trained LoRA" } },
};

const renderNode = (data: any) =>
  render(
    <ProcessNode data={data} isConnectable id="n1" selected={false} type="process" zIndex={1} dragging={false} xPos={0} yPos={0} />
  );

test("all four object-form inputs render as distinct target handles", () => {
  const { getAllByTestId } = renderNode(loraData);
  const handles = getAllByTestId("handle");
  const targets = handles.filter((h) => h.getAttribute("data-handle-type") === "target");
  const sources = handles.filter((h) => h.getAttribute("data-handle-type") === "source");
  const targetIds = targets.map((h) => h.getAttribute("data-handle-id"));
  expect(targetIds).toEqual(["input", "reg_images", "avoid_images", "captions"]);
  // the lora output still renders
  expect(sources.map((h) => h.getAttribute("data-handle-id"))).toEqual(["lora_export"]);
});

test("multi-input node renders each port on the LEFT edge with a visible label", () => {
  const { getByText, getAllByTestId } = renderNode(loraData);
  // The negative + regularization channels are drawn as named, human-readable
  // labels — the whole point of the fix.
  expect(getByText("Image in")).toBeTruthy();
  expect(getByText("Regularization images")).toBeTruthy();
  expect(getByText("Avoid / Negative images")).toBeTruthy();
  expect(getByText("Captions / words (optional)")).toBeTruthy();
  // labeled layout puts the input handles on the left edge
  const leftHandles = getAllByTestId("handle").filter(
    (h) => h.getAttribute("data-handle-type") === "target" && h.getAttribute("data-handle-position") === "left"
  );
  expect(leftHandles).toHaveLength(4);
});

test("multi-input left ports are full-row transparent hit targets anchored at the left edge", () => {
  // Regression for BOTH the "side ports don't connect" bug and the "connection
  // hint line doesn't show" bug. Each labeled port's Handle is now a
  // position:absolute, inset:0 element that fills its row (via the wb-port-hit
  // CSS that beats globals' 14px !important). Full-row => a drop anywhere on the
  // row connects (elementsFromPoint finds the handle); left edge => ReactFlow's
  // Position.Left anchor pins the hint line to the port.
  const { getAllByTestId } = renderNode(loraData);
  const leftTargets = getAllByTestId("handle").filter(
    (h) => h.getAttribute("data-handle-type") === "target" && h.getAttribute("data-handle-position") === "left"
  );
  expect(leftTargets).toHaveLength(4);
  for (const h of leftTargets) {
    // absolutely positioned, pinned to all four edges of the row
    expect(h.style.position).toBe("absolute");
    expect(h.style.top).toBe("0px");
    expect(h.style.left).toBe("0px");
    expect(h.style.right).toBe("0px");
    expect(h.style.bottom).toBe("0px");
    // transparent so it never hides the label, and no leftover transform that
    // would shift the anchor off the left edge
    expect(h.style.background).toBe("transparent");
    expect(h.style.transform).toBe("none");
    // carries the class the injected CSS uses to force the box to full size
    expect(h.getAttribute("data-handle-class")).toBe("wb-port-hit");
    // must NOT hang off the node's left edge (where overflow:hidden clipped it)
    expect(h.style.left).not.toBe("-7px");
  }
});

test("multi-input labels are WHITE and never intercept the drop", () => {
  // Readability fix: the channel labels render as white text on the blue port
  // strip, and — with the swatch — carry pointer-events:none so the pointer
  // falls through to the full-row handle beneath.
  const { getByText } = renderNode(loraData);
  for (const label of [
    "Image in",
    "Regularization images",
    "Avoid / Negative images",
    "Captions / words (optional)",
  ]) {
    const span = getByText(label);
    expect(span.className).toContain("text-white");
    expect(span.className).toContain("pointer-events-none");
  }
});

test("single-input legacy node keeps the classic top handle (no regression)", () => {
  const legacy: any = {
    label: "Image to Text",
    input_schema: { input_type: "image", file_mode: "multiple", input_variables: "NONE" },
    output_schema: { output_type: "text", file_mode: "single", output_variables: "NONE" },
  };
  const { getAllByTestId } = renderNode(legacy);
  const targets = getAllByTestId("handle").filter((h) => h.getAttribute("data-handle-type") === "target");
  expect(targets).toHaveLength(1);
  expect(targets[0].getAttribute("data-handle-id")).toBe("input");
  expect(targets[0].getAttribute("data-handle-position")).toBe("top");
});
