/**
 * NodeHoverPanel viewport clamp (2400LR-b9c).
 *
 * The panel is position:fixed from the trigger's top; for a card low in the
 * palette it used to run off-screen, making its buttons unreachable.
 */
import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { NodeHoverPanel } from '@/components/ui/node-hover-panel';

const VH = 768;
let panelHeight = 400;

function renderOpen(): HTMLElement {
  render(
    <NodeHoverPanel trigger={<button>card</button>} openDelay={10}>
      <span data-testid="panel-body">body</span>
    </NodeHoverPanel>,
  );
  fireEvent.mouseEnter(screen.getByText('card').parentElement as HTMLElement);
  act(() => {
    jest.advanceTimersByTime(50);
  });
  return screen.getByTestId('panel-body').parentElement as HTMLElement;
}

describe('NodeHoverPanel viewport clamp', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    Object.defineProperty(window, 'innerHeight', { value: VH, configurable: true });
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true });
    jest
      .spyOn(Element.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: Element) {
        const el = this as HTMLElement;
        if (el.className && String(el.className).includes('bg-popover')) {
          return { x: 0, y: 0, top: 0, left: 0, right: 320, bottom: panelHeight, width: 320, height: panelHeight, toJSON: () => ({}) } as DOMRect;
        }
        if (el.style && el.style.display === 'block') {
          const top = VH - 50;
          return { x: 0, y: top, top, left: 0, right: 200, bottom: top + 40, width: 200, height: 40, toJSON: () => ({}) } as DOMRect;
        }
        return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}) } as DOMRect;
      });
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('pulls the panel up so its bottom edge stays inside the viewport', () => {
    panelHeight = 400;
    const panel = renderOpen();
    const top = parseFloat(panel.style.top);
    expect(top).toBeLessThanOrEqual(VH - 400 - 8);
    expect(top).toBeGreaterThanOrEqual(8);
  });

  it('caps a panel taller than the viewport and lets it scroll internally', () => {
    panelHeight = 2000;
    const panel = renderOpen();
    expect(panel.style.maxHeight).toBe(`${VH - 16}px`);
    expect(panel.style.overflowY).toBe('auto');
    expect(parseFloat(panel.style.top)).toBe(8);
  });
});
