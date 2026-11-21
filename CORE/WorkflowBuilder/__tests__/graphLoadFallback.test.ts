/**
 * LDF-4 #2/#3 — graph load fallback. Reproduces Tim's "Generate
 * Albedo Map" stage scenario where the saved graph_json had no
 * position fields and edges lacked `type: "custom"`.
 */
import { safeLoadGraph, safeLoadGraphWithDefs } from "@/lib/graph-load-fallback";

describe("safeLoadGraph", () => {
  it("Tim's Generate Albedo Map (v1) — no positions, no edge types", () => {
    // Exact shape pulled from the stage DB
    const graph = {
      nodes: [
        { id: "n1", data: { label: "Image Input", nodeDefinitionId: 37 }, type: "input" },
        { id: "n2", data: { label: "Albedo Texture Process", nodeDefinitionId: 224 }, type: "process" },
        { id: "n3", data: { label: "Return Assets Export", nodeDefinitionId: 167 }, type: "output" },
      ],
      edges: [
        { id: "e1", source: "n1", target: "n2" },
        { id: "e2", source: "n2", target: "n3" },
      ],
    };
    const out = safeLoadGraph(graph);
    // All 3 nodes get positions
    expect(out.nodes).toHaveLength(3);
    for (const n of out.nodes) {
      expect(typeof n.position.x).toBe("number");
      expect(typeof n.position.y).toBe("number");
    }
    // Positions are distinct (no stacking at 0,0)
    const positions = out.nodes.map(n => `${n.position.x},${n.position.y}`);
    expect(new Set(positions).size).toBe(3);
    // Both edges get type: "custom"
    expect(out.edges).toHaveLength(2);
    for (const e of out.edges) {
      expect(e.type).toBe("custom");
    }
  });

  it("preserves explicit positions when present", () => {
    const graph = {
      nodes: [
        { id: "n1", data: {}, type: "input", position: { x: 500, y: 300 } },
        { id: "n2", data: {}, type: "process", position: { x: 800, y: 400 } },
      ],
      edges: [],
    };
    const out = safeLoadGraph(graph);
    expect(out.nodes[0].position).toEqual({ x: 500, y: 300 });
    expect(out.nodes[1].position).toEqual({ x: 800, y: 400 });
  });

  it("preserves edge type when present", () => {
    const graph = {
      nodes: [],
      edges: [
        { id: "e1", source: "a", target: "b", type: "smoothstep" },
      ],
    };
    const out = safeLoadGraph(graph);
    expect(out.edges[0].type).toBe("smoothstep");
  });

  it("drops malformed edges with no source/target", () => {
    const graph = {
      nodes: [],
      edges: [
        { id: "e1", source: "a", target: "b" },
        { id: "e2", source: "a" },                  // no target
        { id: "e3", target: "c" },                  // no source
        null as any,
        "not-an-edge" as any,
      ],
    };
    const out = safeLoadGraph(graph);
    expect(out.edges).toHaveLength(1);
    expect(out.edges[0].id).toBe("e1");
  });

  it("handles null / undefined / empty input", () => {
    expect(safeLoadGraph(null)).toEqual({ nodes: [], edges: [] });
    expect(safeLoadGraph(undefined)).toEqual({ nodes: [], edges: [] });
    expect(safeLoadGraph({})).toEqual({ nodes: [], edges: [] });
    expect(safeLoadGraph({ nodes: [], edges: [] })).toEqual({ nodes: [], edges: [] });
  });

  it("LDF-4 #2a — backfilled positions are a horizontal line, 20px gap", () => {
    const graph = {
      nodes: [
        { id: "n1", data: {}, type: "input" },
        { id: "n2", data: {}, type: "input" },
        { id: "n3", data: {}, type: "input" },
        { id: "n4", data: {}, type: "input" },
      ],
      edges: [],
    };
    const out = safeLoadGraph(graph);
    const xs = out.nodes.map(n => n.position.x);
    const ys = out.nodes.map(n => n.position.y);
    // Each x is unique + strictly increasing (no stacking, no diagonal).
    expect(new Set(xs).size).toBe(4);
    for (let i = 1; i < xs.length; i++) {
      expect(xs[i]).toBeGreaterThan(xs[i - 1]);
    }
    // y is constant — horizontal line.
    expect(new Set(ys).size).toBe(1);
    // Stride is 540 (≈240 px node body + 300 px visual gap).
    expect(xs[1] - xs[0]).toBe(540);
  });

  it("handles non-numeric position fields (NaN, string, null)", () => {
    const graph = {
      nodes: [
        { id: "n1", data: {}, type: "input", position: { x: NaN, y: 0 } },
        { id: "n2", data: {}, type: "input", position: { x: "100" as any, y: 200 } },
        { id: "n3", data: {}, type: "input", position: null as any },
      ],
      edges: [],
    };
    const out = safeLoadGraph(graph);
    for (const n of out.nodes) {
      expect(typeof n.position.x).toBe("number");
      expect(typeof n.position.y).toBe("number");
      expect(Number.isNaN(n.position.x)).toBe(false);
    }
  });
});

describe("safeLoadGraphWithDefs — enrichment", () => {
  it("Tim's #2 deeper bug — wizard-saved data is {label, nodeDefinitionId} only; needs schema/process_type/cost backfill from catalog so handles can render", () => {
    const graph = {
      nodes: [
        { id: "n1", data: { label: "Image Input", nodeDefinitionId: 387 }, type: "input" },
        { id: "n2", data: { label: "Albedo Texture Process", nodeDefinitionId: 412 }, type: "process" },
      ],
      edges: [{ id: "e1", source: "n1", target: "n2" }],
    };
    const defsById = new Map<number, any>([
      [387, {
        id: 387, name: "Image Input", description: "Input node",
        process_type: "Input", input_schema: { input_type: "NONE" },
        output_schema: { output_type: "image" },
        configuration_schema: {},
        cost_in_credits: 0, gpu_variants: { cpu: "img:cpu" },
      }],
      [412, {
        id: 412, name: "Albedo Texture Process", description: "Albedo",
        process_type: "GPU", input_schema: { input_type: "image" },
        output_schema: { output_type: "image" },
        configuration_schema: {},
        cost_in_credits: 5, gpu_variants: { cuda: "albedo:cuda" },
      }],
    ]);
    const out = safeLoadGraphWithDefs(graph, defsById);
    // Node 1 enriched
    expect(out.nodes[0].data.input_schema).toEqual({ input_type: "NONE" });
    expect(out.nodes[0].data.output_schema).toEqual({ output_type: "image" });
    expect(out.nodes[0].data.process_type).toBe("Input");
    expect(out.nodes[0].data.cost_in_credits).toBe(0);
    expect(out.nodes[0].data.gpu_variants).toEqual({ cpu: "img:cpu" });
    // Saved label preserved
    expect(out.nodes[0].data.label).toBe("Image Input");
    expect(out.nodes[0].data.nodeDefinitionId).toBe(387);
    // Node 2 enriched
    expect(out.nodes[1].data.process_type).toBe("GPU");
    expect(out.nodes[1].data.cost_in_credits).toBe(5);
  });

  it("saved values win over catalog values (e.g. label override)", () => {
    const graph = {
      nodes: [
        { id: "n1", data: { label: "Custom Name", nodeDefinitionId: 42, cost_in_credits: 99 }, type: "input" },
      ],
      edges: [],
    };
    const defsById = new Map<number, any>([
      [42, { id: 42, name: "Default Name", cost_in_credits: 5, input_schema: {} }],
    ]);
    const out = safeLoadGraphWithDefs(graph, defsById);
    expect(out.nodes[0].data.label).toBe("Custom Name");
    expect(out.nodes[0].data.cost_in_credits).toBe(99); // saved wins
    expect(out.nodes[0].data.input_schema).toEqual({}); // catalog fills gap
  });

  it("nodeDefinitionId not in catalog — leaves data as-is (still backfills position)", () => {
    const graph = {
      nodes: [
        { id: "n1", data: { label: "Orphan", nodeDefinitionId: 9999 }, type: "input" },
      ],
      edges: [],
    };
    const defsById = new Map<number, any>(); // empty
    const out = safeLoadGraphWithDefs(graph, defsById);
    expect(out.nodes[0].data).toEqual({ label: "Orphan", nodeDefinitionId: 9999 });
    expect(typeof out.nodes[0].position.x).toBe("number");
  });

  it("name fallback — wizard node carries nodeDefinitionName (no id) → resolves by name + backfills id", () => {
    const graph = {
      nodes: [
        { id: "n1", data: { label: "Image Input", nodeDefinitionName: "Image Input" }, type: "input" },
      ],
      edges: [],
    };
    const def = {
      id: 387, name: "Image Input", description: "Input node",
      process_type: "Input", input_schema: { input_type: "NONE" },
      output_schema: { output_type: "image" }, configuration_schema: {},
      cost_in_credits: 0, gpu_variants: { cpu: "img:cpu" },
    };
    const defsByName = new Map<string, any>([["Image Input", def]]);
    const out = safeLoadGraphWithDefs(graph, null, defsByName);
    // Schema backfilled from catalog so handles can render.
    expect(out.nodes[0].data.input_schema).toEqual({ input_type: "NONE" });
    expect(out.nodes[0].data.output_schema).toEqual({ output_type: "image" });
    expect(out.nodes[0].data.process_type).toBe("Input");
    // Numeric id backfilled from the resolved definition.
    expect(out.nodes[0].data.nodeDefinitionId).toBe(387);
    // Original name preserved.
    expect(out.nodes[0].data.nodeDefinitionName).toBe("Image Input");
  });

  it("name fallback — falls back to data.label when nodeDefinitionName absent", () => {
    const graph = {
      nodes: [{ id: "n1", data: { label: "Resize Image Process" }, type: "process" }],
      edges: [],
    };
    const def = { id: 5, name: "Resize Image Process", process_type: "CPU", input_schema: { input_type: "image" } };
    const defsByName = new Map<string, any>([["Resize Image Process", def]]);
    const out = safeLoadGraphWithDefs(graph, null, defsByName);
    expect(out.nodes[0].data.process_type).toBe("CPU");
    expect(out.nodes[0].data.nodeDefinitionId).toBe(5);
  });

  it("name fallback — unknown name resolves to nothing, data left as-is", () => {
    const graph = {
      nodes: [{ id: "n1", data: { nodeDefinitionName: "Nonexistent Placeholder" }, type: "process" }],
      edges: [],
    };
    const defsByName = new Map<string, any>([["Image Input", { id: 1, name: "Image Input" }]]);
    const out = safeLoadGraphWithDefs(graph, null, defsByName);
    expect(out.nodes[0].data).toEqual({ nodeDefinitionName: "Nonexistent Placeholder" });
  });

  it("id resolution wins over name — id present takes priority, no name lookup", () => {
    const graph = {
      nodes: [{ id: "n1", data: { label: "Image Input", nodeDefinitionId: 387 }, type: "input" }],
      edges: [],
    };
    const byId = new Map<number, any>([[387, { id: 387, name: "Image Input", process_type: "Input", input_schema: { input_type: "NONE" } }]]);
    const byName = new Map<string, any>([["Image Input", { id: 999, name: "Image Input", process_type: "WRONG" }]]);
    const out = safeLoadGraphWithDefs(graph, byId, byName);
    expect(out.nodes[0].data.process_type).toBe("Input"); // id def, not name def
    expect(out.nodes[0].data.nodeDefinitionId).toBe(387);
  });

  it("null defsById — same behaviour as plain safeLoadGraph", () => {
    const graph = {
      nodes: [{ id: "n1", data: { label: "X" }, type: "input" }],
      edges: [],
    };
    const out = safeLoadGraphWithDefs(graph, null);
    expect(out.nodes[0].data).toEqual({ label: "X" });
  });
});
