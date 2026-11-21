/**
 * Wizard I/O-confirmation gate (lib/io-gate.ts).
 *
 * Pins the gate logic the Configure step + Next button both use.
 * Two scenarios produce the gate:
 *   1. Analyzer ran but rated I/O low confidence (Script / Repo /
 *      Dockerfile flows).
 *   2. Image mode — no analyzer at all; the dropdowns default to
 *      image/image silently. The wizard pre-marks both fields as
 *      aiFilledFields the first time the user lands on Configure,
 *      so the same gate catches both cases.
 */
import { ioBannerBody, needsIOConfirmation } from "../lib/io-gate";

describe("needsIOConfirmation", () => {
  describe("Image mode", () => {
    it("gates when both fields are still ai-filled", () => {
      expect(needsIOConfirmation({
        sourceType: "image",
        schemaConfidence: "high",   // analyzer didn't run; default
        aiFilledFields: new Set(["inputType", "outputType"]),
      })).toBe(true);
    });

    it("releases when EITHER field has been touched", () => {
      // Touching Input dropdown removes "inputType" from
      // aiFilledFields. Gate releases — user has engaged with
      // the form.
      expect(needsIOConfirmation({
        sourceType: "image",
        schemaConfidence: "high",
        aiFilledFields: new Set(["outputType"]),
      })).toBe(false);
      // Symmetric: touching Output is also enough.
      expect(needsIOConfirmation({
        sourceType: "image",
        schemaConfidence: "high",
        aiFilledFields: new Set(["inputType"]),
      })).toBe(false);
    });

    it("releases when Confirm button cleared both", () => {
      // Wizard's "Confirm as shown" button removes both. Empty
      // set = explicit rubber-stamp.
      expect(needsIOConfirmation({
        sourceType: "image",
        schemaConfidence: "high",
        aiFilledFields: new Set(),
      })).toBe(false);
    });
  });

  describe("Analyzer-driven modes (script / repo / dockerfile)", () => {
    it("gates only when schemaConfidence is low AND a field is still ai-filled", () => {
      expect(needsIOConfirmation({
        sourceType: "script",
        schemaConfidence: "low",
        aiFilledFields: new Set(["inputType"]),
      })).toBe(true);
    });

    it("releases when schemaConfidence is medium or high", () => {
      expect(needsIOConfirmation({
        sourceType: "script",
        schemaConfidence: "medium",
        aiFilledFields: new Set(["inputType", "outputType"]),
      })).toBe(false);
      expect(needsIOConfirmation({
        sourceType: "script",
        schemaConfidence: "high",
        aiFilledFields: new Set(["inputType"]),
      })).toBe(false);
    });

    it("releases when low confidence but user has touched both fields", () => {
      expect(needsIOConfirmation({
        sourceType: "repo",
        schemaConfidence: "low",
        aiFilledFields: new Set(),
      })).toBe(false);
    });

    it("releases when low confidence and aiFilledFields lacks I/O entries", () => {
      // E.g. the analyzer didn't put inputType/outputType in
      // aiFilledFields (it filled other fields like description).
      expect(needsIOConfirmation({
        sourceType: "dockerfile",
        schemaConfidence: "low",
        aiFilledFields: new Set(["description"]),
      })).toBe(false);
    });
  });

  describe("source-type independence of the AI-filled signal", () => {
    it("never gates when neither inputType nor outputType is ai-filled", () => {
      for (const sourceType of [
        "script", "repo", "image", "dockerfile", "describe",
      ] as const) {
        for (const schemaConfidence of ["high", "medium", "low"] as const) {
          expect(needsIOConfirmation({
            sourceType,
            schemaConfidence,
            aiFilledFields: new Set(["description", "nodeName"]),
          })).toBe(false);
        }
      }
    });
  });
});

describe("ioBannerBody", () => {
  it("returns the image-mode copy for image source", () => {
    const body = ioBannerBody("image");
    expect(body).toMatch(/don't analyze/i);
    expect(body).toMatch(/pick both Input type and Output type/i);
  });

  it("returns the AI-wasn't-sure copy for analyzer-driven sources", () => {
    for (const sourceType of [
      "script", "repo", "dockerfile", "describe",
    ] as const) {
      const body = ioBannerBody(sourceType);
      expect(body).toMatch(/AI wasn't sure/i);
      expect(body).toMatch(/Pick \(or confirm\)/);
    }
  });
});
