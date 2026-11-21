/**
 * LDF-4 #2/#3 — defensive backfill for graphs loaded from older
 * saved versions OR wizard outputs. Failure modes the fallback fixes:
 *
 * 1. **Missing `position`** — every node SHALL have `{x, y}` numeric
 *    coords. ReactFlow stacks position-less nodes at (0,0). Cascade
 *    diagonal positions.
 *
 * 2. **Missing edge `type`** — edges without `type: "custom"` skip
 *    the custom-edge component.
 *
 * 3. **Malformed edges** — drop edges with no `source` or `target`.
 *
 * 4. **Missing schema/render fields on `data`** — wizard-saved nodes
 *    contain only `{label, nodeDefinitionId}`; the input/process/
 *    output Node components expect `input_schema`, `output_schema`,
 *    `process_type`, `cost_in_credits`, `gpu_variants`, etc. to
 *    render handles. Without these, ReactFlow renders the node card
 *    body but no Handles → edges have nothing to connect to.
 *    Enrich each node's `data` from the live NodeDefinition catalog
 *    before setting state.
 *
 * Used by `handleVersionSelection`, `handleWizardApplied`, and the
 * import path. Pure logic in `safeLoadGraph` (no enrichment); async
 * `safeLoadGraphWithEnrichment` does the catalog fetch.
 */

export interface LoadableGraph {
  nodes?: any[];
  edges?: any[];
}

export interface SafeGraph {
  nodes: any[];
  edges: any[];
}

/** Backfill positions + edge types only. No catalog enrichment. */
export function safeLoadGraph(graph: LoadableGraph | null | undefined): SafeGraph {
  return safeLoadGraphWithDefs(graph, null);
}

/** Backfill positions + edge types + (when provided) enrich each
 *  node's `data` from a NodeDefinition lookup. Primary key is the
 *  numeric `nodeDefinitionId`; `defsByName` is a fallback keyed by the
 *  definition's `name` for graphs whose nodes carry only a
 *  `nodeDefinitionName` (the shape the workflow wizard emits — the
 *  builder LLM references catalog entries BY NAME, not id). Without the
 *  name fallback such a node resolves to no definition and pastes as a
 *  bare card with no schema → no <Handle> children → nothing to wire
 *  edges to. */
export function safeLoadGraphWithDefs(
  graph: LoadableGraph | null | undefined,
  defsById: Map<number, any> | null,
  defsByName?: Map<string, any> | null,
): SafeGraph {
  const incomingNodes = (graph?.nodes || []) as any[];
  const safeNodes = incomingNodes.map((n, i) => {
    const pos = (n && typeof n === "object" && n.position) ? n.position : null;
    const isFiniteNumber = (v: unknown): boolean =>
      typeof v === "number" && Number.isFinite(v);
    // LDF-4 #2a — fallback layout is a horizontal line with a 300px
    // visual gap between adjacent nodes. Stride = ~240px node body
    // + 300px gap = 540px. y is constant so nodes don't stack on top
    // of each other and don't drift diagonally.
    const safePos = (pos && isFiniteNumber(pos.x) && isFiniteNumber(pos.y))
      ? pos
      : { x: 80 + i * 540, y: 80 };

    // Enrich data with NodeDefinition fields when missing — wizard-
    // saved graphs only contain {label, nodeDefinitionId}. Without
    // input_schema / output_schema / process_type the input/process/
    // output Node components can't render their <Handle> children.
    let safeData = n?.data || {};
    const defId = safeData?.nodeDefinitionId;
    // Resolve the backing NodeDefinition: by numeric id first, then by
    // name (wizard-emitted nodes carry `nodeDefinitionName` — or a
    // matching `label` — but no id, so an id-only lookup silently misses
    // them and they render as bare, handle-less cards).
    let def =
      (defsById && typeof defId === "number") ? defsById.get(defId) : undefined;
    if (!def && defsByName) {
      const defName =
        (typeof safeData?.nodeDefinitionName === "string" && safeData.nodeDefinitionName) ||
        (typeof safeData?.label === "string" ? safeData.label : undefined);
      if (defName) def = defsByName.get(defName);
    }
    if (def) {
      safeData = {
        // start with the def's authoritative fields
        description: def.description,
        process_type: def.process_type,
        input_schema: def.input_schema,
        output_schema: def.output_schema,
        configuration_schema: def.configuration_schema,
        custom_metadata: def.custom_metadata,
        cost_in_credits: def.cost_in_credits,
        gpu_variants: def.gpu_variants,
        // GO.9 — mirror per-accelerator status alongside gpu_variants.
        variant_status: (def as any).variant_status,
        accepted_formats: def.accepted_formats,
        node_kind: def.node_kind,
        // overlay anything the saved graph already had (label,
        // nickname, user inputs etc.) — saved values win.
        ...safeData,
      };
      // Backfill the numeric id when we resolved BY NAME so downstream
      // id-keyed code (canvas chip, save round-trip, re-load) works the
      // same as a palette-dragged node. Only fills a gap — an id the
      // saved graph already carried is left untouched.
      if (typeof safeData.nodeDefinitionId !== "number") {
        safeData = { ...safeData, nodeDefinitionId: def.id };
      }
    }
    return { ...n, position: safePos, data: safeData };
  });
  // Build a quick id→data lookup for handle inference.
  const dataById = new Map<string, any>();
  for (const n of safeNodes) {
    if (n && n.id) dataById.set(n.id, n.data || {});
  }

  const incomingEdges = (graph?.edges || []) as any[];
  const safeEdges = incomingEdges
    .filter(e => e && typeof e === "object" && e.source && e.target)
    .map(e => ({
      ...e,
      type: e.type || "custom",
      // Infer sourceHandle from the source node's output_schema if
      // the saved edge didn't specify it. ReactFlow can fail to wire
      // an edge when the source/target node has named handles but
      // the edge has no handle id.
      sourceHandle: e.sourceHandle || inferHandleName(dataById.get(e.source)?.output_schema, "output"),
      targetHandle: e.targetHandle || inferHandleName(dataById.get(e.target)?.input_schema, "input"),
    }));
  return { nodes: safeNodes, edges: safeEdges };
}

/** Mirror of getSchemaEntries from input-node.tsx — returns the
 *  first handle name a node component would produce from its
 *  schema. Used to infer missing sourceHandle/targetHandle on
 *  edges from legacy graphs. */
function inferHandleName(schema: any, side: "input" | "output"): string | undefined {
  if (!schema || typeof schema !== "object") return undefined;
  const legacyKey = side === "input" ? "input_type" : "output_type";
  const legacy = schema[legacyKey];
  if (legacy && legacy !== "NONE") {
    const types = String(legacy).split(",").map(s => s.trim());
    return types.length > 1 ? `${side}_0` : side;
  }
  const entries = Object.keys(schema);
  return entries.length > 0 ? entries[0] : undefined;
}

/** Fetch the NodeDefinition catalog for the current user, then run
 *  the safeLoadGraph fallback with enrichment. */
export async function safeLoadGraphWithEnrichment(
  graph: LoadableGraph | null | undefined,
  apiUrl: string,
  accessToken: string | null,
): Promise<SafeGraph> {
  let defsById: Map<number, any> | null = null;
  let defsByName: Map<string, any> | null = null;
  if (accessToken) {
    try {
      const r = await fetch(`${apiUrl}/nodes/?limit=1000&offset=0`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
      });
      if (r.ok) {
        const list: any[] = await r.json();
        defsById = new Map(list.map(d => [d.id, d]));
        // Name-keyed fallback for wizard-emitted nodes that reference
        // catalog entries by name (nodeDefinitionName) rather than id.
        // Last write wins on duplicate names — acceptable; the id path
        // is the primary resolver.
        defsByName = new Map(
          list
            .filter(d => typeof d.name === "string" && d.name)
            .map(d => [d.name as string, d]),
        );
      }
    } catch {
      // Catalog fetch is best-effort; positions still backfill.
    }
  }
  return safeLoadGraphWithDefs(graph, defsById, defsByName);
}
