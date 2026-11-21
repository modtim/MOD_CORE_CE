/**
 * 2400LR-b2y: unit coverage for the workflow-loader "stuck -> Retry" path.
 *
 * workflow-loader.tsx keeps a bare "Loading..." forever if the next/dynamic
 * chunk fetch never resolves. After STUCK_AFTER_MS it flips a `stuck` state and
 * renders "This is taking longer than expected." plus a Retry button. jsdom's
 * location.reload cannot be spied, so Loading takes an optional `onRetry` prop
 * (default: window.location.reload()) that the test injects.
 */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { Loading } from '@/components/workflow-loader';

// Mirrors STUCK_AFTER_MS in components/workflow-loader.tsx:13 (not exported).
const STUCK_AFTER_MS = 8000;
const STUCK_TEXT = 'This is taking longer than expected.';

beforeEach(() => {
  jest.useFakeTimers();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe('workflow-loader stuck -> Retry', () => {
  it('shows Loading only until STUCK_AFTER_MS, then the stuck text and Retry', () => {
    const onRetry = jest.fn();
    render(<Loading onRetry={onRetry} />);
    expect(screen.getByText('Loading...')).toBeTruthy();
    expect(screen.queryByText(STUCK_TEXT)).toBeNull();

    act(() => {
      jest.advanceTimersByTime(STUCK_AFTER_MS - 1);
    });
    expect(screen.queryByText(STUCK_TEXT)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();

    act(() => {
      jest.advanceTimersByTime(1);
    });
    expect(screen.getByText(STUCK_TEXT)).toBeTruthy();
    const retry = screen.getByRole('button', { name: 'Retry' });

    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('unmounting before the timeout never flips to stuck and logs no error', () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const onRetry = jest.fn();
    const { unmount } = render(<Loading onRetry={onRetry} />);
    expect(screen.queryByText(STUCK_TEXT)).toBeNull();

    unmount();
    act(() => {
      jest.advanceTimersByTime(STUCK_AFTER_MS + 50);
    });

    expect(screen.queryByText(STUCK_TEXT)).toBeNull();
    expect(errorSpy).not.toHaveBeenCalled();
    expect(onRetry).not.toHaveBeenCalled();
    expect(jest.getTimerCount()).toBe(0);
  });
});
