/**
 * Pure DAG-metadata renderer — TypeScript mirror of the canonical
 * implementation at `CORE/API/utils/metadata_template.py`
 * (and its duplicate at `CORE/API/utils/metadata_template.py`).
 *
 * This file drives the New Node wizard's live preview of the
 * `<Name>_<Type>_PROCESS_Node.py` metadata block. The user sees exactly
 * what will get committed — the server-side renderer re-runs on commit
 * from the same metadata dict and produces byte-identical output.
 *
 * Parity with the Python renderer is enforced by
 * `CORE/API/tests/test_dag_template.py` (shared fixtures) and
 * `CORE/WorkflowBuilder/__tests__/metadataTemplate.test.ts`. If you edit
 * this file, edit the Python side and the fixtures in lockstep, or
 * the test fails and the wizard preview silently drifts from what
 * ships.
 *
 * DAGs intentionally do NOT carry variant/queue info. `process_type`
 * and `gpu_variants` live on `NodeDefinition`; the scheduler reads
 * them at enqueue time. Removing them here means the wizard preview
 * exactly matches what gets committed (no trailing `process_type`
 * line to explain).
 */

export interface NodeMetadata {
  node_name: string;
  executable_path: string;
  description?: string;
  input_type?: string;
  output_type?: string;
  cost_in_credits?: number;
  tenant_id?: number | null;
  container_timeout?: number;
  container_env_extras?: Record<string, string> | null;
  accepted_formats?: string[] | null;
}

export interface RenderedNode {
  filename: string;
  content: string;
  node_kind: string;
  category: string;
}

// Common-category hints. NOT a validation gate — the DAG renderer
// accepts any string the wizard / LLM passes for input_type /
// output_type (see openType() below) so domain-specific labels like
// "ifc", "parquet", "dicom" survive end-to-end. Kept exported because
// downstream consumers may want the list of well-known categories for
// suggestion UI.
export const COMMON_INPUT_TYPES = new Set([
  "image",
  "video",
  "mesh",
  "audio",
  "text",
  "texture",
  "pointcloud",
  "pdf",
  "document",
  "json",
  "csv",
  "binary",
  "other",
]);
export const COMMON_OUTPUT_TYPES = COMMON_INPUT_TYPES;
// Kept defined for non-DAG consumers (wizard's process_type picker on
// Review step, which writes into NodeDefinition, not the DAG).
export const VALID_PROCESS_TYPES = new Set([
  "CPU",
  "CUDA",
  "ROCM",
  "INTEL",
  "NPU",
  "ACCELERATOR",
]);

export function toSnake(name: string): string {
  return name
    .replace(/[^a-zA-Z0-9\s]/g, "")
    .trim()
    .replace(/\s+/g, "_")
    .toLowerCase();
}

export function safeName(name: string): string {
  return name.replace(/ /g, "").replace(/[^a-zA-Z0-9]/g, "");
}

export function inferCategory(inputType: string, outputType: string): string {
  const map: Record<string, string> = {
    image: "Texture",
    texture: "Texture",
    mesh: "Mesh",
    video: "Video",
    audio: "Audio",
    text: "General",
  };
  const out = map[(outputType || "").toLowerCase()];
  if (out) return out;
  const inp = map[(inputType || "").toLowerCase()];
  return inp || "General";
}

export function defaultDescription(
  nodeName: string,
  inputType: string,
  outputType: string,
): string {
  return `${nodeName} - processes ${inputType} files and produces ${outputType} output`;
}

export function defaultAcceptedFormats(inputType: string): string[] {
  const map: Record<string, string[]> = {
    image: ["jpg", "jpeg", "png", "tif", "tiff"],
    texture: ["jpg", "jpeg", "png", "tif", "tiff"],
    video: ["mp4", "mov", "mkv", "webm"],
    audio: ["wav", "mp3", "flac", "ogg"],
    mesh: ["obj", "fbx", "gltf", "glb", "stl"],
    text: ["txt", "md", "json", "yaml", "csv"],
    pdf: ["pdf"],
    document: ["docx", "doc", "odt", "rtf"],
    pointcloud: ["ply", "pcd", "las", "laz", "xyz"],
    json: ["json"],
    csv: ["csv", "tsv"],
  };
  return map[(inputType || "").toLowerCase()] || [];
}

export function renderFilename(
  nodeName: string,
  category: string,
  tenantId: number | null | undefined,
): string {
  const base = safeName(nodeName);
  if (tenantId !== null && tenantId !== undefined) {
    return `tenant_${tenantId}_${base}_PROCESS_Node.py`;
  }
  return `${base}_${category.toUpperCase()}_PROCESS_Node.py`;
}

/**
 * Compact JSON matching Python's `json.dumps(obj, separators=(", ", ": "))`:
 * ", " between items and ": " between keys/values. A naive regex on
 * `JSON.stringify` output would corrupt commas inside string values, so
 * we emit the shapes we need (string / number / bool / null / string[]
 * / Record<string,string>) explicitly.
 */
function jsonDumpCompact(obj: unknown): string {
  if (obj === null) return "null";
  if (typeof obj === "boolean") return obj ? "true" : "false";
  if (typeof obj === "number") return String(obj);
  if (typeof obj === "string") return JSON.stringify(obj);
  if (Array.isArray(obj)) {
    return "[" + obj.map(jsonDumpCompact).join(", ") + "]";
  }
  if (typeof obj === "object") {
    const parts: string[] = [];
    for (const k of Object.keys(obj as Record<string, unknown>)) {
      parts.push(JSON.stringify(k) + ": " + jsonDumpCompact((obj as Record<string, unknown>)[k]));
    }
    return "{" + parts.join(", ") + "}";
  }
  return "null";
}

export function coerceAndValidate(metadata: Partial<NodeMetadata>): NodeMetadata {
  const m = metadata || {};
  let name = (m.node_name || "").trim();
  if (!name) name = "Untitled Node";

  let exe = (m.executable_path || "").trim();
  if (!exe) exe = safeName(name).toLowerCase() + ":latest";

  const enumOrDefault = (v: unknown, valid: Set<string>, def: string): string =>
    typeof v === "string" && valid.has(v) ? v : def;

  // input_type / output_type are open strings — any non-empty value
  // the LLM (or user) provides flows through verbatim (lowercased) so
  // domain-specific labels like "ifc", "parquet", "dicom" survive into
  // the rendered DAG metadata. Falls back to "other" only on
  // missing/non-string. Mirrors _open_type in the Python twin.
  const openType = (v: unknown, def: string): string => {
    if (typeof v !== "string") return def;
    const cleaned = v.trim().toLowerCase();
    return cleaned || def;
  };

  const inputType = openType(m.input_type, "other");
  const outputType = openType(m.output_type, "other");

  const clamp = (v: unknown, lo: number, hi: number, def: number): number => {
    const n = Number(v);
    if (!Number.isFinite(n) || !Number.isInteger(n)) return def;
    if (n < lo) return lo;
    if (n > hi) return hi;
    return n;
  };

  const costInCredits = clamp(m.cost_in_credits ?? 1, 0, 10_000, 1);
  const containerTimeout = clamp(m.container_timeout ?? 7200, 60, 86_400, 7200);

  let tenantId: number | null = null;
  if (m.tenant_id !== undefined && m.tenant_id !== null) {
    const n = Number(m.tenant_id);
    tenantId = Number.isFinite(n) && Number.isInteger(n) ? n : null;
  }

  const extras =
    m.container_env_extras && typeof m.container_env_extras === "object"
      ? m.container_env_extras
      : null;

  const formats =
    m.accepted_formats && Array.isArray(m.accepted_formats) ? m.accepted_formats : null;

  return {
    node_name: name.slice(0, 200),
    executable_path: exe.slice(0, 300),
    description: (m.description || "").trim().slice(0, 400),
    input_type: inputType,
    output_type: outputType,
    cost_in_credits: costInCredits,
    container_timeout: containerTimeout,
    tenant_id: tenantId,
    container_env_extras: extras,
    accepted_formats: formats,
  };
}

export function render(metadata: NodeMetadata): RenderedNode {
  const m = metadata;
  const nodeKind = toSnake(m.node_name);
  const category = inferCategory(m.input_type || "image", m.output_type || "image");
  const description =
    m.description ||
    defaultDescription(m.node_name, m.input_type || "image", m.output_type || "image");
  const acceptedFormats =
    m.accepted_formats !== undefined && m.accepted_formats !== null
      ? m.accepted_formats
      : defaultAcceptedFormats(m.input_type || "image");
  const tenantLine =
    m.tenant_id !== null && m.tenant_id !== undefined
      ? `tenant_id = ${m.tenant_id}`
      : "tenant_id = None";

  let configSchema: string;
  if (m.container_env_extras && Object.keys(m.container_env_extras).length > 0) {
    const extras: Record<string, string> = {};
    for (const k of Object.keys(m.container_env_extras)) extras[k] = "string";
    configSchema = `{"extra_variables": ${jsonDumpCompact(extras)}}`;
  } else {
    configSchema = `{"extra_variables": "NONE"}`;
  }

  const inputSchema =
    `{"input_type": "${m.input_type || "image"}", ` +
    `"file_mode": "both", ` +
    `"input_variables": "NONE"}`;
  const outputSchema =
    `{"output_type": "${m.output_type || "image"}", ` +
    `"file_mode": "both", ` +
    `"paired": True, ` +
    `"output_variables": "NONE"}`;
  const acceptedFormatsLiteral = jsonDumpCompact(acceptedFormats);

  const content =
    "# --- METADATA BLOCK ---\n" +
    `name = "${m.node_name}"\n` +
    `description = "${description}"\n` +
    `node_kind = "${nodeKind}"\n` +
    `category = "${category}"\n` +
    `executable_path = "${m.executable_path}"\n` +
    `input_schema = ${inputSchema}\n` +
    `output_schema = ${outputSchema}\n` +
    `configuration_schema = ${configSchema}\n` +
    `metadata = {"accepts_metadata": True, "metadata_required": False}\n` +
    `batch_mode = "batch"\n` +
    `accepted_formats = ${acceptedFormatsLiteral}\n` +
    `cost_in_credits = ${m.cost_in_credits ?? 1}\n` +
    `${tenantLine}\n` +
    `image_pull_policy = "always"\n` +
    `container_timeout = ${m.container_timeout ?? 7200}\n` +
    "# --- END METADATA BLOCK ---\n";

  return {
    filename: renderFilename(m.node_name, category, m.tenant_id),
    content,
    node_kind: nodeKind,
    category: category,
  };
}
