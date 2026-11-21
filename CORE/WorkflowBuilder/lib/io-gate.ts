/**
 * Wizard I/O-confirmation gate.
 *
 * The wizard's Configure step has two dropdowns (Input type / Output
 * type) that drive how the resulting Node wires into a workflow. If
 * the user submits a build without confirming both, the resulting
 * Node has vague I/O and can't be connected to anything — the
 * silent-broken-node failure mode.
 *
 * Two scenarios produce a need-to-confirm gate:
 *
 *   1. The analyzer ran (Script / Repo / Dockerfile mode) and rated
 *      the schema as low confidence. We trust the analyzer's hint
 *      and require the user to touch at least one field.
 *
 *   2. The analyzer didn't run (Image mode — there's no script to
 *      analyze). The dropdowns default to "image"/"image" silently.
 *      The user must explicitly confirm by touching at least one
 *      field, otherwise the defaults stick without intent.
 *
 * `aiFilledFields` is the source of truth for "AI filled it, user
 * hasn't touched it." When the user changes a dropdown, the setter
 * removes the entry from the set. So "field is still in
 * aiFilledFields" === "user has NOT confirmed this field."
 *
 * For Image mode the wizard pre-marks both inputType and outputType
 * as aiFilledFields the first time the analysis is "skipped," so
 * the same gate logic catches both cases.
 */

export type SourceType =
  | "script"
  | "repo"
  | "image"
  | "dockerfile"
  | "describe";

export type Confidence = "high" | "medium" | "low";

export interface IOGateInput {
  sourceType: SourceType;
  schemaConfidence: Confidence;
  aiFilledFields: ReadonlySet<string>;
}

/**
 * Returns true iff the wizard needs the user to confirm I/O types
 * before proceeding past Configure. When true: the banner shows and
 * the Next button is disabled.
 *
 * Gate-release semantics differ by source type:
 *
 *   Image mode — the analyzer never ran. Defaults are image/image
 *   silently. We just need the user to ENGAGE with the form. Touching
 *   either dropdown OR clicking "Confirm as shown" releases the gate.
 *   Gate fires only while BOTH fields are still ai-filled (no touch
 *   yet on either).
 *
 *   Analyzer-driven modes — the analyzer made guesses for BOTH fields
 *   and flagged low confidence. Both need explicit confirmation since
 *   either guess could be wrong. Gate fires while EITHER field is
 *   still ai-filled.
 */
export function needsIOConfirmation(input: IOGateInput): boolean {
  const { sourceType, schemaConfidence, aiFilledFields } = input;
  if (sourceType === "image") {
    // Either dropdown touched = engagement = release.
    return (
      aiFilledFields.has("inputType") &&
      aiFilledFields.has("outputType")
    );
  }
  // Analyzer-driven flows: both must be touched.
  const stillAiFilled =
    aiFilledFields.has("inputType") || aiFilledFields.has("outputType");
  if (!stillAiFilled) return false;
  return schemaConfidence === "low";
}

/**
 * The banner copy varies by source type. Image mode says "we didn't
 * analyze this image"; the analyzer-driven cases say "the AI wasn't
 * sure." Same call-to-action either way.
 */
export function ioBannerBody(sourceType: SourceType): string {
  if (sourceType === "image") {
    return (
      "We don't analyze the contents of a base image — pick both Input " +
      "type and Output type below before building. A node with vague I/O " +
      "can't be wired into a workflow."
    );
  }
  return (
    "The AI wasn't sure what this node consumes or produces. Pick " +
    "(or confirm) both Input type and Output type below before " +
    "building — a node with vague I/O can't be wired into a workflow."
  );
}
