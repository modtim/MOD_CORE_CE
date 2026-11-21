/**
 * Bug.B61 — ensure no plain-slash-slash comment lines leak into JSX children as visible text.
 *
 * Reads workflow-builder.tsx source from disk and asserts:
 *   1. The "Canvas height: 100dvh" text is inside a JSX comment block, not a bare comment.
 *   2. No line in the file starts with "// Canvas height" (the old broken pattern).
 */
import * as fs from "fs";
import * as path from "path";

const SOURCE_FILE = path.resolve(
  __dirname,
  "../components/workflow-builder.tsx"
);

describe("Bug.B61 — no JSX-text-comment", () => {
  let source: string;

  beforeAll(() => {
    source = fs.readFileSync(SOURCE_FILE, "utf-8");
  });

  it("should not contain bare '// Canvas height' comment lines", () => {
    const lines = source.split("\n");
    const offenders = lines.filter((l) => /^\s*\/\/ Canvas height/.test(l));
    expect(offenders).toEqual([]);
  });

  it("should contain the canvas-height comment inside a JSX /* block", () => {
    const { findJsxCommentRange, lineContainingText, linesBeforeCloseBrace } =
      buildMatchers(source);

    const targetLine = lineContainingText("Canvas height: 100dvh");
    expect(targetLine).toBeGreaterThan(-1);

    const range = findJsxCommentRange(targetLine);
    expect(range).toBeDefined();
    expect(range!.openLine).toBeLessThanOrEqual(targetLine);

    const closeLine = range!.closeLine;
    expect(closeLine).toBeGreaterThan(range!.openLine);

    // Verify the target line itself contains an opening {/* before the text.
    const targetLineContent = source.split("\n")[targetLine];
    expect(targetLineContent).toMatch(/\{\s*\/\*/);
  });
});

// --- helpers ---

function buildMatchers(source: string) {
  const lines = source.split("\n");

  function lineContainingText(needle: string): number {
    return lines.findIndex((l) => l.includes(needle));
  }

  function findJsxCommentRange(lineNum: number): { openLine: number; closeLine: number } | undefined {
    // The target line may be inside a multi-line JSX comment.  Find the nearest
    // {/* on or before the line, and the nearest */} on or after it.
    // Walk backwards for the opening {/* (skip closed single-line blocks).
    let openLine = -1;
    for (let i = lineNum; i >= 0; i--) {
      if (/^\s*\{\s*\/\*\s.*\*\/\s*$/.test(lines[i])) {
        continue;
      }
      if (lines[i].includes("*/")) {
        break;
      }
      const m = lines[i].match(/\{\s*\/\*/);
      if (m) {
        openLine = i;
        break;
      }
    }
    if (openLine === -1) return undefined;

    // Walk forwards for the closing */}.
    let closeSeen = -1;
    for (let i = lineNum; i < lines.length; i++) {
      if (lines[i].includes("*/")) {
        closeSeen = i;
        break;
      }
    }

    return { openLine, closeLine: closeSeen };
  }

  function linesBeforeCloseBrace(lineNum: number): string[] {
    const result: string[] = [];
    for (let i = 0; i <= lineNum; i++) {
      // Skip complete single-line JSX comments (they cannot be openers
      // for an unclosed block).
      if (/^\s*\{\s*\/\*\s.*\*\/\s*$/.test(lines[i])) continue;
      if (lines[i].includes("*/")) break;
      const m = lines[i].match(/\{\s*\/\*/);
      if (m) result.push(m[0].trim());
    }
    return result;
  }

  return { findJsxCommentRange, lineContainingText, linesBeforeCloseBrace };
}
