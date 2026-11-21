import React from "react";
import { render } from "@testing-library/react";

// generalized-stack-wake D12 — the on-node managed-stack overlay.
// The overlay now renders on EVERY canvas node (all node types), not only the
// stack-backed ones. Asserts: a node whose kind is in the bindings map gets a
// COLOURED badge with the state's colour + a "Requires stack: <name> · <state>"
// tooltip and a data-stack-name; a node with no binding (unbound kind, no kind
// at all, or an empty bindings map) gets a MUTED badge (data-stack-state="none")
// so stack status is visible platform-wide.

// Mock reactflow's Handle/Position (nodes render ports; the real reactflow needs
// a provider we don't want here). Same stub the sibling node tests use.
jest.mock("reactflow", () => ({
  Handle: () => <div data-testid="handle" />,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));
// Stub heavy children (three.js / previews) irrelevant to the overlay.
jest.mock("@/components/nodes/asset-preview", () => ({ AssetPreview: () => null }));
jest.mock("@/components/model3d-thumbnail", () => ({ Model3DViewerModal: () => null }));
jest.mock("@/components/node-chips", () => ({ NodeChips: () => null, LifecycleBadge: () => null }));

import { ProcessNode } from "@/components/nodes/process-node";
import { InputNode } from "@/components/nodes/input-node";
import { NodeStackOverlay } from "@/components/nodes/node-stack-overlay";
import {
  NodeStackBindingsProvider,
  stackStateStyle,
  type NodeBindingsMap,
} from "@/lib/node-stack-bindings";

const baseProps: any = {
  isConnectable: true,
  id: "n1",
  selected: false,
  type: "process",
  zIndex: 1,
  dragging: false,
  xPos: 0,
  yPos: 0,
};

const BINDINGS: NodeBindingsMap = {
  studio_voice: { stack_name: "maxine-nim", family: "nim", state: "running" },
  sdxl_turbo: { stack_name: "sdxl-stack", family: "sdxl", state: "waking" },
  brand_vlm: { stack_name: "vlm-stack", family: "vlm", state: "asleep" },
  vsr_upscale: { stack_name: "vsr-stack", family: "generic", state: "waiting" },
  mystery_node: { stack_name: "who-knows", family: "llm", state: "banana" as any },
};

// stack_name -> {family,state}. Mirrors the `stacks` key of the node-bindings
// response; used to resolve a per-instance data.requires_stack assignment.
const STACKS = {
  "maxine-nim": { family: "nim", state: "running" },
  "sdxl-stack": { family: "sdxl", state: "waking" },
  "sleepy-stack": { family: "generic", state: "asleep" },
} as any;

const renderNode = (data: any, bindings: NodeBindingsMap = BINDINGS, stacks: any = {}) =>
  render(
    <NodeStackBindingsProvider value={bindings} stacks={stacks}>
      <ProcessNode {...baseProps} data={data} />
    </NodeStackBindingsProvider>,
  );

describe("NodeStackOverlay on a canvas node", () => {
  it("shows the coloured overlay for a bound node (running → green) with the stack tooltip", () => {
    const { getByTestId } = renderNode({ label: "Studio Voice", node_kind: "studio_voice" });
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay).toBeTruthy();
    expect(overlay.getAttribute("data-stack-state")).toBe("running");
    expect(overlay.getAttribute("data-stack-name")).toBe("maxine-nim");
    expect(overlay.getAttribute("title")).toBe("Requires stack: maxine-nim · running");
    expect(getByTestId("node-stack-dot").className).toContain("bg-green-500");
    // running is a settled state — no pulse
    expect(getByTestId("node-stack-dot").className).not.toContain("animate-pulse");
  });

  it("colours waking amber and pulses", () => {
    const { getByTestId } = renderNode({ label: "SDXL", node_kind: "sdxl_turbo" });
    const dot = getByTestId("node-stack-dot");
    expect(dot.className).toContain("bg-amber-500");
    expect(dot.className).toContain("animate-pulse");
    expect(getByTestId("node-stack-overlay").getAttribute("title")).toBe(
      "Requires stack: sdxl-stack · waking",
    );
  });

  it("colours asleep grey (no pulse)", () => {
    const { getByTestId } = renderNode({ label: "VLM", node_kind: "brand_vlm" });
    const dot = getByTestId("node-stack-dot");
    expect(dot.className).toContain("bg-gray-400");
    expect(dot.className).not.toContain("animate-pulse");
  });

  it("colours waiting amber and pulses", () => {
    const { getByTestId } = renderNode({ label: "VSR", node_kind: "vsr_upscale" });
    const dot = getByTestId("node-stack-dot");
    expect(dot.className).toContain("bg-amber-500");
    expect(dot.className).toContain("animate-pulse");
  });

  it("falls back to the muted 'unknown' style for an unrecognised state", () => {
    const { getByTestId } = renderNode({ label: "Mystery", node_kind: "mystery_node" });
    const dot = getByTestId("node-stack-dot");
    expect(dot.className).toContain("bg-gray-300");
  });

  it("renders a MUTED badge (state=none) for a node whose kind is not bound to a stack", () => {
    const { getByTestId, queryByTestId } = renderNode({ label: "Plain", node_kind: "not_a_stack_node" });
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-state")).toBe("none");
    // muted badge carries no stack name and no coloured state dot
    expect(overlay.getAttribute("data-stack-name")).toBeNull();
    expect(queryByTestId("node-stack-dot")).toBeNull();
  });

  it("renders a MUTED badge (state=none) for a node with no node_kind at all", () => {
    const { getByTestId } = renderNode({ label: "Legacy" });
    expect(getByTestId("node-stack-overlay").getAttribute("data-stack-state")).toBe("none");
  });

  it("renders a MUTED badge (state=none) when the bindings map is empty", () => {
    const { getByTestId } = renderNode({ label: "Studio Voice", node_kind: "studio_voice" }, {});
    expect(getByTestId("node-stack-overlay").getAttribute("data-stack-state")).toBe("none");
  });

  // a11y: muted/unbound badge must NOT trap keyboard focus (WCAG 2.4.3 — aria-hidden-focus)
  it("muted (state=none) badge is non-focusable via tabIndex=-1", () => {
    const { getByTestId } = renderNode({ label: "Plain", node_kind: "not_a_stack_node" });
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-state")).toBe("none");
    expect(overlay.getAttribute("tabindex")).toBe("-1");
  });
});

describe("NodeStackOverlay per-instance requires_stack assignment", () => {
  it("shows the ASSIGNED stack + its live state, resolved from the stacks map", () => {
    const { getByTestId } = renderNode(
      { label: "Any", node_kind: "not_a_stack_node", requires_stack: "maxine-nim" },
      BINDINGS,
      STACKS,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-name")).toBe("maxine-nim");
    expect(overlay.getAttribute("data-stack-state")).toBe("running");
    expect(overlay.getAttribute("title")).toBe("Requires stack: maxine-nim · running");
    expect(getByTestId("node-stack-dot").className).toContain("bg-green-500");
  });

  it("takes precedence over the node_kind auto-binding", () => {
    // node_kind studio_voice would auto-bind to maxine-nim/running, but the
    // instance is assigned sdxl-stack (waking) — the assignment must win.
    const { getByTestId } = renderNode(
      { label: "Studio Voice", node_kind: "studio_voice", requires_stack: "sdxl-stack" },
      BINDINGS,
      STACKS,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-name")).toBe("sdxl-stack");
    expect(overlay.getAttribute("data-stack-state")).toBe("waking");
    expect(getByTestId("node-stack-dot").className).toContain("bg-amber-500");
    expect(getByTestId("node-stack-dot").className).toContain("animate-pulse");
  });

  it("shows the assigned name with an 'unknown' state when the stack isn't in the stacks map", () => {
    const { getByTestId } = renderNode(
      { label: "Any", node_kind: "not_a_stack_node", requires_stack: "not-deployed-yet" },
      BINDINGS,
      STACKS,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-name")).toBe("not-deployed-yet");
    expect(overlay.getAttribute("data-stack-state")).toBe("unknown");
    expect(getByTestId("node-stack-dot").className).toContain("bg-gray-300");
  });

  it("falls back to the node_kind auto-binding when requires_stack is cleared", () => {
    const { getByTestId } = renderNode(
      { label: "Studio Voice", node_kind: "studio_voice", requires_stack: "" },
      BINDINGS,
      STACKS,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-name")).toBe("maxine-nim");
    expect(overlay.getAttribute("data-stack-state")).toBe("running");
  });

  it("shows the MUTED badge when neither assigned nor auto-bound", () => {
    const { getByTestId, queryByTestId } = renderNode(
      { label: "Plain", node_kind: "not_a_stack_node" },
      BINDINGS,
      STACKS,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-state")).toBe("none");
    expect(queryByTestId("node-stack-dot")).toBeNull();
  });
});

describe("NodeStackOverlay mounts on the newly-wired node components", () => {
  const inputProps: any = { ...baseProps, type: "input" };

  it("mounts a MUTED badge on an input node with no bound kind", () => {
    const { getByTestId } = render(
      <NodeStackBindingsProvider value={BINDINGS}>
        <InputNode {...inputProps} data={{ label: "Upload" }} />
      </NodeStackBindingsProvider>,
    );
    expect(getByTestId("node-stack-overlay").getAttribute("data-stack-state")).toBe("none");
  });

  it("mounts a COLOURED badge on an input node whose kind is bound", () => {
    const { getByTestId } = render(
      <NodeStackBindingsProvider value={BINDINGS}>
        <InputNode {...inputProps} data={{ label: "Studio Voice", node_kind: "studio_voice" }} />
      </NodeStackBindingsProvider>,
    );
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-state")).toBe("running");
    expect(overlay.getAttribute("data-stack-name")).toBe("maxine-nim");
    expect(getByTestId("node-stack-dot").className).toContain("bg-green-500");
  });
});

describe("NodeStackOverlay standalone (binding resolution via context)", () => {
  const renderOverlay = (nodeKind: string | undefined, bindings: NodeBindingsMap) =>
    render(
      <NodeStackBindingsProvider value={bindings}>
        <NodeStackOverlay nodeKind={nodeKind} />
      </NodeStackBindingsProvider>,
    );

  it("renders a MUTED badge when the kind is unbound", () => {
    const { getByTestId, queryByTestId } = renderOverlay("nope", BINDINGS);
    expect(getByTestId("node-stack-overlay").getAttribute("data-stack-state")).toBe("none");
    expect(queryByTestId("node-stack-dot")).toBeNull();
  });

  it("renders a MUTED badge when the kind is undefined", () => {
    const { getByTestId } = renderOverlay(undefined, BINDINGS);
    expect(getByTestId("node-stack-overlay").getAttribute("data-stack-state")).toBe("none");
  });

  it("renders the coloured badge when the kind is bound", () => {
    const { getByTestId } = renderOverlay("studio_voice", BINDINGS);
    const overlay = getByTestId("node-stack-overlay");
    expect(overlay.getAttribute("data-stack-state")).toBe("running");
    expect(overlay.getAttribute("data-stack-name")).toBe("maxine-nim");
  });
});

describe("stackStateStyle mapping", () => {
  it("maps each documented state to its colour + pulse", () => {
    expect(stackStateStyle("running").dotClass).toBe("bg-green-500");
    expect(stackStateStyle("running").pulse).toBe(false);
    expect(stackStateStyle("waking").dotClass).toBe("bg-amber-500");
    expect(stackStateStyle("waking").pulse).toBe(true);
    expect(stackStateStyle("waiting").dotClass).toBe("bg-amber-500");
    expect(stackStateStyle("waiting").pulse).toBe(true);
    expect(stackStateStyle("asleep").dotClass).toBe("bg-gray-400");
    expect(stackStateStyle("asleep").pulse).toBe(false);
    expect(stackStateStyle("unknown").dotClass).toBe("bg-gray-300");
  });
  it("falls through to unknown for anything unrecognised", () => {
    expect(stackStateStyle("banana").dotClass).toBe("bg-gray-300");
    expect(stackStateStyle(undefined).dotClass).toBe("bg-gray-300");
  });
});
