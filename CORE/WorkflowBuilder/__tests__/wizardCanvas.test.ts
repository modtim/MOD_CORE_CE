// Tests for the canvas serialisation helper
// (canvas-aware-workflow-wizard §8.1).
//
// Covers:
//   - empty nodes → null (signal to caller to omit canvas field)
//   - basic shape: every node serialises with id, name, label, position
//   - selection picks up node.selected === true entries only
//   - empty config dicts are normalised to null (prompt-token saving)
//   - source/target ports propagate from sourceHandle/targetHandle
//   - reads node-definition name from data.nodeDefinitionName then data.label

import type { Node, Edge } from "reactflow";
import { serialiseCanvasForWizard } from "@/lib/wizard-canvas";

function n(
  id: string,
  defName: string,
  opts?: Partial<{ selected: boolean; label: string; config: Record<string, unknown>; x: number; y: number }>,
): Node {
  return {
    id,
    type: "default",
    position: { x: opts?.x ?? 0, y: opts?.y ?? 0 },
    data: {
      nodeDefinitionName: defName,
      label: opts?.label ?? defName,
      ...(opts?.config ? { config: opts.config } : {}),
    },
    selected: opts?.selected,
  };
}

function e(
  id: string,
  source: string,
  target: string,
  opts?: Partial<{ sourceHandle: string; targetHandle: string }>,
): Edge {
  return {
    id,
    source,
    target,
    sourceHandle: opts?.sourceHandle,
    targetHandle: opts?.targetHandle,
  };
}

describe("serialiseCanvasForWizard", () => {
  test("empty canvas returns null", () => {
    expect(serialiseCanvasForWizard([], [])).toBeNull();
  });

  test("single node with no selection", () => {
    const out = serialiseCanvasForWizard([n("n1", "Resize")], []);
    expect(out).not.toBeNull();
    expect(out!.selection.node_ids).toEqual([]);
    expect(out!.graph.nodes).toHaveLength(1);
    expect(out!.graph.nodes[0].id).toBe("n1");
    expect(out!.graph.nodes[0].node_definition_name).toBe("Resize");
    expect(out!.graph.edges).toEqual([]);
  });

  test("selection picks up node.selected entries only", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "Resize", { selected: true }),
       n("n2", "MakeMask"),
       n("n3", "ImgOut", { selected: true })],
      [],
    );
    expect(out!.selection.node_ids).toEqual(["n1", "n3"]);
  });

  test("edges propagate source/target ports", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A"), n("n2", "B")],
      [e("e1", "n1", "n2", { sourceHandle: "out", targetHandle: "in" })],
    );
    expect(out!.graph.edges).toHaveLength(1);
    expect(out!.graph.edges[0]).toMatchObject({
      id: "e1",
      source_node_id: "n1",
      source_port: "out",
      target_node_id: "n2",
      target_port: "in",
    });
  });

  test("missing source/target handles serialise as null", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A"), n("n2", "B")],
      [e("e1", "n1", "n2")],
    );
    expect(out!.graph.edges[0].source_port).toBeNull();
    expect(out!.graph.edges[0].target_port).toBeNull();
  });

  test("empty config object is normalised to null", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A", { config: {} })],
      [],
    );
    expect(out!.graph.nodes[0].config).toBeNull();
  });

  test("non-empty config is preserved", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A", { config: { threshold: 0.5 } })],
      [],
    );
    expect(out!.graph.nodes[0].config).toEqual({ threshold: 0.5 });
  });

  test("node-definition name falls back to data.label when nodeDefinitionName missing", () => {
    const node: Node = {
      id: "n1",
      type: "default",
      position: { x: 0, y: 0 },
      data: { label: "Resize" }, // no nodeDefinitionName
    };
    const out = serialiseCanvasForWizard([node], []);
    expect(out!.graph.nodes[0].node_definition_name).toBe("Resize");
  });

  test("position is captured", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A", { x: 100, y: 250 })],
      [],
    );
    expect(out!.graph.nodes[0].position).toEqual({ x: 100, y: 250 });
  });

  test("edge_ids in selection is always empty (UI selects nodes only)", () => {
    const out = serialiseCanvasForWizard(
      [n("n1", "A", { selected: true }), n("n2", "B")],
      [e("e1", "n1", "n2")],
    );
    expect(out!.selection.edge_ids).toEqual([]);
  });
});
