/**
 * TypeScript side of the DAG-template parity test.
 *
 * Reads the same fixtures the Python test consumes
 * (CORE/API/tests/fixtures/metadata_template_samples.json) and renders each
 * input through the TS implementation. Both tests MUST produce the
 * exact `expected.content` string — byte-for-byte — so the wizard's
 * live preview matches what the server writes on commit.
 *
 * If this test fails, either:
 *   1. The TS renderer diverged from Python → fix `lib/metadataTemplate.ts`.
 *   2. A legitimate template change → update Python + TS + fixtures in
 *      one commit.
 */
import { render, coerceAndValidate } from "../lib/metadataTemplate";
// Fixture is duplicated from CORE/API/tests/fixtures/ into this tree
// because jest's module resolver can't reach above its rootDir. The
// Python test at test_dag_template.py::test_fixture_copies_identical
// asserts byte-parity between the two copies so they can't drift.
import fixtures from "./fixtures/metadata_template_samples.json";

interface Case {
  name: string;
  input: Record<string, unknown>;
  expected: {
    filename: string;
    node_kind: string;
    category: string;
    content: string;
  };
}

describe("metadataTemplate parity with Python", () => {
  for (const c of (fixtures as { cases: Case[] }).cases) {
    test(c.name, () => {
      const md = coerceAndValidate(c.input as any);
      const out = render(md);
      expect(out.filename).toBe(c.expected.filename);
      expect(out.node_kind).toBe(c.expected.node_kind);
      expect(out.category).toBe(c.expected.category);
      expect(out.content).toBe(c.expected.content);
    });
  }
});

describe("coerceAndValidate", () => {
  test("strips unknown keys", () => {
    const md = coerceAndValidate({
      node_name: "X",
      executable_path: "ghcr.io/modtim/x:latest",
      random_junk: "ignored",
    } as any);
    expect((md as any).random_junk).toBeUndefined();
  });

  test("unknown types pass through lowercased; unknown keys are dropped", () => {
    const md = coerceAndValidate({
      node_name: "X",
      executable_path: "ghcr.io/modtim/x:latest",
      input_type: "NOT_AN_ENUM",
      process_type: "TPU",
    } as any);
    // input_type stopped being an enum deliberately. This test asserted
    // `"image"` long after that changed, so it was pinning a contract the code
    // no longer had.
    expect(md.input_type).toBe("not_an_enum");
    expect((md as any).process_type).toBeUndefined();
  });

  test("domain type labels survive verbatim", () => {
    // The reason input_type is open rather than an enum: an enum drops any
    // label it does not already know, and the formats that matter to reality
    // capture and AEC are exactly the ones a general enum would not list.
    for (const label of ["ifc", "step", "usd", "parquet", "dicom", "E57"]) {
      const md = coerceAndValidate({
        node_name: "X", executable_path: "ghcr.io/modtim/x:latest", input_type: label,
      } as any);
      expect(md.input_type).toBe(label.toLowerCase());
    }
  });

  test("a missing or non-string type falls back to other", () => {
    const base = { node_name: "X", executable_path: "ghcr.io/modtim/x:latest" };
    expect(coerceAndValidate({ ...base } as any).input_type).toBe("other");
    expect(coerceAndValidate({ ...base, input_type: 42 } as any).input_type).toBe("other");
    expect(coerceAndValidate({ ...base, input_type: "   " } as any).input_type).toBe("other");
  });

  test("clamps numeric ranges", () => {
    const md = coerceAndValidate({
      node_name: "X",
      executable_path: "ghcr.io/modtim/x:latest",
      cost_in_credits: -50,
      container_timeout: 10_000_000,
    });
    expect(md.cost_in_credits).toBe(0);
    expect(md.container_timeout).toBe(86_400);
  });

  test("derives exe from name when missing", () => {
    const md = coerceAndValidate({ node_name: "My Cool Node" } as any);
    expect(md.executable_path).toBe("mycoolnode:latest");
  });
});

describe("render determinism", () => {
  test("same input renders byte-identical output twice", () => {
    const md = coerceAndValidate({
      node_name: "Detn",
      executable_path: "ghcr.io/modtim/detn:latest",
    });
    expect(render(md).content).toBe(render(md).content);
  });
});
