/**
 * custom-edge.tsx "FAN ×N" badge (2400LR-jst1). The edge shows the badge only
 * when data.fanCount > 1; its accessible name comes from its text content
 * ("FAN ×N"); clicking dispatches the DOM CustomEvent "mod:open-fanout" with
 * { nodeId, count } and must not bubble to the edge-delete handler.
 * Only module-level reactflow/lucide hooks are mocked.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import CustomEdge from '@/components/custom-edge';

jest.mock('reactflow', () => ({
  BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  getBezierPath: () => ['M0,0', 10, 20],
  useReactFlow: () => ({ setEdges: jest.fn() }),
}));
jest.mock('lucide-react', () => ({ GitBranch: () => <svg data-testid="icon" /> }));

function renderEdge(data?: Record<string, unknown>, source = 'node-src') {
  const props: any = {
    id: 'e1', source, target: 'node-tgt',
    sourceX: 0, sourceY: 0, targetX: 100, targetY: 100,
    sourcePosition: 'right', targetPosition: 'left',
    data,
  };
  return render(<CustomEdge {...props} />);
}

describe('CustomEdge FAN badge', () => {
  it('renders a button named exactly "FAN ×2" when fanCount is 2', () => {
    renderEdge({ fanCount: 2 });
    expect(screen.getByRole('button', { name: 'FAN ×2' })).toBeTruthy();
  });

  it('shows the actual count in the name for larger fan-outs', () => {
    renderEdge({ fanCount: 7 });
    expect(screen.getByRole('button', { name: 'FAN ×7' })).toBeTruthy();
  });

  it.each([[0], [1], [undefined]])('renders no badge when fanCount is %s', (fanCount) => {
    renderEdge({ fanCount });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders no badge when edge data is absent', () => {
    renderEdge(undefined);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('click dispatches mod:open-fanout with data.fanSourceId and count', () => {
    const seen: any[] = [];
    const h = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('mod:open-fanout', h);
    try {
      renderEdge({ fanCount: 3, fanSourceId: 'fan-node' });
      fireEvent.click(screen.getByRole('button', { name: 'FAN ×3' }));
    } finally {
      window.removeEventListener('mod:open-fanout', h);
    }
    expect(seen).toEqual([{ nodeId: 'fan-node', count: 3 }]);
  });

  it('click falls back to the edge source id when fanSourceId is unset', () => {
    const seen: any[] = [];
    const h = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('mod:open-fanout', h);
    try {
      renderEdge({ fanCount: 2 }, 'src-9');
      fireEvent.click(screen.getByRole('button', { name: 'FAN ×2' }));
    } finally {
      window.removeEventListener('mod:open-fanout', h);
    }
    expect(seen).toEqual([{ nodeId: 'src-9', count: 2 }]);
  });

  it('click does not propagate to ancestors (stopPropagation)', () => {
    const parentClick = jest.fn();
    const { container } = render(
      <div onClick={parentClick}>
        <CustomEdge {...({
          id: 'e1', source: 's', target: 't', sourceX: 0, sourceY: 0, targetX: 1, targetY: 1,
          sourcePosition: 'right', targetPosition: 'left', data: { fanCount: 2 },
        } as any)} />
      </div>,
    );
    fireEvent.click(container.querySelector('button')!);
    expect(parentClick).not.toHaveBeenCalled();
  });
});
