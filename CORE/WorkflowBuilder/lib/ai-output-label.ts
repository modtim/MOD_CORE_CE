/**
 * FX.72 p4 — "AI output labelled in the UI" (OWNER-DECISIONS Q55=C).
 *
 * Derives whether a canvas node is AI/generative from the fields the WB
 * actually hydrates onto NodeData (hooks/useWorkflowApi + lib/graph-load-fallback
 * merge these from the NodeDefinition: node_kind, process_type, custom_metadata,
 * gpu_variants — NOT the def's `type` category, which stays library-only).
 *
 * The match lists are EXPLICIT and documented — no fuzzy label matching (a
 * user nickname like "My AI Helper" must not trigger the badge).
 *
 * Provenance of the lists (repo-wide node_kind vocabulary, CORE/API seeds +
 * services + nim_ephemeral.py):
 *  - `nim_*` prefix  — every local/on-demand NVIDIA NIM model node
 *    (nim_studiovoice_local, nim_vsr_local, nim_bridge variants…).
 *  - text_llm, switchyard_llm, ai_conversation — LLM generation nodes
 *    (seed_text_llm_node.py, seed_switchyard_llm_node.py).
 *  - sdxl_generate, stable_diffusion, inpaint, depth_anything, cnn_image_classifier,
 *    local_ai, local_ai_train, unsloth_training, lora_train — diffusion/classifier/
 *    trainer nodes (CORE/DockerContainers names).
 *  - rag_query — RAG answer node (embeddings + LLM answer over the corpus).
 *
 * custom_metadata flags: a wizard-built node may carry `ai_model`, `ai`,
 * `generative`, or a `category`/`tags` value in the AI tag set below.
 */

export const AI_OUTPUT_LABEL_TEXT = "AI-generated";
export const AI_OUTPUT_TOOLTIP = "Produced by an AI model — review before use";

const AI_NODE_KINDS = new Set([
  "text_llm",
  "switchyard_llm",
  "ai_conversation",
  "rag_query",
  "sdxl_generate",
  "stable_diffusion",
  "inpaint",
  "depth_anything",
  "cnn_image_classifier",
  "local_ai",
  "local_ai_train",
  "unsloth_training",
]);

const AI_NODE_KIND_PREFIXES = ["nim_"];

// Lowercased category/tag words checked against custom_metadata.category,
// custom_metadata.tags[], and (when present) a node's `type` category.
const AI_CATEGORY_TAGS = new Set([
  "ai",
  "ml",
  "llm",
  "ai model",
  "generation",
  "generative",
  "generative ai",
  "image generation",
  "text generation",
  "language model",
  "diffusion",
  "embedding",
  "classifier",
]);

export function isAiNode(nodeData: Record<string, any>): boolean {
  const kind = ((nodeData?.node_kind ?? "") as string).toLowerCase();
  if (kind && AI_NODE_KINDS.has(kind)) return true;
  if (AI_NODE_KIND_PREFIXES.some((p) => kind.startsWith(p))) return true;

  const meta = nodeData?.custom_metadata;
  if (meta && typeof meta === "object") {
    if (meta.ai === true || meta.ai_model === true || meta.generative === true) return true;
    const metaCategory = typeof meta.category === "string" ? meta.category.toLowerCase() : "";
    if (AI_CATEGORY_TAGS.has(metaCategory)) return true;
    if (Array.isArray(meta.tags)) {
      for (const tag of meta.tags) {
        if (typeof tag === "string" && AI_CATEGORY_TAGS.has(tag.toLowerCase())) return true;
      }
    }
  }

  // NodeDefinition.type (palette category, e.g. "AI Model") — only present on
  // library rows, harmless when absent on canvas data.
  const type = typeof nodeData?.type === "string" ? nodeData.type.toLowerCase() : "";
  if (AI_CATEGORY_TAGS.has(type)) return true;

  return false;
}

/**
 * FX.72 p4b — "AI-generated" badge on terminal output-node previews.
 *
 * An Output node has no producer fields of its own: the badge must come from
 * the node that FEEDS it. This pure helper answers "is any upstream producer
 * of `nodeId` an AI node?" over plain React Flow shapes (Node/Edge), so it is
 * unit-testable without a live graph. Callers pass the memoised `useNodes()`
 * / `useEdges()` hook results.
 *
 * Only direct (distance-1) producers count: an Output fed by a converter that
 * is fed by an LLM shows the converter's provenance, not the LLM's — matching
 * how process-node.tsx labels only its own node.
 */
export function upstreamIsAi(
  nodeId: string,
  nodes: { id: string; data?: any }[],
  edges: { source?: string; target?: string }[],
): boolean {
  if (!nodeId || !nodes || !edges) return false;

  const dataById = new Map(nodes.map((n) => [n.id, n.data]));
  for (const edge of edges) {
    if (edge.target !== nodeId) continue;
    const sourceData = dataById.get(edge.source ?? "");
    if (sourceData && isAiNode(sourceData)) return true;
  }
  return false;
}
