/**
 * Variant skip helpers — pure logic the New Node wizard delegates
 * to so the user doesn't have to manually deselect known-broken
 * variants on Review or Build.
 */

import {
  computeReviewInitialSkipped,
  computeBuildAutoSkipFromArtifacts,
  TODO_MODEL_ID_MARKER,
} from "../lib/variant-skip";

describe("computeReviewInitialSkipped", () => {
  test("`suggested` is ignored — no hard signal means nothing is pre-skipped", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm", "intel", "cpu"],
      ["cuda", "cpu"],
      [],
      {},
    );
    expect(out.size).toBe(0);
  });

  test("cpuOnly=true no longer pre-skips GPU variants (empirical verification replaces the heuristic)", () => {
    // Previously: cpuOnly=true pre-skipped cuda/rocm/intel/npu.
    // Now: the build pipeline empirically determines which variants
    // actually work, so we leave them all pre-checked. A pure-CPU
    // script will have its GPU variants marked incompatible at build
    // time via the GPU-presence verifier — no prediction needed here.
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm", "intel", "cpu"],
      [],
      [],
      {},
      true,
    );
    expect(out.size).toBe(0);
  });

  test("cpuOnly=true with hard signals still skips those hard-signal variants", () => {
    // cpuOnly is a no-op now, but circuit-broken + source-incompatible
    // are HARD signals from the server and still pre-skip.
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm", "cpu"],
      [],
      ["rocm"],
      { cuda: "deep CUDA — no hipify path" },
      true,
    );
    expect(Array.from(out).sort()).toEqual(["cuda", "rocm"]);
    expect(out.has("cpu")).toBe(false);
  });

  test("cpuOnly defaults to false — GPU variants stay checked", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm", "cpu"],
      [],
      [],
      {},
    );
    expect(out.size).toBe(0);
  });

  test("circuit-broken variants are pre-skipped even if suggested", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm"],
      ["cuda", "rocm"],
      ["rocm"],
      {},
    );
    expect(Array.from(out)).toEqual(["rocm"]);
  });

  test("source-incompatible variants are pre-skipped even if suggested", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm", "intel"],
      ["cuda", "rocm", "intel"],
      [],
      { rocm: "deep CUDA — no hipify path", intel: "deep CUDA — no dpct path" },
    );
    expect(Array.from(out).sort()).toEqual(["intel", "rocm"]);
  });

  test("empty inputs return empty skip set", () => {
    expect(computeReviewInitialSkipped([], [], [], {}).size).toBe(0);
  });

  test("the same variant appearing in multiple skip reasons isn't double-counted", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm"],
      ["cuda"],
      ["rocm"],
      { rocm: "incompatible" },
    );
    expect(Array.from(out)).toEqual(["rocm"]);
    expect(out.size).toBe(1);
  });

  test("falsy incompatible_reasons[v] does not pre-skip", () => {
    const out = computeReviewInitialSkipped(
      ["cuda", "rocm"],
      ["cuda", "rocm"],
      [],
      { rocm: "" },
    );
    expect(out.size).toBe(0);
  });
});

describe("computeBuildAutoSkipFromArtifacts", () => {
  test("variant flagged adapter-incompatible is auto-skipped", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      { npu: { incompatible: true }, cuda: { incompatible: false } },
      undefined,
    );
    expect(Array.from(out)).toEqual(["npu"]);
  });

  test("variant whose dockerfile contains TODO_MODEL_ID is auto-skipped", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      undefined,
      {
        cuda: "FROM python:3.11\nRUN pip install torch\n",
        npu: `FROM python:3.11\nRUN optimum-cli export openvino --model ${TODO_MODEL_ID_MARKER}\n`,
      },
    );
    expect(Array.from(out)).toEqual(["npu"]);
  });

  test("a variant skipped via both signals appears once", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      { npu: { incompatible: true } },
      { npu: `FROM python:3.11\nRUN export ${TODO_MODEL_ID_MARKER}\n` },
    );
    expect(Array.from(out)).toEqual(["npu"]);
    expect(out.size).toBe(1);
  });

  test("clean inputs produce empty skip set", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      { cuda: { incompatible: false }, rocm: { incompatible: false } },
      { cuda: "FROM python:3.11", rocm: "FROM rocm/dev:latest" },
    );
    expect(out.size).toBe(0);
  });

  test("malformed adaptation entries are ignored, not crashed on", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      // null entry, string entry, number entry — all bogus
      { cuda: null as unknown as { incompatible?: boolean }, rocm: "bad" as unknown as { incompatible?: boolean }, intel: 42 as unknown as { incompatible?: boolean } },
      undefined,
    );
    expect(out.size).toBe(0);
  });

  test("non-string dockerfile entry is ignored", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      undefined,
      { cuda: 123 as unknown as string },
    );
    expect(out.size).toBe(0);
  });

  test("undefined / null inputs produce empty skip set", () => {
    expect(computeBuildAutoSkipFromArtifacts(undefined, undefined).size).toBe(0);
    expect(computeBuildAutoSkipFromArtifacts(null, null).size).toBe(0);
  });

  test("TODO_MODEL_ID anywhere in the dockerfile triggers skip (case-sensitive)", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      undefined,
      {
        a: `# leading comment with ${TODO_MODEL_ID_MARKER}`,
        b: `RUN echo something`,
        c: `${TODO_MODEL_ID_MARKER}`,
      },
    );
    expect(Array.from(out).sort()).toEqual(["a", "c"]);
  });

  test("lowercase 'todo_model_id' does not match (we mirror the server-side check)", () => {
    const out = computeBuildAutoSkipFromArtifacts(
      undefined,
      { a: "RUN echo todo_model_id" },
    );
    expect(out.size).toBe(0);
  });
});
