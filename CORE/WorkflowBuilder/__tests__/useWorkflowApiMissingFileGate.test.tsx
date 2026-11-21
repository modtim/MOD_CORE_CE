/**
 * rc20 row58 — Execute's missing-file gate must key on the NODE KIND, not on
 * input_schema.input_type alone.
 *
 * useWorkflowApi.ts:1793 gated the "Input File Required" dialog behind
 * `isNoFileInput = inputType === 'NONE'`, but EVERY seeded system input node
 * carries input_schema.input_type "NONE" (CORE/Metadata/system/{Image,Text,
 * Script,Resource,Audio,Video,Mesh,ComicImage}_INPUT_Node.py), so the dialog
 * was unreachable for the whole palette — wb-ce-batch20.spec.ts:135 stayed RED.
 * The gate's own comment says the carve is for TEXT/SCRIPT inputs only.
 *
 * The fix narrows the carve to node_kind text_input / script_input (the stable
 * key the palette/graph already carry: lib/graph-load-fallback.ts:102,
 * nodes/node-stack-overlay.tsx:25), keeping the text_content exemption.
 *
 * Harness reused from __tests__/executeWorkflowCeWarnings.test.tsx: the REAL
 * hook runs under renderHook; only global.fetch is mocked and routed per URL.
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";

const toastMock = jest.fn();
jest.mock("@/components/ui/use-toast", () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const RUN_URL = /\/road\/workflow-versions\/\d+\/run$/;
const VERSIONS_URL = /\/road\/\d+\/versions$/;
const CREATE_URL = /\/road\/$/;
const FEATURES_URL = /\/platform\/features$/;

let runCalls: number;

async function advance(ms: number): Promise<void> {
  await act(async () => { await jest.advanceTimersByTimeAsync(ms); });
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await advance(0);
}

function inputNode(kind: string, opts: { files?: boolean; text?: boolean; namedRef?: string } = {}): any {
  return {
    id: "n1",
    type: "input",
    position: { x: 0, y: 0 },
    data: {
      label: kind.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()),
      node_kind: kind,
      userPrompt: "hello",
      input_schema: { input_type: "NONE", input_variables: "NONE" },
      ...(opts.namedRef === undefined
        ? {}
        : {
            configuration_schema: {
              extra_variables: {
                ARTIFACT_NAME: { type: "named_ref", ref_type: "rag", default: "", description: "pick one" },
              },
            },
          }),
      selectedFiles: opts.files ? [{ id: 7, name: "a.png" }] : [],
      selectedMetadata: [],
      extraVariables: {
        ...(opts.text ? { text_content: "hello" } : {}),
        ...(opts.namedRef === undefined ? {} : { ARTIFACT_NAME: opts.namedRef }),
      },
    },
  };
}

// Save an already-dirty graph through the PII "Save as-is" path, then leave a
// re-render between the save and the Execute click (verbatim from
// executeWorkflowCeWarnings.test.tsx:mountAndSave — it is what sets
// currentWorkflow + currentVersionNumber so Execute reaches the input gate).
async function mountAndSave(nodes: any[]): Promise<any> {
  const hook = renderHook(() => useWorkflowApi(nodes, [], () => {}, () => {}, {}));
  await settle();
  act(() => { hook.result.current.markDirty(); });
  await settle();
  act(() => { hook.result.current.handleSaveInitiation("saveAs"); });
  await settle();
  act(() => { hook.result.current.handleSaveConfirm(); });
  await settle();
  expect(hook.result.current.isSanitizationDialogOpen).toBe(true);
  act(() => { hook.result.current.confirmSaveAsIs(); });
  await settle();
  expect(hook.result.current.isSaveDialogOpen).toBe(false);
  hook.rerender();
  await settle();
  return hook;
}

describe("useWorkflowApi Execute missing-file gate (rc20 row58)", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    runCalls = 0;
    _resetPlatformFeaturesCache();
    global.fetch = jest.fn(async (url: string) => {
      const u = String(url);
      if (FEATURES_URL.test(u)) return makeResponse(200, { entitlements: { edition: "ce", capabilities: [] } });
      if (RUN_URL.test(u)) { runCalls += 1; return makeResponse(200, { workflow_run_id: 99, status: "running" }); }
      if (VERSIONS_URL.test(u)) return makeResponse(200, { id: 21, version_number: 1, workflow_id: 11 });
      if (CREATE_URL.test(u)) return makeResponse(200, { id: 11, name: "W Copy", archived_at: null });
      return makeResponse(200, []);
    }) as unknown as typeof global.fetch;
    window.history.replaceState({}, "", "/?token=tok");
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  // U1 — the RED case from wb-ce-batch20.spec.ts:135: a file-bearing input
  // (Resource Input, input_type "NONE") with NO file must open the
  // "Input File Required" dialog and POST no run.
  it("Resource Input with no file: dialog opens, no run", async () => {
    const hook = await mountAndSave([inputNode("resource_input")]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(true);
    expect(runCalls).toBe(0);
  });

  // U2 — same for Image Input (the palette's other file-bearing input).
  it("Image Input with no file: dialog opens, no run", async () => {
    const hook = await mountAndSave([inputNode("image_input")]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(true);
    expect(runCalls).toBe(0);
  });

  // U3 — the exemption the old carve gave everything must survive for the
  // text/script kinds ONLY: Text Input with no file and no text_content runs.
  it("Text Input with no file: no dialog, run fires", async () => {
    const hook = await mountAndSave([inputNode("text_input")]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U4 — Script Input likewise exempt (the :1791 comment names it).
  it("Script Input with no file: no dialog, run fires", async () => {
    const hook = await mountAndSave([inputNode("script_input")]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U5 — a file-bearing input WITH a file still runs (no regression on the
  // golden/b10/b13/b21/b22/b26 path that uploads before Execute).
  it("Image Input with a file: no dialog, run fires", async () => {
    const hook = await mountAndSave([inputNode("image_input", { files: true })]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U6 — the second gate (:1818 "No files selected in input nodes."): a
  // file-bearing input with no file must not reach that toast either — the
  // first gate already returned.
  it("Resource Input with no file: no 'No files selected' toast", async () => {
    toastMock.calls = [];
    const hook = await mountAndSave([inputNode("resource_input")]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    const noFiles = toastMock.calls.filter((c: any) =>
      JSON.stringify(c).includes("No files selected in input nodes."));
    expect(noFiles.length).toBe(0);
  });

  // U7 — rc20 row58-r2: RAG Input selects a saved KB by NAME (a named_ref
  // extra_variable, RAGInput_INPUT_Node.py:19), not a file. With a non-empty
  // named_ref value it is no-file-OK: no dialog, run fires once.
  it("RAG Input with named_ref set and no file: no dialog, run fires", async () => {
    const hook = await mountAndSave([inputNode("rag_input", { namedRef: "brand-guide" })]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U8 — same for LoRA Input (LoRAInput_INPUT_Node.py:19 LORA_NAME named_ref).
  it("LoRA Input with named_ref set and no file: no dialog, run fires", async () => {
    const hook = await mountAndSave([inputNode("lora_input", { namedRef: "hero-lora" })]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // U9 — an EMPTY named_ref must still open the dialog (0 runs): the carve only
  // applies when a selection actually exists.
  it("RAG Input with empty named_ref and no file: dialog opens, no run", async () => {
    const hook = await mountAndSave([inputNode("rag_input", { namedRef: "" })]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(true);
    expect(runCalls).toBe(0);
  });

  // U10 — same for LoRA Input with an empty named_ref.
  it("LoRA Input with empty named_ref and no file: dialog opens, no run", async () => {
    const hook = await mountAndSave([inputNode("lora_input", { namedRef: "" })]);
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(true);
    expect(runCalls).toBe(0);
  });
});
