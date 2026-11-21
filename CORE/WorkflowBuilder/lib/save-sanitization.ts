// Save-time sanitization helpers (issue #21).
//
// When the user saves a workflow, some nodes carry per-run inputs the
// user keyed in (uploaded files, parameter overrides, flag selections,
// API key references). Those values are useful WHILE the user is
// running the workflow themselves but are noise — and possibly leak
// secrets — when the workflow is shared or reused as a template.
//
// This module owns the canonical list of "user-input" data keys and
// the helpers the save flow uses to detect them, strip them, and
// remember per-workflow suppression of the prompt.

/**
 * Curated allowlist of data-key names that a node carries to track
 * a user's per-run input. New input mechanisms should be added here
 * (one place, both the prompt detection and the clean-copy stripper
 * read from this list).
 *
 * LDF-3 §4 — extended to cover the file-input fields the Input Node
 * actually uses (`selectedFiles`, `fileId`, `fileName`, `file`) plus
 * the runtime-overrides bag (`extraVariables`, `extraVariableValues`)
 * and per-tenant secret bindings (`secret_refs`). Tim's stage-test
 * "Save a clean copy" still kept the uploaded image because none of
 * those keys were in the allowlist.
 */
export const USER_INPUT_KEY_ALLOWLIST: readonly string[] = [
  "inputValue", "input_value",
  "userInput", "user_input",
  "selectedFlags", "selected_flags",
  "uploadedFile", "uploaded_file", "uploadedFiles", "uploaded_files",
  "paramValues", "param_values",
  // File-input fields used by Input Node — uploaded images, single
  // files, file IDs.
  "selectedFiles", "selected_files",
  "fileId", "file_id",
  "fileName", "file_name",
  "file",
  // Runtime override bags + secret bindings.
  "extraVariables", "extra_variables",
  "extraVariableValues", "extra_variable_values",
  "secret_refs", "secretRefs",
];

/**
 * Permissive prefix-match for keys that begin with `input`, `user`, or
 * `param` and continue with an uppercase letter — covers field names
 * like `inputUrl`, `userPrompt`, `paramThreshold`. Underscore variants
 * are explicitly enumerated in the allowlist instead so we don't
 * accidentally match system metadata keys like `input_schema`,
 * `input_type`, `param_schema`.
 */
export const USER_INPUT_KEY_REGEX = /^(input|user|param)[A-Z]/;

/** Keys that look like user input (start with input/user/param) but
 *  are actually system metadata. Belt-and-braces against the regex —
 *  if we ever loosen the regex these stay denied. */
const SCHEMA_DENYLIST = new Set<string>([
  "input_schema", "output_schema", "configuration_schema", "custom_metadata",
  "input_type", "output_type", "input_value_pairs",
]);

/** Does this single key qualify as user-input under our policy? */
export function isUserInputKey(key: string): boolean {
  if (SCHEMA_DENYLIST.has(key)) return false;
  if (USER_INPUT_KEY_ALLOWLIST.includes(key)) return true;
  return USER_INPUT_KEY_REGEX.test(key);
}

/**
 * LDF-3 §4 — emptiness check used by both the prompt-firing rule and
 * the snapshot. A user-input key whose value is "empty" should NOT
 * trigger the dialog (Tim's #3: workflow with no actual inputs keyed
 * still triggered the prompt because keys EXISTED on the data object).
 *
 * Empty: null, undefined, empty string, empty array, empty object,
 * boolean `false`. Any other value counts as non-empty.
 */
export function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (value === false) return true;
  if (typeof value === "string") return value.length === 0;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") {
    return Object.keys(value as Record<string, unknown>).length === 0;
  }
  return false;
}

/**
 * Generic node shape — we only need `data` to be an object-or-undefined
 * so this works against ReactFlow's typed `Node<NodeData>` AND raw JSON
 * nodes without pulling reactflow types into a non-React module. Using
 * `object` (rather than `Record<string, unknown>`) keeps the helper
 * compatible with structured `data` types that don't carry an index
 * signature.
 */
export interface SanitizableNode {
  data?: object;
}

function dataKeys(data: object | undefined): string[] {
  if (!data) return [];
  return Object.keys(data as Record<string, unknown>);
}

function dataEntries(data: object | undefined): Array<[string, unknown]> {
  if (!data) return [];
  return Object.entries(data as Record<string, unknown>);
}

/**
 * Number of nodes whose `data` carries at least one user-input key
 * with a NON-EMPTY value (LDF-3 §4 — empty keys don't trigger the
 * dialog).
 */
export function countNodesWithUserInput(nodes: SanitizableNode[]): number {
  let count = 0;
  for (const n of nodes) {
    for (const [k, v] of dataEntries(n.data)) {
      if (isUserInputKey(k) && !isEmptyValue(v)) {
        count += 1;
        break;
      }
    }
  }
  return count;
}

/** Convenience: any node has user input? Faster than countNodesWithUserInput. */
export function nodesHaveUserInput(nodes: SanitizableNode[]): boolean {
  for (const n of nodes) {
    for (const [k, v] of dataEntries(n.data)) {
      if (isUserInputKey(k) && !isEmptyValue(v)) return true;
    }
  }
  return false;
}

/**
 * Returns a deep-cloned copy of the nodes with every user-input key
 * removed from each node's `data`. The original input is not mutated.
 * Non-data fields (id, position, type, label, etc.) ride through
 * untouched.
 */
export function stripUserInputFromNodes<T extends SanitizableNode>(nodes: T[]): T[] {
  return nodes.map((n) => {
    if (!n.data) return n;
    const cleaned: Record<string, unknown> = {};
    for (const [k, v] of dataEntries(n.data)) {
      if (!isUserInputKey(k)) cleaned[k] = v;
    }
    return { ...n, data: cleaned as T["data"] };
  });
}

/**
 * Per-workflow suppression flag. When set, the save-sanitization dialog
 * doesn't prompt for that specific workflow until the user-input
 * snapshot delta forces a re-prompt (see `userInputSnapshot` below).
 */
const SUPPRESS_KEY_PREFIX = "mod.workflowSave.suppressSanitization.";

export function getSanitizationSuppressed(workflowId: number | string): { snapshot: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(`${SUPPRESS_KEY_PREFIX}${workflowId}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.snapshot === "string") return parsed;
    return null;
  } catch {
    return null;
  }
}

export function setSanitizationSuppressed(workflowId: number | string, snapshot: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      `${SUPPRESS_KEY_PREFIX}${workflowId}`,
      JSON.stringify({ snapshot, ts: Date.now() }),
    );
  } catch { /* quota — non-fatal */ }
}

/**
 * Compact, stable string representation of which (node, key) pairs are
 * carrying user input. Used as the "snapshot" against which a
 * subsequent save's delta is compared so that a fresh user-input key
 * (e.g. user adds a new param to a previously-clean node) re-triggers
 * the prompt even if the user previously picked "Save as-is".
 */
export function userInputSnapshot(nodes: Array<SanitizableNode & { id?: string | number }>): string {
  const pairs: string[] = [];
  for (const n of nodes) {
    if (!n.data) continue;
    const id = n.id != null ? String(n.id) : "?";
    // Only include keys with NON-EMPTY values so the dismiss-flag
    // delta tracks user-meaningful changes, not metadata churn.
    const keys = dataEntries(n.data)
      .filter(([k, v]) => isUserInputKey(k) && !isEmptyValue(v))
      .map(([k]) => k)
      .sort();
    if (keys.length > 0) {
      pairs.push(`${id}:${keys.join(",")}`);
    }
  }
  return pairs.sort().join("|");
}
