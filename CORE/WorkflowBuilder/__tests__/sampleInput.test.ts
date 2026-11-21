/**
 * γ — real-data sample loop helpers.
 *
 * Verifies the data-shape decision the wizard keys off: when the
 * user attached a sample file, the build-and-register-local POST
 * body MUST carry the three sample fields + ``defer_registration:
 * true``. When the user didn't attach one, the body is unchanged
 * (no defer flag, no sample fields).
 */

import {
  deferredRegistrationFields,
  validateSampleSize,
  stripDataUrlPrefix,
  SAMPLE_INPUT_MAX_BYTES,
} from "@/lib/sample-input";

describe("deferredRegistrationFields", () => {
  it("returns the three sample fields when a sample is attached", () => {
    const got = deferredRegistrationFields({
      filename: "input.png",
      base64: "iVBORw0KGgo=",
    });
    expect(got).toEqual({
      sample_input_filename: "input.png",
      sample_input_body_b64: "iVBORw0KGgo=",
      defer_registration: true,
    });
  });

  it("returns an empty object when no sample is attached", () => {
    expect(deferredRegistrationFields(null)).toEqual({});
  });

  it("treats an empty filename as no attachment (defensive)", () => {
    expect(
      deferredRegistrationFields({ filename: "", base64: "abc" }),
    ).toEqual({});
  });

  it("treats empty base64 as no attachment (defensive)", () => {
    expect(
      deferredRegistrationFields({ filename: "x.bin", base64: "" }),
    ).toEqual({});
  });

  it("never sets defer_registration without a sample present — guards against silent bypass of the user-approval gate", () => {
    const got = deferredRegistrationFields(null);
    expect((got as any).defer_registration).toBeUndefined();
  });
});

describe("validateSampleSize", () => {
  it("accepts files up to the cap", () => {
    const file = { size: SAMPLE_INPUT_MAX_BYTES } as File;
    expect(validateSampleSize(file)).toBeNull();
  });

  it("rejects files over the cap", () => {
    const file = { size: SAMPLE_INPUT_MAX_BYTES + 1 } as File;
    expect(validateSampleSize(file)).not.toBeNull();
  });
});

describe("stripDataUrlPrefix", () => {
  it("strips a typical data URL prefix", () => {
    expect(
      stripDataUrlPrefix("data:application/octet-stream;base64,SGVsbG8="),
    ).toBe("SGVsbG8=");
  });

  it("returns the input unchanged when no prefix", () => {
    expect(stripDataUrlPrefix("SGVsbG8=")).toBe("SGVsbG8=");
  });
});
