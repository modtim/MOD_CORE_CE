/**
 * Tests for the number-input fix in the node config panel's VariableControl.
 *
 * Regression: on the VSR node, TARGET_WIDTH / TARGET_HEIGHT (type: "number"
 * extra_variables) were impossible to type into — every keystroke re-parsed /
 * clamped the value, so the field couldn't be cleared, leading/partial input got
 * coerced, and the caret jumped. The fix keeps the raw string in local state
 * while editing and only coerces to a number on blur/Enter. These tests drive
 * the real component with a controlled parent (mirroring onExtraVariableChange).
 */

import React, { useState } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VariableControl } from '@/components/node-config-panel';

// Radix Slider (used by the slider-backed number path) measures layout via
// ResizeObserver / pointer-capture APIs that jsdom doesn't implement.
beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

// Controlled harness mirroring the panel's extraVariables flow: onChange commits
// the value up, which flows back down as `value` (just like updateNodeData).
function Harness({
  config,
  initial,
  onCommit,
}: {
  config: any;
  initial?: any;
  onCommit?: (v: any) => void;
}) {
  const [val, setVal] = useState<any>(initial);
  return (
    <div>
      <VariableControl
        name="TARGET_WIDTH"
        config={config}
        value={val}
        onChange={(v: any) => {
          setVal(v);
          onCommit?.(v);
        }}
      />
      <span data-testid="committed">{String(val)}</span>
    </div>
  );
}

const numberConfig = { type: 'number', description: 'Target width in pixels', default: 1920 };

describe('VariableControl number input', () => {
  it('lets you type a full number freely and commits it on blur', async () => {
    const user = userEvent.setup();
    render(<Harness config={numberConfig} initial={1920} />);
    const input = screen.getByRole('spinbutton') as HTMLInputElement;

    await user.clear(input);
    await user.type(input, '1080');
    // While typing, the raw string is shown — no mid-typing coercion.
    expect(input.value).toBe('1080');

    // Not committed until blur.
    fireEvent.blur(input);
    expect(screen.getByTestId('committed')).toHaveTextContent('1080');
  });

  it('can be fully cleared without snapping back to the old value', async () => {
    const user = userEvent.setup();
    render(<Harness config={numberConfig} initial={1920} />);
    const input = screen.getByRole('spinbutton') as HTMLInputElement;

    await user.clear(input);
    // The bug: clearing produced "" -> NaN -> onChange skipped -> field re-rendered
    // with the stale value. It must now stay empty.
    expect(input.value).toBe('');
  });

  it('supports clear-then-retype (backspace-friendly editing)', async () => {
    const user = userEvent.setup();
    render(<Harness config={numberConfig} initial={1920} />);
    const input = screen.getByRole('spinbutton') as HTMLInputElement;

    await user.clear(input);
    await user.type(input, '1920');
    expect(input.value).toBe('1920');
    fireEvent.blur(input);
    expect(screen.getByTestId('committed')).toHaveTextContent('1920');
  });

  it('commits an integer with parseInt, not a float', async () => {
    const user = userEvent.setup();
    const onCommit = jest.fn();
    render(
      <Harness
        config={{ type: 'integer', description: 'count' }}
        initial={undefined}
        onCommit={onCommit}
      />,
    );
    const input = screen.getByRole('spinbutton') as HTMLInputElement;
    await user.type(input, '42');
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenLastCalledWith(42);
  });

  it('does not re-hydrate from an incoming prop change while the field is focused', () => {
    // Simulates a poll/refresh pushing a new value mid-edit. The user's in-progress
    // draft must win until they blur.
    function RefreshHarness() {
      const [val, setVal] = useState<any>(100);
      return (
        <div>
          <button onClick={() => setVal(9999)}>refresh</button>
          <VariableControl
            name="TARGET_WIDTH"
            config={numberConfig}
            value={val}
            onChange={setVal}
          />
        </div>
      );
    }
    render(<RefreshHarness />);
    const input = screen.getByRole('spinbutton') as HTMLInputElement;

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '55' } });
    expect(input.value).toBe('55');

    // External refresh fires while focused — must NOT clobber the draft.
    act(() => {
      fireEvent.click(screen.getByText('refresh'));
    });
    expect(input.value).toBe('55');
  });

  it('clamps to the schema min/max only on commit for slider-backed numbers', async () => {
    const user = userEvent.setup();
    const onCommit = jest.fn();
    // min/max present -> slider path; typing must not clamp mid-edit.
    render(
      <Harness
        config={{ type: 'integer', description: 'size', min: 100, max: 4096, step: 1 }}
        initial={1920}
        onCommit={onCommit}
      />,
    );
    const input = screen.getByRole('spinbutton') as HTMLInputElement;
    await user.clear(input);
    await user.type(input, '9000');
    // Not clamped while typing.
    expect(input.value).toBe('9000');
    fireEvent.blur(input);
    // Clamped to max on commit.
    expect(onCommit).toHaveBeenLastCalledWith(4096);
  });
});

describe('VariableControl non-number controls still work', () => {
  it('renders a boolean as a switch and toggles', () => {
    const onChange = jest.fn();
    render(
      <VariableControl
        name="ENABLED"
        config={{ type: 'boolean', description: 'on/off' }}
        value={false}
        onChange={onChange}
      />,
    );
    const sw = screen.getByRole('switch');
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('renders a string as a text input and commits on every keystroke', async () => {
    const user = userEvent.setup();
    render(
      <Harness config={{ type: 'string', description: 'label' }} initial={''} />,
    );
    const input = screen.getByRole('textbox') as HTMLInputElement;
    await user.type(input, 'hi');
    expect(input.value).toBe('hi');
    expect(screen.getByTestId('committed')).toHaveTextContent('hi');
  });
});
