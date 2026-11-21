// service-stack-builder Group 0 — shared contracts (frontend).
// MUST mirror CORE/API/service_builder_schemas.py field names exactly.

export type ServiceSourceKind = "image" | "node" | "git" | "script" | "site";

export interface ServiceSource {
  kind: ServiceSourceKind;
  // image
  image?: string;
  // node (de-specialized into a long-lived service)
  node_definition_id?: number;
  // git
  repo_url?: string;
  ref?: string;
  build_context?: string;
  // script
  script?: string;
  base_image?: string;
  filename?: string;
  // site
  site_source?: "upload" | "repo";
  site_ref?: string;
}

export interface GpuReservation {
  vram_gb: number;
}

export interface ServiceSpec {
  name: string;
  source: ServiceSource;
  env: Record<string, string>;
  ports: number[]; // container ports (never host ports — see exposure)
  volumes: string[];
  gpu?: GpuReservation | null;
  replicas: number;
  cpu: number;
  mem_gb: number;
  expose: boolean;
  command?: string | null;
}

export interface AssembleRequest {
  name: string;
  services: ServiceSpec[];
}

export interface AssembleResponse {
  compose_spec: Record<string, unknown>;
  notes: Record<string, string>;
}

export interface EstimateServiceUnits {
  name: string;
  cpu: number;
  mem_gb: number;
  gpu: number[]; // reserved vram_gb per GPU
}

export interface EstimateRequest {
  services: EstimateServiceUnits[];
}

export interface EstimateServiceCost {
  name: string;
  credits_per_hour: number;
}

export interface EstimateResponse {
  per_service: EstimateServiceCost[];
  total_credits_per_hour: number;
  projected_credits_per_month: number;
  rates: Record<string, number>; // {cpu, mem, gpu}
  metering_enabled: boolean;
}

// Pre-deploy fleet placement verdict (POST /v1/stacks/placement-check) —
// computed by the same engine a deploy claim uses, so the field can never
// promise what a deploy would refuse.
export interface PlacementCheckResponse {
  placeable: boolean;
  services: Array<{
    name: string;
    placeable: boolean;
    best_worker?: string | null;
    shortfall?: {
      needed_gb: number;
      max_available_gb: number | null;
      kind: string | null;
    } | null;
  }>;
  message: string;
}

// Convenience factory for a blank service row in the builder.
export function blankService(name: string): ServiceSpec {
  return {
    name,
    source: { kind: "image" },
    env: {},
    ports: [],
    volumes: [],
    gpu: null,
    replicas: 1,
    cpu: 0,
    mem_gb: 0,
    expose: false,
    command: null,
  };
}
