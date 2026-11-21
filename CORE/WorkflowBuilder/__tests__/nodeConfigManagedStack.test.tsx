/**
 * generalized-stack-wake D12 — the node config panel's "Managed stack" selector.
 *
 * The selector lists the tenant's managed (persistent) stacks (fetched via
 * authenticatedFetch GET /persistent-stacks) plus a "None (self-contained)"
 * option. Choosing a stack sets data.requires_stack on the node via the SAME
 * updateNodeData mechanism every other config field uses (so it persists into
 * the workflow graph on save); choosing None clears it. These tests drive the
 * real NodeConfigPanel with a spy updateNodeData and a mocked stacks list.
 */

import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Layout / pointer APIs some ui primitives reach for, absent in jsdom.
beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

// Mock ONLY authenticatedFetch; keep the module's other exports real so the
// panel's transitive imports still resolve.
jest.mock("@/lib/api-client", () => ({
  ...jest.requireActual("@/lib/api-client"),
  authenticatedFetch: jest.fn(),
}));

import { authenticatedFetch } from "@/lib/api-client";
import NodeConfigPanel from "@/components/node-config-panel";

const mockAuthFetch = authenticatedFetch as jest.MockedFunction<typeof authenticatedFetch>;

const STACKS_RESPONSE = [
  { id: 2, name: "sdxl-stack", status: "asleep" },
  { id: 1, name: "maxine-nim", status: "running" },
];

function makeResponse(body: any): any {
  return { ok: true, status: 200, headers: new Headers(), json: async () => body };
}

// A minimal process node — no nodeDefinitionId / status, so the ONLY network
// call the panel makes on mount is the managed-stacks list.
const makeNode = (data: any = {}) => ({
  id: "n1",
  type: "process",
  position: { x: 0, y: 0 },
  data: { label: "Resize", node_kind: "resize", ...data },
});

afterEach(() => {
  jest.clearAllMocks();
});

describe("NodeConfigPanel — Managed stack selector", () => {
  it("lists fetched stacks with a status hint plus a None option, and shows the current value", async () => {
    mockAuthFetch.mockResolvedValue(makeResponse(STACKS_RESPONSE));
    const updateNodeData = jest.fn();
    render(
      <NodeConfigPanel
        node={makeNode({ requires_stack: "maxine-nim" }) as any}
        updateNodeData={updateNodeData}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    const select = (await screen.findByTestId("requires-stack-select")) as HTMLSelectElement;
    // Current assignment reflected.
    expect(select.value).toBe("maxine-nim");
    // Options include None + both stacks (with status hint) once the fetch lands.
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /sdxl-stack — asleep/ })).toBeTruthy(),
    );
    expect(screen.getByRole("option", { name: "None (self-contained)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: /maxine-nim — running/ })).toBeTruthy();
  });

  it("sets data.requires_stack via updateNodeData when a stack is chosen", async () => {
    mockAuthFetch.mockResolvedValue(makeResponse(STACKS_RESPONSE));
    const updateNodeData = jest.fn();
    render(
      <NodeConfigPanel
        node={makeNode() as any}
        updateNodeData={updateNodeData}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    const select = (await screen.findByTestId("requires-stack-select")) as HTMLSelectElement;
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /sdxl-stack — asleep/ })).toBeTruthy(),
    );
    fireEvent.change(select, { target: { value: "sdxl-stack" } });
    expect(updateNodeData).toHaveBeenCalledWith(
      "n1",
      expect.objectContaining({ requires_stack: "sdxl-stack" }),
    );
  });

  it("clears data.requires_stack when None is chosen", async () => {
    mockAuthFetch.mockResolvedValue(makeResponse(STACKS_RESPONSE));
    const updateNodeData = jest.fn();
    render(
      <NodeConfigPanel
        node={makeNode({ requires_stack: "maxine-nim" }) as any}
        updateNodeData={updateNodeData}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    const select = (await screen.findByTestId("requires-stack-select")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "" } });
    expect(updateNodeData).toHaveBeenCalledTimes(1);
    const [, payload] = updateNodeData.mock.calls[0];
    expect("requires_stack" in payload).toBe(false);
  });
});
