import * as fs from "fs";
import * as path from "path";

// RC20 e2e red (wb-node-catalogue-audit.spec.ts:373) — after a cancelled run the
// canvas live region must carry BOTH the node transition sentence
// ("Return Assets Export: failure") and the all-terminal sentence
// ("Run finished with status failed"). The builder's node-diff effect called
// announceCanvas TWICE in the same tick (node sentence, then terminal sentence);
// two setCanvasLiveMessage calls batch into ONE DOM mutation, so the region only
// ever held the LAST sentence and the node sentence was swallowed — the e2e
// MutationObserver saw seq 4 then seq 6, never seq 5. The fix joins every
// sentence an effect pass produces into ONE announceCanvas call.

describe("node-diff effect announces at most one region update per pass", () => {
  const lines = fs.readFileSync(
    path.resolve(__dirname, "../components/workflow-builder.tsx"),
    "utf-8",
  ).split("\n");
  const start = lines.findIndex((l) => l.includes("const lastNodeStatuses = useRef"));
  const end = lines.findIndex(
    (l, i) => i > start && l.includes("}, [nodes, api.selectedRunId, announceCanvas]);")
  );
  const effect = lines.slice(start, end + 1).join("\n");

  it("the node-diff effect exists and is keyed on selectedRunId", () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    expect(effect).toContain("const runId = api.selectedRunId;");
  });

  it("the effect emits ONE combined region update, not two same-tick pushes", () => {
    const calls = (effect.match(/announceCanvas\(/g) ?? []).length;
    expect(calls).toBe(1);
    expect(effect).toContain("const parts: string[] = [];");
    expect(effect).toContain("announceCanvas(parts.join(\". \"));");
  });

  it("the combined sentence keeps both the node word and the terminal word", () => {
    // Sentence builders unchanged: the node sentence and terminal sentence are
    // still produced by diffStatuses/describeTerminalRunStatus and joined.
    expect(effect).toContain("diffStatuses(lastNodeStatuses.current ?? undefined, snaps)");
    expect(effect).toContain("describeTerminalRunStatus(");
    expect(effect).toContain("parts.push(changes.join(\". \"));");
    expect(effect).toContain("parts.push(terminalSentence);");
  });
});
