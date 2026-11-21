/**
 * LDF-4 §5.4 — BuildQueuePanel Refresh button.
 *
 * Confirms the Refresh affordance restored after the accidental revert
 * in a022aa2e:
 *   - button is rendered in the panel header
 *   - clicking it re-mounts every visible BuildQueueRow (bumps the
 *     `refreshKey` portion of each row's React key) so per-row poll
 *     effects re-fire
 *   - is debounced via a disabled flag so rapid clicks don't fan out
 *     N concurrent re-fetches per row
 *
 * The Refresh implementation works via a key change on each row,
 * causing React to unmount + remount and re-trigger useEffect-based
 * polls. We assert the unmount+remount by tracking how many distinct
 * BuildQueueRow instances mount across clicks (via the mocked child
 * keeping a mount-count side effect).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import BuildQueuePanel from '@/components/build-queue-panel';
import type { BuildQueueEntry } from '@/components/build-queue-row';

// Mock the row so we can count mounts cheaply and avoid pulling in
// the full polling/fetch machinery (the auto-poll surface is covered
// in buildQueueRow.test.tsx).
const mountCounter = { count: 0 };

jest.mock('@/components/build-queue-row', () => {
  const React = jest.requireActual('react');
  function MockRow({ entry }: { entry: BuildQueueEntry }) {
    React.useEffect(() => {
      mountCounter.count += 1;
    }, []);
    return <div data-testid={`row-${entry.taskId}`}>row-{entry.taskId}</div>;
  }
  return { __esModule: true, default: MockRow };
});

const noop = () => {};

const baseEntry = (over: Partial<BuildQueueEntry>): BuildQueueEntry => ({
  taskId: 1,
  nodeName: 'demo',
  startedAt: Date.now(),
  ...over,
});

function renderPanel(builds: BuildQueueEntry[]) {
  mountCounter.count = 0;
  return render(
    <BuildQueuePanel
      open
      onClose={noop}
      builds={builds}
      accessToken="t"
      apiUrl="http://localhost:8000"
      onFinished={noop}
      onDismiss={noop}
      onClearCompleted={noop}
    />,
  );
}

describe('BuildQueuePanel Refresh button (LDF-4 §5.4)', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });
  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
  });

  it('renders a Refresh button in the panel header', () => {
    renderPanel([baseEntry({ taskId: 1 })]);
    expect(
      screen.getByRole('button', { name: /refresh/i }),
    ).toBeInTheDocument();
  });

  it('clicking Refresh re-mounts every row (re-triggers per-row poll)', async () => {
    const builds = [
      baseEntry({ taskId: 1 }),
      baseEntry({ taskId: 2 }),
      baseEntry({ taskId: 3 }),
    ];
    renderPanel(builds);
    // Initial mount: 3 rows.
    await waitFor(() => expect(mountCounter.count).toBe(3));

    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));
    // After refresh: each row unmounts + remounts → +3 more mounts.
    await waitFor(() => expect(mountCounter.count).toBe(6));
  });

  it('Refresh is debounced — rapid clicks during cooldown do nothing', async () => {
    const builds = [baseEntry({ taskId: 1 })];
    renderPanel(builds);
    await waitFor(() => expect(mountCounter.count).toBe(1));

    const btn = screen.getByRole('button', { name: /refresh/i });
    fireEvent.click(btn);
    // Disabled state set synchronously after first click.
    expect(btn).toBeDisabled();
    // First click already remounted the row.
    await waitFor(() => expect(mountCounter.count).toBe(2));

    // Spam-click while disabled — must NOT remount again.
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(mountCounter.count).toBe(2);

    // After the 1.5s cooldown elapses, the button re-enables and the
    // next click works.
    act(() => { jest.advanceTimersByTime(1500); });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    await waitFor(() => expect(mountCounter.count).toBe(3));
  });

  it('Refresh button is rendered even when there are no completed rows', () => {
    // The pre-revert version only rendered the toolbar row when
    // `completedCount > 0`. The post-restore version renders the
    // toolbar (with Refresh) unconditionally; "Clear completed"
    // remains conditional. This test pins that behavior.
    renderPanel([baseEntry({ taskId: 1 /* no outcome → in-progress */ })]);
    expect(screen.getByRole('button', { name: /refresh/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /clear .* completed/i })).toBeNull();
  });
});
