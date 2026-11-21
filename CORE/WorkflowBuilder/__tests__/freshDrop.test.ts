/**
 * Reproducer for Tim's #3: dialog fires after just dropping 3 nodes.
 */
import {
  isUserInputKey,
  isEmptyValue,
  countNodesWithUserInput,
  nodesHaveUserInput,
} from '@/lib/save-sanitization';

describe('freshly-dropped node should not trigger dialog', () => {
  // Exact shape createNode() in workflow-utils.ts produces
  const freshlyDroppedNode = (id: string, label: string, type: string) => ({
    id,
    type,
    position: { x: 100, y: 100 },
    data: {
      label,
      description: 'Some description from NodeDef',
      nodeDefinitionId: 42,
      process_type: 'CPU',
      input_schema: { input_type: 'image', file_mode: 'single' },
      output_schema: { output_type: 'image', file_mode: 'single' },
      configuration_schema: { extra_variables: 'NONE' },
      custom_metadata: null,
      cost_in_credits: 1,
      gpu_variants: { cpu: 'image:cpu' },
      node_kind: 'some_dag',
    },
  });

  it('three freshly-dropped nodes do not trigger detection', () => {
    const nodes = [
      freshlyDroppedNode('n1', 'Image Input', 'input'),
      freshlyDroppedNode('n2', 'Albedo Texture Process', 'process'),
      freshlyDroppedNode('n3', 'Return Assets Export', 'output'),
    ];

    // Walk every key on every data and report which match
    const matches: Array<[string, string, string, unknown]> = [];
    for (const n of nodes) {
      for (const [k, v] of Object.entries(n.data)) {
        if (isUserInputKey(k) && !isEmptyValue(v)) {
          matches.push([n.id, n.data.label, k, v]);
        }
      }
    }
    if (matches.length > 0) {
      console.log('MATCHES (these trigger the dialog):', matches);
    }
    expect(nodesHaveUserInput(nodes)).toBe(false);
    expect(countNodesWithUserInput(nodes)).toBe(0);
  });

  it('node with nickname / status / preview / accepted_formats still false', () => {
    const node = {
      id: 'n1', type: 'input', position: { x: 0, y: 0 },
      data: {
        label: 'Image Input',
        nickname: 'Hero Shot',
        status: 'success',
        previewAssets: [{ url: 'foo' }],
        accepted_formats: ['png', 'jpg'],
        outputCount: 1,
        input_schema: { input_type: 'image', file_mode: 'single' },
        output_schema: { output_type: 'image' },
        nodeDefinitionId: 1,
        process_type: 'CPU',
        cost_in_credits: 1,
      },
    };
    expect(nodesHaveUserInput([node])).toBe(false);
  });

  it('inputValue with non-empty string DOES trigger', () => {
    const node = {
      id: 'n1', type: 'process', position: { x: 0, y: 0 },
      data: { label: 'X', inputValue: 'user typed this' },
    };
    expect(nodesHaveUserInput([node])).toBe(true);
  });

  it('selectedFiles populated DOES trigger (Image Input scenario)', () => {
    const node = {
      id: 'n1', type: 'input', position: { x: 0, y: 0 },
      data: { label: 'Image Input', selectedFiles: [{ id: 'cog-42', name: 'photo.jpg' }] },
    };
    expect(nodesHaveUserInput([node])).toBe(true);
  });
});
