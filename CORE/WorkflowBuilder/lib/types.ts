// In WorkflowBuilder/lib/types.ts

import type { Node, Edge } from "reactflow";

// Add the 'status' and 'node_kind' properties to the NodeData interface
export interface PreviewAsset {
  url: string;
  file_extension: string;
  category: 'image' | 'model3d' | 'video' | 'audio' | 'text' | 'pdf' | 'splat' | 'pointcloud' | 'font' | 'archive' | 'other';
  content_hash: string;
  logical_name: string;
}

export interface NodeData {
  label: string;
  description?: string;
  process_type?: string;
  input_schema?: { [key: string]: any };
  output_schema?: { [key: string]: any };
  configuration_schema?: { [key: string]: any };
  custom_metadata?: { [key: string]: any };
  cost_in_credits?: number;
  // LDF-3 §3 — chip surface needs gpu_variants on the canvas-node data.
  // Mirror of NodeDefinition.gpu_variants. Hydrated when a node lands on
  // the canvas (drag from library, wizard apply, version load).
  gpu_variants?: Record<string, unknown>;
  // GO.9 — mirror of NodeDefinition.variant_status, hydrated alongside
  // gpu_variants so the right-rail node detail panel can render per-
  // accelerator verified/unverified/incompatible/not_recommended badges
  // without re-fetching the NodeDefinition.
  variant_status?: Record<string, { status: string; perf_ratio?: number | null }> | null;
  nodeDefinitionId?: number;
  // node-version-pinning — the immutable NodeVersion this canvas node is pinned
  // to. Stamped to the node's current version when dropped, and rewritten by the
  // "update to vN" action. Persists in graph_json.data and is honored at run
  // creation (create_workflow_run) so a later rebuild can't change what runs.
  pinnedNodeVersionId?: number;
  // Mirrors NodeDefinition.current_version / current_node_version_id from the
  // palette response, so the canvas can badge "vN" and detect "update available"
  // (pinned < current) without re-fetching.
  currentNodeVersion?: number;
  currentNodeVersionId?: number;
  // FX.103 p4 — node deprecation/retirement lifecycle (part-2 alias names the
  // palette API returns: lifecycle_state = NodeDefinition.status, retire_at =
  // retire_after). Hydrated when a node lands on the canvas and refreshed by
  // the node-config panel's live-def fetch, so an existing workflow containing
  // a deprecated node badges its header without re-fetching. NOT to be
  // confused with NodeData.status, which is a RUN state (queued/running/...).
  lifecycleState?: 'active' | 'deprecated' | 'retired';
  retireAt?: string | null;
  outputCount?: number;
  fileName?: string;
  file?: any;
  fileId?: string;
  // 'blocked' is set transiently by hooks/useWorkflowApi.ts when a
  // node is gated behind a paid plan or missing dependency.
  // 'pending_action' is emitted by the backend for approval nodes waiting on user action.
  status?: 'queued' | 'running' | 'success' | 'failure' | 'blocked' | 'pending_action';
  node_kind?: string;
  // generalized-stack-wake D12 — per-instance managed-stack assignment. Set from
  // the node config panel's "Managed stack" selector; when present it ties THIS
  // node instance to the named managed stack (overriding the node_kind auto-
  // binding), and clearing it makes the node self-contained. Persisted into the
  // workflow graph_json.data on save like any other node-data field.
  requires_stack?: string;
  selectedFiles?: Array<{
    id: string;
    name: string;
    cog_type: string;
    parent_id: string | null;
  }>;
  calculatedFileCount?: number | null;
  previewAssets?: PreviewAsset[];
  error_message?: string;
  started_at?: string;
  inputPreviewUrl?: string;
  inputPreviewCategory?: string;
  // User-supplied alias for a node instance — surfaces in the node
  // header so two nodes of the same node_kind can be told apart.
  nickname?: string;
  // Per-tenant secrets selected for this node instance. Persisted to
  // the workflow's graph_json; resolved server-side at run time.
  // Shape: each entry binds an env_var to the secret_name the user
  // chose from the tenant's secret store.
  secret_refs?: Array<{ secret_name: string; env_var: string }>;
  // Free-form key/value bag for runtime overrides (env vars, scalar
  // configuration knobs). Saved to graph_json.
  extraVariables?: Record<string, string | number | boolean>;
  // File-input nodes: list of MIME types or file extensions the node
  // accepts. Validated client-side at attach time.
  accepted_formats?: string[];
  // File-input nodes: subset of cog metadata fields the user picked
  // to forward as inputs (alongside the file itself).
  selectedMetadata?: string[];
  // NIM-backed nodes: catalog selection (model id + tag).
  _nimModels?: Array<{ id: string; tag?: string }>;
  // Output nodes belonging to a specific run — populated when the
  // node is rendered inside the run-history viewer.
  workflow_run_id?: number;
}

export type WorkflowNode = Node<NodeData>;
export type { Edge };

// Represents the full workflow data with nodes and edges
export interface Workflow {
  id: number;
  name: string;
  nodes: WorkflowNode[];
  edges: Edge[];
  latest_version_id?: number;
}

// Represents the summary data from the initial API call
export interface WorkflowSummary {
  id: number;
  name:string;
  latest_version_id: number;
  // Archive state (soft-hide). Absent/null = visible; set = archived.
  archived_at?: string | null;
}

// Represents a single version of a workflow from the API
export interface WorkflowVersion {
  id: number;
  version_number: number;
  graph_json: {
    nodes: WorkflowNode[];
    edges: Edge[];
    [key: string]: any;
  };
  created_at: string;
}

// Represents a single node definition from your API
export interface NodeDefinition {
    id: number;
    name: string;
    description: string;
    process_type: string;
    input_schema: {
        input_type: string;
        [key: string]: any;
    };
    output_schema: {
        output_type: string;
        [key: string]: any;
    };
    configuration_schema?: { [key: string]: any };
    custom_metadata?: { [key: string]: any };
    cost_in_credits?: number;
    // LDF-3 §3 — surfaced on the canvas chip + library hover + right rail.
    gpu_variants?: Record<string, unknown>;
    // GO.9 — honest per-accelerator labels. `{variant: {status, perf_ratio}}`,
    // status one of verified/unverified/incompatible/not_recommended.
    // Derived server-side (utils.queue_selector.compute_variant_status) from
    // gpu_variants + incompatible_reasons + variant_verification_status +
    // not_recommended_variants + variant_performance. Populated by the node
    // list/detail endpoints; None/undefined when the node has no variants.
    variant_status?: Record<string, { status: string; perf_ratio?: number | null }> | null;
    // FX.73 p4c — CE-edition badge. True when the node is included in the CE
    // (open-source) edition: the API's single source of truth
    // (node_definitions.ce_included, stamped at CE seed time from the node-seed
    // keep-list) on the palette payload. null/undefined => no badge.
    ce_included?: boolean | null;
    node_kind?: string;
    tenant_id?: number | null;
    scan_status?: string;
    scan_result?: { [key: string]: any };
    rejection_reason?: string;
    calibration_status?: string;
    calibration_progress?: { completed: number; total: number };
    [key: string]: any;
}

// --- Workflow Export/Import Types ---
export interface WorkflowExport {
    export_version: string;
    name: string;
    description?: string;
    graph_json: {
        nodes: WorkflowNode[];
        edges: Edge[];
    };
    required_nodes: string[];  // List of dag_ids needed
}

export interface WorkflowImportResult {
    success: boolean;
    workflow_id?: number;
    workflow_name: string;
    missing_nodes: string[];
    message: string;
}

export interface WorkflowValidateResult {
    valid: boolean;
    missing_nodes: string[];
    available_nodes: string[];
    message: string;
}