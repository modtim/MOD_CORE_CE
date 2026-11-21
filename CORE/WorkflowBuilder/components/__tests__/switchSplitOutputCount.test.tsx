/**
 * Switch/Split "Number of Outputs" (2400LR-jst2). SwitchNode / SplitNode render
 * one source Handle per outputCount (default 3 / 2) titled "Case N" / "Path N"
 * (what e2e wb-ce-batch23 locates by title). The config panel's #outputCount
 * input calls updateNodeData(node.id, {...data, outputCount}) for any integer
 * > 0 and IGNORES (does not clamp) 0, negatives and NaN; there is no max.
 * Mocked: reactflow (Handle -> div[title], plus data-handle-type) and the
 * per-node stack overlay (needs the stack-binding hook; unrelated here).
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('reactflow', () => ({
  Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
  Handle: ({ type, title }: { type: string; title?: string }) => (
    <div data-testid={`handle-${type}`} data-handle-type={type} title={title} />
  ),
}));
jest.mock('@/components/nodes/node-stack-overlay', () => ({ NodeStackOverlay: () => null }));

beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

import { SwitchNode } from '@/components/nodes/switch-node';
import { SplitNode } from '@/components/nodes/split-node';
import NodeConfigPanel from '@/components/node-config-panel';

const ORIG_FETCH = (global as any).fetch;
afterEach(() => { (global as any).fetch = ORIG_FETCH; });

function outputTitles(): string[] {
  return screen.queryAllByTestId('handle-source').map((h) => h.getAttribute('title') || '');
}
const seq = (prefix: string, n: number) => Array.from({ length: n }, (_, i) => `${prefix} ${i + 1}`);

describe('SwitchNode output handles', () => {
  it.each([1, 3, 5])('renders exactly %i output handles titled Case 1..N', (n) => {
    render(<SwitchNode {...({ data: { label: 'S', outputCount: n }, isConnectable: true } as any)} />);
    expect(outputTitles()).toEqual(seq('Case', n));
    expect(screen.getAllByTestId('handle-target')).toHaveLength(1);
  });

  it('defaults to 3 cases when outputCount is unset', () => {
    render(<SwitchNode {...({ data: { label: 'S' }, isConnectable: true } as any)} />);
    expect(outputTitles()).toEqual(seq('Case', 3));
  });

  it('every output title matches the e2e /^Case \\d+$/ locator', () => {
    render(<SwitchNode {...({ data: { outputCount: 4 }, isConnectable: true } as any)} />);
    outputTitles().forEach((t) => expect(t).toMatch(/^Case \d+$/));
  });
});

describe('SplitNode output handles', () => {
  it.each([1, 2, 4])('renders exactly %i output handles titled Path 1..N', (n) => {
    render(<SplitNode {...({ data: { label: 'P', outputCount: n }, isConnectable: true } as any)} />);
    expect(outputTitles()).toEqual(seq('Path', n));
    outputTitles().forEach((t) => expect(t).toMatch(/^Path \d+$/));
  });

  it('defaults to 2 paths when outputCount is unset', () => {
    render(<SplitNode {...({ data: { label: 'P' }, isConnectable: true } as any)} />);
    expect(outputTitles()).toEqual(seq('Path', 2));
  });
});

function openPanel(type: 'switch' | 'split', outputCount?: number) {
  (global as any).fetch = () =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  const updateNodeData = jest.fn();
  const data: any = { label: 'N', ...(outputCount === undefined ? {} : { outputCount }) };
  render(
    <NodeConfigPanel
      node={{ id: 'n1', type, position: { x: 0, y: 0 }, data } as any}
      updateNodeData={updateNodeData}
      onClose={() => {}}
      accessToken="tok"
    />,
  );
  const input = document.getElementById('outputCount') as HTMLInputElement;
  return { input, updateNodeData, data };
}

describe('config panel "Number of Outputs" field', () => {
  it('shows type defaults (switch 3, split 2) when outputCount is unset', () => {
    expect(openPanel('switch').input.value).toBe('3');
    document.body.innerHTML = '';
    expect(openPanel('split').input.value).toBe('2');
  });

  it('typing a valid count calls updateNodeData with the node id and merged data', () => {
    const { input, updateNodeData, data } = openPanel('switch', 3);
    fireEvent.change(input, { target: { value: '5' } });
    expect(updateNodeData).toHaveBeenCalledTimes(1);
    expect(updateNodeData).toHaveBeenCalledWith('n1', { ...data, outputCount: 5 });
  });

  it.each(['0', '-2', ''])('ignores invalid value %p (no clamping, no update)', (v) => {
    const { input, updateNodeData } = openPanel('split', 2);
    fireEvent.change(input, { target: { value: v } });
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  it('has min=1 and no max; a large count is passed through unclamped', () => {
    const { input, updateNodeData } = openPanel('switch', 3);
    expect(input.getAttribute('min')).toBe('1');
    expect(input.hasAttribute('max')).toBe(false);
    fireEvent.change(input, { target: { value: '50' } });
    expect(updateNodeData).toHaveBeenCalledWith('n1', expect.objectContaining({ outputCount: 50 }));
  });
});
