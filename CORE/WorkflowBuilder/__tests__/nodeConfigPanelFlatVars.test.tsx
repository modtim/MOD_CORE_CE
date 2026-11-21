/**
 * BUG.WB-NODE-VARS-UI (owner 11-07) — "in Workflow Builder the Resize Image node
 * shows no input controls for its variables (WIDTH, HEIGHT, IMAGE_OP fixed,
 * MAX_SIDE, KEEP_ASPECT ...)".
 *
 * Root cause: shape mismatch. Nested-form node defs wrap variables in
 * configuration_schema.extra_variables (rendered); FLAT-form defs declare them
 * at the TOP level of configuration_schema — CORE/API/node_definitions_domains/
 * image_ops.py:492 IMAGE_RESIZE_SPEC -> :515: IMAGE_OP, WIDTH, HEIGHT, KEEP_ASPECT,
 * MAX_SIDE, every one type "string" with a string default and NO enum. The
 * panel (components/node-config-panel.tsx:874 gate) and the Execute named_ref
 * scan (hooks/useWorkflowApi.ts:1844) now mirror the server's own flat rules
 * (CORE/API/services/node_config_env_defaults.py:162 flat_env_defaults + :38
 * _IGNORED_KEYS). Cases (a)-(e) drive the REAL panel; (f)-(g) drive the REAL
 * hook (m4 leg: routes GET {API}/road/{id}/versions then POST
 * {API}/road/{currentWorkflow.id}/run — the run POST is the assertion that
 * fails when the hook mirror is dropped).
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import NodeConfigPanel from '@/components/node-config-panel';
import { act, renderHook } from '@testing-library/react';
import { useWorkflowApi } from '@/hooks/useWorkflowApi';
import { _resetPlatformFeaturesCache } from '@/lib/platform-features';

const toastMock = jest.fn();
jest.mock('@/components/ui/use-toast', () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

function renderPanel(data: any) {
  return render(
    <NodeConfigPanel
      node={
        {
          id: 'n1',
          type: 'process',
          position: { x: 0, y: 0 },
          data: { label: 'Resize Image', node_kind: 'resize', ...data },
        } as any
      }
      updateNodeData={() => {}}
      onClose={() => {}}
      accessToken="tok"
    />,
  );
}

// The panel renders non-variable controls too (Node Type + Nickname inputs), so
// variable controls are counted as a DELTA over a no-variable baseline render.
function baseline(): { textbox: number; spinbutton: number; switch: number; combobox: number } {
  const { unmount } = renderPanel({
    input_schema: { input_type: 'image' },
    output_schema: { output_type: 'image' },
    configuration_schema: {},
  });
  const counts = countsOf();
  unmount();
  return counts;
}

function countsOf() {
  return {
    textbox: screen.queryAllByRole('textbox').length,
    spinbutton: screen.queryAllByRole('spinbutton').length,
    switch: screen.queryAllByRole('switch').length,
    combobox: screen.queryAllByRole('combobox').length,
  };
}

// The Options block is the orange container that carries the "Options" heading
// (components/node-config-panel.tsx:1010 comment + :1011 hasOptions && ( +
// :1017 bg-orange-50 wrapper + :1018 Object.entries(extraVars!) map).
function optionsHeading() {
  return screen.queryByText('Options');
}

describe('NodeConfigPanel flat configuration_schema variables (BUG.WB-NODE-VARS-UI)', () => {
  // (a) nested form still renders exactly as before (regression guard, m3).
  it('nested extra_variables: Options block + one control per variable', async () => {
    const base = baseline();
    renderPanel({
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: {
        extra_variables: {
          WIDTH: { type: 'integer', default: 1024, description: 'target width' },
          KEEP_ASPECT: { type: 'boolean', default: true, description: 'keep aspect ratio' },
        },
      },
    });
    expect(optionsHeading()).not.toBeNull();
    const c = countsOf();
    expect(c.spinbutton - base.spinbutton).toBe(1);
    expect(c.switch - base.switch).toBe(1);
  });

  // (b) flat form, synthetic shapes: enum -> dropdown, boolean -> switch, integer -> number box.
  it('flat configuration_schema: dropdown + switch + number boxes', async () => {
    const base = baseline();
    renderPanel({
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: {
        IMAGE_OP: { type: 'string', default: 'resize', enum: ['resize', 'fit', 'fill'], description: 'operation' },
        KEEP_ASPECT: { type: 'boolean', default: true, description: 'keep aspect ratio' },
        WIDTH: { type: 'integer', default: 1024, description: 'target width' },
        HEIGHT: { type: 'integer', default: 1024, description: 'target height' },
        MAX_SIDE: { type: 'integer', default: 16384, description: 'longest edge' },
      },
    });
    expect(optionsHeading()).not.toBeNull();
    const c = countsOf();
    expect(c.combobox - base.combobox).toBe(1);
    expect(c.switch - base.switch).toBe(1);
    expect(c.spinbutton - base.spinbutton).toBe(3);
  });

  // (c) no variables at all -> no Options block, no controls (fail closed).
  it('schema with no variables: no Options block, no controls', async () => {
    const base = baseline();
    renderPanel({
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: { input_schema: { input_type: 'image' }, output_schema: { output_type: 'image' } },
    });
    expect(optionsHeading()).toBeNull();
    const c = countsOf();
    expect(c.textbox).toBe(base.textbox);
    expect(c.switch).toBe(base.switch);
    expect(c.combobox).toBe(base.combobox);
  });

  // (d) scaffolding-only schema -> NO Options block (kills the "metadata keys as
  // variables" mutant: with the exclusion set dropped, REQUIRED/PROPERTIES/ENUM/
  // DEFAULT/ITEMS/SCHEMA/DESCRIPTION/EXTRA_VARIABLES all become controls).
  it('JSON-Schema scaffolding keys are never variables', async () => {
    const base = baseline();
    renderPanel({
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: {
        type: 'object',
        title: 'Resize',
        required: ['WIDTH'],
        properties: { WIDTH: { type: 'integer' } },
        description: 'flat scaffolding only',
        TYPE: 'object',
        TITLE: 'Resize',
        REQUIRED: ['WIDTH'],
        PROPERTIES: { WIDTH: { type: 'integer' } },
        ENUM: ['resize'],
        DEFAULT: 'resize',
        ITEMS: {},
        SCHEMA: {},
        DESCRIPTION: 'flat scaffolding only',
        EXTRA_VARIABLES: { WIDTH: { type: 'integer', default: 1 } },
      },
    });
    expect(optionsHeading()).toBeNull();
    const c = countsOf();
    expect(c.textbox).toBe(base.textbox);
    expect(c.spinbutton).toBe(base.spinbutton);
    expect(c.switch).toBe(base.switch);
    expect(c.combobox).toBe(base.combobox);
  });

  // (e) LIVE Resize Image shape verbatim (image_ops.py:515-541): five flat keys,
  // every one type "string" with a string default and NO enum -> all five get a
  // text Input box, none of the scaffolding keys does.
  it('live Resize Image shape: five string vars get text boxes', async () => {
    const base = baseline();
    renderPanel({
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: {
        IMAGE_OP: { type: 'string', default: 'resize', description: 'Fixed for this node: image-ops operation to run.' },
        WIDTH: { type: 'string', default: '', description: 'Target width in pixels (1-16384). Blank = unset.' },
        HEIGHT: { type: 'string', default: '', description: 'Target height in pixels (1-16384). Blank = unset.' },
        KEEP_ASPECT: { type: 'string', default: 'true', description: 'Keep the original aspect ratio (default true).' },
        MAX_SIDE: { type: 'string', default: '16384', description: 'Longest allowed edge in pixels (1-16384).' },
      },
    });
    expect(optionsHeading()).not.toBeNull();
    const c = countsOf();
    expect(c.textbox - base.textbox).toBe(5);
    expect(c.combobox).toBe(base.combobox);
    expect(screen.getByText('IMAGE OP')).toBeInTheDocument();
    expect(screen.getByText('MAX SIDE')).toBeInTheDocument();
  });
});

// ---- m4 leg: the REAL hook's Execute gate must see flat-form named_ref vars.
// Harness copied from __tests__/useWorkflowApiMissingFileGate.test.tsx: the
// REAL hook under renderHook, only global.fetch mocked + routed per URL.
function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => '',
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

function flatNamedRefInput(namedRef: string): any[] {
  return [{
    id: 'n1',
    type: 'input',
    position: { x: 0, y: 0 },
    data: {
      label: 'RAG Input',
      node_kind: 'rag_input',
      userPrompt: 'hello',
      input_schema: { input_type: 'NONE', input_variables: 'NONE' },
      // FLAT form (no extra_variables wrapper) - same shape image_ops.py:515 uses.
      configuration_schema: {
        ARTIFACT_NAME: { type: 'named_ref', ref_type: 'rag', default: '', description: 'pick one' },
      },
      selectedFiles: [],
      selectedMetadata: [],
      extraVariables: { ARTIFACT_NAME: namedRef },
    },
  }];
}

async function mountAndSave(nodes: any[]): Promise<any> {
  const hook = renderHook(() => useWorkflowApi(nodes, [], () => {}, () => {}, {}));
  await settle();
  act(() => { hook.result.current.markDirty(); });
  await settle();
  act(() => { hook.result.current.handleSaveInitiation('saveAs'); });
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

describe('useWorkflowApi flat named_ref scan (BUG.WB-NODE-VARS-UI m4 leg)', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    runCalls = 0;
    _resetPlatformFeaturesCache();
    global.fetch = jest.fn(async (url: string) => {
      const u = String(url);
      if (FEATURES_URL.test(u)) return makeResponse(200, { entitlements: { edition: 'ce', capabilities: [] } });
      if (RUN_URL.test(u)) { runCalls += 1; return makeResponse(200, { workflow_run_id: 99, status: 'running' }); }
      if (VERSIONS_URL.test(u)) return makeResponse(200, { id: 21, version_number: 1, workflow_id: 11 });
      if (CREATE_URL.test(u)) return makeResponse(200, { id: 11, name: 'W Copy', archived_at: null });
      return makeResponse(200, []);
    }) as unknown as typeof global.fetch;
    window.history.replaceState({}, '', '/?token=tok');
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  // (f) flat named_ref with a chosen NAME: no file needed -> POST {API}/road/11/run
  // (useWorkflowApi.ts:1946 runUrl). Dropping the hook mirror makes this RED: the
  // scan misses the flat var, the gate opens the "Input File Required" dialog and
  // runCalls stays 0.
  it('flat named_ref input with a name: no file dialog, run POST goes out', async () => {
    const hook = await mountAndSave(flatNamedRefInput('brand-voice'));
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(false);
    expect(runCalls).toBe(1);
  });

  // (g) fail closed: flat named_ref with an EMPTY value still needs the dialog.
  it('flat named_ref input with no name chosen: dialog opens, no run', async () => {
    const hook = await mountAndSave(flatNamedRefInput(''));
    act(() => { hook.result.current.executeWorkflow(); });
    await settle();
    expect(hook.result.current.isMissingFileDialogOpen).toBe(true);
    expect(runCalls).toBe(0);
  });
});
