/**
 * Tests for lib/ai-output-label.ts — isAiNode() derivation for the
 * FX.72 p4 "AI-generated" badge (canvas header + output preview).
 */

import {
  AI_OUTPUT_LABEL_TEXT,
  AI_OUTPUT_TOOLTIP,
  isAiNode,
  upstreamIsAi,
} from '@/lib/ai-output-label';

describe('isAiNode', () => {
  it('flags seeded LLM node kinds', () => {
    expect(isAiNode({ node_kind: 'text_llm' })).toBe(true);
    expect(isAiNode({ node_kind: 'switchyard_llm' })).toBe(true);
    expect(isAiNode({ node_kind: 'ai_conversation' })).toBe(true);
  });

  it('flags every nim_* NIM node kind', () => {
    expect(isAiNode({ node_kind: 'nim_studiovoice_local' })).toBe(true);
    expect(isAiNode({ node_kind: 'nim_vsr_local' })).toBe(true);
  });

  it('flags diffusion/classifier/trainer kinds', () => {
    expect(isAiNode({ node_kind: 'sdxl_generate' })).toBe(true);
    expect(isAiNode({ node_kind: 'depth_anything' })).toBe(true);
    expect(isAiNode({ node_kind: 'local_ai_train' })).toBe(true);
  });

  it('is case-insensitive on node_kind', () => {
    expect(isAiNode({ node_kind: 'Text_LLM' })).toBe(true);
    expect(isAiNode({ node_kind: 'NIM_VSR_LOCAL' })).toBe(true);
  });

  it('flags custom_metadata AI flags and category/tags', () => {
    expect(isAiNode({ custom_metadata: { ai_model: true } })).toBe(true);
    expect(isAiNode({ custom_metadata: { generative: true } })).toBe(true);
    expect(isAiNode({ custom_metadata: { category: 'Image Generation' } })).toBe(true);
    expect(isAiNode({ custom_metadata: { tags: ['ML', 'vision'] } })).toBe(true);
  });

  it('flags a library row whose type category is an AI tag', () => {
    expect(isAiNode({ type: 'AI Model' })).toBe(true);
    expect(isAiNode({ type: 'ai' })).toBe(true);
    expect(isAiNode({ type: 'Generative' })).toBe(true);
  });

  it('does NOT flag converter/operator/converter kinds', () => {
    expect(isAiNode({ node_kind: 'image_converter' })).toBe(false);
    expect(isAiNode({ node_kind: 'video_converter' })).toBe(false);
    expect(isAiNode({ node_kind: 'human_approval' })).toBe(false);
    expect(isAiNode({ node_kind: 'container_adapter' })).toBe(false);
  });

  it('does NOT flag on user-visible label/nickname alone', () => {
    expect(isAiNode({ label: 'My AI Helper', nickname: 'ai' })).toBe(false);
  });

  it('tolerates null/undefined data', () => {
    expect(isAiNode({})).toBe(false);
    expect(isAiNode({ node_kind: null })).toBe(false);
    expect(isAiNode({ custom_metadata: null })).toBe(false);
    expect(isAiNode({ custom_metadata: 'not-an-object' })).toBe(false);
  });

  it('exposes the accessible label text and tooltip', () => {
    expect(AI_OUTPUT_LABEL_TEXT).toBe('AI-generated');
    expect(AI_OUTPUT_TOOLTIP).toBe('Produced by an AI model — review before use');
  });
});

describe('upstreamIsAi (FX.72 p4b: output-node preview badge)', () => {
  const nodes = [
    { id: 'llm-1', data: { node_kind: 'text_llm', label: 'Summarise' } },
    { id: 'conv-1', data: { node_kind: 'image_converter', label: 'Resize' } },
    { id: 'out-1', data: { label: 'Output' } },
    { id: 'out-2', data: { label: 'Output' } },
  ];

  it('flags an output fed directly by an AI node', () => {
    const edges = [{ id: 'e1', source: 'llm-1', target: 'out-1' }];
    expect(upstreamIsAi('out-1', nodes, edges)).toBe(true);
  });

  it('does NOT flag an output fed only by a non-AI converter', () => {
    const edges = [{ id: 'e2', source: 'conv-1', target: 'out-2' }];
    expect(upstreamIsAi('out-2', nodes, edges)).toBe(false);
    // distance-2 AI (llm -> conv -> out) stays unlabelled: provenance is the
    // converter's, matching process-node.tsx labelling only its own node.
    const twoHop = [
      { id: 'e0', source: 'llm-1', target: 'conv-1' },
      { id: 'e2', source: 'conv-1', target: 'out-2' },
    ];
    expect(upstreamIsAi('out-2', nodes, twoHop)).toBe(false);
  });

  it('tolerates missing ids, dangling edges and empty graphs', () => {
    expect(upstreamIsAi('out-1', [], [])).toBe(false);
    expect(upstreamIsAi('', nodes, [{ source: 'llm-1', target: 'out-1' }])).toBe(false);
    // edge references a node not in the graph (mid-drag / deleted producer)
    expect(upstreamIsAi('out-1', nodes, [{ source: 'ghost', target: 'out-1' }])).toBe(false);
    // node with no data payload must not throw
    expect(upstreamIsAi('out-1', [{ id: 'x' }, ...nodes], [{ source: 'x', target: 'out-1' }])).toBe(false);
  });
});
