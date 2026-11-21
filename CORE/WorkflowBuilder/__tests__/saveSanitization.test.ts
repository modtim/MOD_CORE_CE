/**
 * Tests for save-sanitization helpers — the workflow save flow uses
 * these to detect, strip, and remember per-workflow suppression of
 * the user-input prompt (issue #21).
 */

import {
  USER_INPUT_KEY_ALLOWLIST,
  USER_INPUT_KEY_REGEX,
  isUserInputKey,
  countNodesWithUserInput,
  nodesHaveUserInput,
  stripUserInputFromNodes,
  userInputSnapshot,
  getSanitizationSuppressed,
  setSanitizationSuppressed,
} from '@/lib/save-sanitization';

describe('isUserInputKey', () => {
  it('matches every key in the curated allowlist', () => {
    for (const key of USER_INPUT_KEY_ALLOWLIST) {
      expect(isUserInputKey(key)).toBe(true);
    }
  });

  it('matches keys that fit the regex (input/user/param + uppercase letter)', () => {
    expect(isUserInputKey('inputUrl')).toBe(true);
    expect(isUserInputKey('userPrompt')).toBe(true);
    expect(isUserInputKey('paramThreshold')).toBe(true);
  });

  it('LDF-3 §4 — does NOT match system metadata keys that share prefixes', () => {
    // Tim's bug: input_schema / output_schema / input_type matched the
    // regex `^(input|user|param)[A-Z_]/` and falsely tripped detection
    // on every node, even those with no user input keyed.
    expect(isUserInputKey('input_schema')).toBe(false);
    expect(isUserInputKey('output_schema')).toBe(false);
    expect(isUserInputKey('configuration_schema')).toBe(false);
    expect(isUserInputKey('input_type')).toBe(false);
    expect(isUserInputKey('output_type')).toBe(false);
    expect(isUserInputKey('custom_metadata')).toBe(false);
  });

  it('LDF-3 §4 — covers Input-Node file fields (the strip path missed these)', () => {
    expect(isUserInputKey('selectedFiles')).toBe(true);
    expect(isUserInputKey('selected_files')).toBe(true);
    expect(isUserInputKey('fileId')).toBe(true);
    expect(isUserInputKey('fileName')).toBe(true);
    expect(isUserInputKey('file')).toBe(true);
  });

  it('LDF-3 §4 — covers runtime-overrides + secret-binding keys', () => {
    expect(isUserInputKey('extraVariables')).toBe(true);
    expect(isUserInputKey('extraVariableValues')).toBe(true);
    expect(isUserInputKey('secret_refs')).toBe(true);
    expect(isUserInputKey('secretRefs')).toBe(true);
  });

  it('does NOT match unrelated keys', () => {
    expect(isUserInputKey('label')).toBe(false);
    expect(isUserInputKey('nodeDefinitionName')).toBe(false);
    expect(isUserInputKey('id')).toBe(false);
    expect(isUserInputKey('type')).toBe(false);
    expect(isUserInputKey('position')).toBe(false);
  });

  it('does NOT match plain prefix-only words (no uppercase/_)', () => {
    expect(isUserInputKey('input')).toBe(false);
    expect(isUserInputKey('user')).toBe(false);
    expect(isUserInputKey('param')).toBe(false);
    expect(isUserInputKey('inputs')).toBe(false); // no upper, no underscore
  });

  it('the exported regex is reachable for outside use', () => {
    expect(USER_INPUT_KEY_REGEX.test('inputFoo')).toBe(true);
    expect(USER_INPUT_KEY_REGEX.test('foo')).toBe(false);
  });
});

describe('countNodesWithUserInput / nodesHaveUserInput', () => {
  // Widening the inferred type to a structural shape with extra `id`
  // keeps TS happy when these literals reach helpers typed against
  // SanitizableNode (which doesn't declare `id`). Runtime is unchanged.
  const nodes: Array<{ id: string; data?: object }> = [
    { id: 'a', data: { label: 'Node A' } },
    { id: 'b', data: { label: 'Node B', inputValue: 'hello' } },
    { id: 'c', data: { label: 'Node C', userPrompt: 'world', selectedFlags: ['x'] } },
    { id: 'd' /* no data */ },
  ];

  it('counts only nodes with at least one user-input key', () => {
    expect(countNodesWithUserInput(nodes)).toBe(2);
  });

  it('treats no-data nodes as zero contribution', () => {
    const noData: Array<{ id: string; data?: object }> = [{ id: 'x' }];
    expect(countNodesWithUserInput(noData)).toBe(0);
  });

  it('returns true if any node has user input', () => {
    expect(nodesHaveUserInput(nodes)).toBe(true);
  });

  it('returns false on a fully-clean graph', () => {
    const clean: Array<{ id: string; data?: object }> = [
      { id: 'a', data: { label: 'A' } },
      { id: 'b', data: { nodeDefinitionName: 'foo' } },
    ];
    expect(nodesHaveUserInput(clean)).toBe(false);
  });

  it('returns false on an empty graph', () => {
    expect(nodesHaveUserInput([])).toBe(false);
    expect(countNodesWithUserInput([])).toBe(0);
  });

  it('LDF-3 §4 — empty user-input values do NOT trigger detection', () => {
    // Tim's #3: dialog fired even on workflows where the user hadn't
    // keyed in any actual values. Keys can still be present on `data`
    // (e.g. when the Image Input node is rendered) but if their values
    // are empty, the dialog must NOT fire.
    const empties: Array<{ id: string; data?: object }> = [
      { id: 'a', data: { selectedFiles: [] } },
      { id: 'b', data: { inputValue: '' } },
      { id: 'c', data: { paramValues: {} } },
      { id: 'd', data: { extraVariables: {}, secret_refs: [] } },
      { id: 'e', data: { selectedFlags: [], userInput: null } },
    ];
    expect(nodesHaveUserInput(empties)).toBe(false);
    expect(countNodesWithUserInput(empties)).toBe(0);
  });

  it('LDF-3 §4 — uploaded image triggers detection (Tim\'s actual scenario)', () => {
    const withImage: Array<{ id: string; data?: object }> = [
      {
        id: 'image-input-1',
        data: {
          label: 'Image Input',
          // Stored under selectedFiles by the Input Node — this used
          // to be invisible to detection.
          selectedFiles: [{ id: 'cog-42', name: 'photo.jpg' }],
        },
      },
    ];
    expect(nodesHaveUserInput(withImage)).toBe(true);
    expect(countNodesWithUserInput(withImage)).toBe(1);
  });
});

describe('stripUserInputFromNodes', () => {
  it('removes user-input keys but keeps everything else', () => {
    const before = [
      {
        id: 'a',
        data: { label: 'A', inputValue: 'hello', nodeDefinitionName: 'foo' },
      },
    ];
    const after = stripUserInputFromNodes(before);
    expect(after).toEqual([
      { id: 'a', data: { label: 'A', nodeDefinitionName: 'foo' } },
    ]);
  });

  it('does not mutate the original nodes', () => {
    const before = [
      { id: 'a', data: { inputValue: 'hello', label: 'A' } },
    ];
    stripUserInputFromNodes(before);
    expect(before[0].data).toEqual({ inputValue: 'hello', label: 'A' });
  });

  it('returns nodes without data unchanged', () => {
    const before = [{ id: 'a' }, { id: 'b', data: { label: 'B' } }];
    const after = stripUserInputFromNodes(before);
    expect(after[0]).toEqual({ id: 'a' });
    expect(after[1]).toEqual({ id: 'b', data: { label: 'B' } });
  });

  it('handles regex-matched user input keys', () => {
    const before = [{ id: 'a', data: { paramThreshold: 0.5, label: 'A' } }];
    const after = stripUserInputFromNodes(before);
    expect(after[0].data).toEqual({ label: 'A' });
  });

  it("LDF-3 §4 — strips Image Input's selectedFiles (Tim's bug)", () => {
    const before = [
      {
        id: 'image-input-1',
        data: {
          label: 'Image Input',
          nodeDefinitionName: 'Image Input',
          selectedFiles: [{ id: 'cog-42', name: 'photo.jpg' }],
          fileId: 'cog-42',
          fileName: 'photo.jpg',
        },
      },
    ];
    const after = stripUserInputFromNodes(before);
    // All file-input keys must be removed; canonical metadata stays.
    expect(after[0].data).toEqual({
      label: 'Image Input',
      nodeDefinitionName: 'Image Input',
    });
  });

  it('LDF-3 §4 — strips runtime overrides + secret bindings', () => {
    const before = [
      {
        id: 'a',
        data: {
          label: 'X',
          extraVariables: { THRESHOLD: 0.5 },
          secret_refs: [{ secret_name: 'OPENAI_KEY', env_var: 'OPENAI_API_KEY' }],
        },
      },
    ];
    const after = stripUserInputFromNodes(before);
    expect(after[0].data).toEqual({ label: 'X' });
  });

  it('LDF-3 §4 — does NOT strip system metadata (input_schema etc.)', () => {
    // Pre-fix the regex matched input_schema and the strip helper would
    // have removed it. After §4 the schema-denylist short-circuits.
    const before = [
      {
        id: 'a',
        data: {
          label: 'A',
          input_schema: { input_type: 'image' },
          output_schema: { output_type: 'image' },
          configuration_schema: {},
          inputValue: 'should-go',
        },
      },
    ];
    const after = stripUserInputFromNodes(before);
    expect(after[0].data).toEqual({
      label: 'A',
      input_schema: { input_type: 'image' },
      output_schema: { output_type: 'image' },
      configuration_schema: {},
    });
  });
});

describe('userInputSnapshot', () => {
  it('produces a stable snapshot for the same input', () => {
    const nodes = [
      { id: 'a', data: { inputValue: 'hello', label: 'A' } },
      { id: 'b', data: { userPrompt: 'world' } },
    ];
    expect(userInputSnapshot(nodes)).toBe(userInputSnapshot(nodes));
  });

  it('changes when a new user-input key is added', () => {
    const before = [{ id: 'a', data: { inputValue: 'hello' } }];
    const after = [
      { id: 'a', data: { inputValue: 'hello', userPrompt: 'world' } },
    ];
    expect(userInputSnapshot(before)).not.toBe(userInputSnapshot(after));
  });

  it('does NOT change when a non-user-input key is added', () => {
    const before = [{ id: 'a', data: { inputValue: 'hello' } }];
    const after = [
      { id: 'a', data: { inputValue: 'hello', label: 'New' } },
    ];
    expect(userInputSnapshot(before)).toBe(userInputSnapshot(after));
  });

  it('is order-independent across nodes', () => {
    const a = [
      { id: '1', data: { inputValue: 'x' } },
      { id: '2', data: { userPrompt: 'y' } },
    ];
    const b = [
      { id: '2', data: { userPrompt: 'y' } },
      { id: '1', data: { inputValue: 'x' } },
    ];
    expect(userInputSnapshot(a)).toBe(userInputSnapshot(b));
  });

  it('returns an empty string for clean nodes', () => {
    expect(userInputSnapshot([
      { id: 'a', data: { label: 'A' } },
      { id: 'b' },
    ])).toBe('');
  });
});

describe('suppression flag persistence', () => {
  beforeEach(() => {
    // jsdom provides a real localStorage; clear between tests
    window.localStorage.clear();
  });

  it('returns null when no suppression set', () => {
    expect(getSanitizationSuppressed(42)).toBeNull();
  });

  it('round-trips a snapshot value', () => {
    setSanitizationSuppressed(42, 'a:inputValue|b:userPrompt');
    const got = getSanitizationSuppressed(42);
    expect(got?.snapshot).toBe('a:inputValue|b:userPrompt');
  });

  it('isolates per-workflow keys', () => {
    setSanitizationSuppressed(42, 'X');
    setSanitizationSuppressed(99, 'Y');
    expect(getSanitizationSuppressed(42)?.snapshot).toBe('X');
    expect(getSanitizationSuppressed(99)?.snapshot).toBe('Y');
  });

  it('returns null on parse failure', () => {
    window.localStorage.setItem(
      'mod.workflowSave.suppressSanitization.42',
      'not-json{',
    );
    expect(getSanitizationSuppressed(42)).toBeNull();
  });
});
