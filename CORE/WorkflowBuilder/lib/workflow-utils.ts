// In WorkflowBuilder/lib/workflow-utils.ts

import type { Node, XYPosition } from "reactflow"
import type { NodeData, NodeDefinition } from "./types"

let nodeIdCounter = 0

export const generateNodeId = (type: string): string => {
  nodeIdCounter++
  return `${type}-${nodeIdCounter}`
}

interface CreateNodeFromDefinitionParams {
  nodeDef: NodeDefinition & { type: string };
  position: XYPosition;
}

export const createNode = ({ nodeDef, position }: CreateNodeFromDefinitionParams): Node<NodeData> => {
  const newNode: Node<NodeData> = {
    id: generateNodeId(nodeDef.type),
    type: nodeDef.type,
    position,
    data: {
      label: nodeDef.name,
      description: nodeDef.description,
      nodeDefinitionId: nodeDef.id,
      // --- Copy all relevant properties from the API definition ---
      process_type: nodeDef.process_type,
      input_schema: nodeDef.input_schema,
      output_schema: nodeDef.output_schema,
      configuration_schema: nodeDef.configuration_schema,
      custom_metadata: nodeDef.custom_metadata,
      cost_in_credits: nodeDef.cost_in_credits,
      // LDF-3 §3 — chip needs gpu_variants on the canvas-node data so
      // the bottom-right chip resolves the processing-requirement label
      // without re-fetching the NodeDefinition.
      gpu_variants: (nodeDef as any).gpu_variants,
      // GO.9 — mirror per-accelerator status alongside gpu_variants.
      variant_status: (nodeDef as any).variant_status,
      node_kind: nodeDef.node_kind,
      // node-version-pinning — pin to the node's current build at add time, and
      // carry the current version for the canvas badge / update detection.
      pinnedNodeVersionId: (nodeDef as any).current_node_version_id,
      currentNodeVersion: (nodeDef as any).current_version,
      currentNodeVersionId: (nodeDef as any).current_node_version_id,
      // FX.103 p4 — carry the deprecation/retirement lifecycle onto the canvas
      // node so an existing workflow's node header badges it. Palette payload
      // names are lifecycle_state / retire_at (part-2 aliases).
      lifecycleState: (nodeDef as any).lifecycle_state ?? (nodeDef as any).status,
      retireAt: (nodeDef as any).retire_at ?? (nodeDef as any).retire_after,
    },
  };
  return newNode;
};