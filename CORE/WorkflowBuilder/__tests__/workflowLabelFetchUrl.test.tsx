/**
 * 2400UI139 — the workflow `data_classification` label fetch must interpolate a
 * CALLED api base, never the bare imported `apiUrl` function.
 *
 * Reads components/workflow-builder.tsx from disk and asserts:
 *   1. The label fetch uses the called form `${apiUrl()}/road/`.
 *   2. No `${apiUrl}/` interpolation appears before the first local
 *      `const apiUrl = getApiUrl()` shadow — i.e. none resolving to the
 *      uncalled function imported from lib/runtime-config.
 */
import * as fs from "fs";
import * as path from "path";

const SOURCE_FILE = path.resolve(
  __dirname,
  "../components/workflow-builder.tsx"
);

describe("2400UI139 — workflow label fetch URL", () => {
  let source: string;

  beforeAll(() => {
    source = fs.readFileSync(SOURCE_FILE, "utf-8");
  });

  it("should interpolate the called apiUrl() into the workflow label fetch", () => {
    expect(source).toMatch(/\$\{apiUrl\(\)\}\/road\/\$\{api\.currentWorkflow\?\.id\}/);
  });

  it("should not interpolate the uncalled apiUrl function into the label path", () => {
    expect(/\$\{apiUrl\}\/road\/\$\{api\.currentWorkflow\?\.id\}/.test(source)).toBe(false);
  });

  it("should not interpolate bare apiUrl before any local shadow exists", () => {
    const lines = source.split("\n");
    const shadowIdx = lines.findIndex((l) => /const apiUrl = getApiUrl\(\)/.test(l));
    expect(shadowIdx).toBeGreaterThan(-1);
    const offenders = lines
      .map((l, i) => ({ l, i }))
      .filter(({ l }) => /\$\{apiUrl\}\//.test(l))
      .map(({ i }) => i)
      .filter((i) => i < shadowIdx);
    expect(offenders).toEqual([]);
  });
});
