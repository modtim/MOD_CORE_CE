/**
 * γ — real-data sample loop helpers used by new-node-wizard.
 *
 * The wizard accepts an optional sample file the user attaches on
 * the Source step. When present, the build-and-register-local
 * request adds three fields and forces defer_registration so the
 * imagebuilder runs the freshly-built image against the sample
 * before any NodeDefinition row is created.
 *
 * Pulled out of the wizard component so the data-shape decision is
 * testable in isolation (the wizard itself is too big to render in
 * a unit test).
 */

export const SAMPLE_INPUT_MAX_BYTES = 8 * 1024 * 1024;

export type SampleAttachment = {
  filename: string;
  base64: string;
};

/**
 * Returns the build-and-register-local POST body fields contributed
 * by an optional sample attachment. When `sample` is null, returns
 * an empty object so the caller can spread it unconditionally.
 *
 * This is the ONE source of truth for "did the user opt into the
 * deferred-registration flow?" — anywhere the wizard polls or
 * branches, it's keyed off whether a sample was attached at submit
 * time, which means the API gate (defer_registration=true) and the
 * UI gate (poll /build-task-status, render thumbs UI) cannot drift.
 */
export function deferredRegistrationFields(
  sample: SampleAttachment | null,
): Record<string, unknown> {
  if (!sample || !sample.filename || !sample.base64) {
    return {};
  }
  return {
    sample_input_filename: sample.filename,
    sample_input_body_b64: sample.base64,
    defer_registration: true,
  };
}

/**
 * Size-validate an attached sample. Returns null when valid, an
 * error string when the file is too large (the wizard surfaces this
 * as a destructive toast and refuses the file).
 */
export function validateSampleSize(file: File): string | null {
  if (file.size > SAMPLE_INPUT_MAX_BYTES) {
    return `Sample input must be ${SAMPLE_INPUT_MAX_BYTES / (1024 * 1024)} MB or smaller. For bigger inputs, register without a sample and test in the workflow.`;
  }
  return null;
}

/**
 * Strip the data-URL prefix from a FileReader.readAsDataURL result.
 * Browser readers return strings like
 * ``data:application/octet-stream;base64,XXX`` — the API only
 * wants the XXX. Returns the input unchanged when no comma is
 * found (defensive; lets the caller pass already-stripped strings
 * through unmolested in tests).
 */
export function stripDataUrlPrefix(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  if (comma < 0) return dataUrl;
  return dataUrl.slice(comma + 1);
}
