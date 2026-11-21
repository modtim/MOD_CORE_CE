/**
 * LDF-4 #5 — Process queue (WorkflowQueueDialog) actions:
 *   - Resume button is gone for failed runs.
 *   - Watch button only shows for running/queued runs that carry
 *     workflow_id + workflow_version_id, and clicking it closes the
 *     dialog + invokes onWatch with (workflow_id, version_id, run_id).
 *
 * The dialog calls fetch() to populate `runs`. We mock global.fetch
 * to return a known shape so we can assert per-row affordances
 * deterministically.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkflowQueueDialog } from '@/components/WorkflowQueueDialog';

type RunStatus = 'completed' | 'failed' | 'running' | 'queued';

interface RunFixture {
  id: number;
  run_name: string | null;
  status: RunStatus;
  started_at: string | null;
  completed_at: string | null;
  workflow_id: number | null;
  workflow_version_id: number | null;
  workflow_name: string | null;
  version_number: number | null;
  orchestrator_run_id: string | null;
  run_files: Array<{ id: number; file_id: string; title: string; status: string | null }>;
}

const baseRun = (over: Partial<RunFixture>): RunFixture => {
  // Using `??` would clobber explicit `null` (e.g. workflow_version_id:
  // null) back to the default — which is exactly the case the
  // "hides Watch when …_id is null" test exercises. Spread last so
  // explicit nulls win.
  return {
    id: 100,
    run_name: `Run ${over.id ?? 100}`,
    status: 'running',
    started_at: new Date().toISOString(),
    completed_at: null,
    workflow_id: 7,
    workflow_version_id: 3,
    workflow_name: 'Demo workflow',
    version_number: 1,
    orchestrator_run_id: 'oid-x',
    run_files: [],
    ...over,
  };
};

function mockFetchOnce(runs: RunFixture[]) {
  (global as any).fetch = jest.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: () => Promise.resolve(runs),
    }),
  );
}

async function renderDialog(props: {
  runs: RunFixture[];
  onWatch?: jest.Mock;
  onClose?: jest.Mock;
}) {
  mockFetchOnce(props.runs);
  const onClose = props.onClose ?? jest.fn();
  const onWatch = props.onWatch;
  await act(async () => {
    render(
      <WorkflowQueueDialog
        isOpen
        onClose={onClose}
        accessToken="test-token"
        onWatch={onWatch}
      />,
    );
  });
  // Wait for the fetch-driven row(s) to land. queryByText returns null
  // when MULTIPLE elements match — using getAllByText so two-row
  // fixtures (e.g. completed + failed) don't false-fail the wait.
  await waitFor(() =>
    expect(screen.getAllByText(/^Run \d+$/).length).toBeGreaterThan(0),
  );
  return { onClose, onWatch };
}

afterEach(() => {
  jest.restoreAllMocks();
  delete (global as any).fetch;
});

describe('WorkflowQueueDialog (LDF-4 #5)', () => {
  it('does not render the Resume button for a failed run', async () => {
    await renderDialog({
      runs: [baseRun({
        id: 1, status: 'failed',
        run_files: [{ id: 1, file_id: 'f1', title: 'a.png', status: 'failed' }],
      })],
    });
    expect(screen.queryByRole('button', { name: /^resume$/i })).toBeNull();
    expect(screen.getByRole('button', { name: /restart/i })).toBeInTheDocument();
  });

  it('renders Watch on a running run that has workflow_id + version_id', async () => {
    await renderDialog({
      runs: [baseRun({ id: 2, status: 'running' })],
      onWatch: jest.fn(),
    });
    expect(screen.getByRole('button', { name: /watch/i })).toBeInTheDocument();
  });

  it('renders Watch on a queued run', async () => {
    await renderDialog({
      runs: [baseRun({ id: 3, status: 'queued' })],
      onWatch: jest.fn(),
    });
    expect(screen.getByRole('button', { name: /watch/i })).toBeInTheDocument();
  });

  it('hides Watch on completed and failed runs', async () => {
    await renderDialog({
      runs: [
        baseRun({ id: 4, status: 'completed', completed_at: new Date().toISOString() }),
        baseRun({ id: 5, status: 'failed' }),
      ],
      onWatch: jest.fn(),
    });
    expect(screen.queryByRole('button', { name: /watch/i })).toBeNull();
  });

  it('hides Watch when workflow_version_id is null', async () => {
    await renderDialog({
      runs: [baseRun({ id: 6, status: 'running', workflow_version_id: null })],
      onWatch: jest.fn(),
    });
    expect(screen.queryByRole('button', { name: /watch/i })).toBeNull();
  });

  it('hides Watch when onWatch prop is not provided', async () => {
    await renderDialog({
      runs: [baseRun({ id: 7, status: 'running' })],
    });
    expect(screen.queryByRole('button', { name: /watch/i })).toBeNull();
  });

  it('clicking Watch closes the dialog then calls onWatch with (workflow_id, version_id, run_id)', async () => {
    const onWatch = jest.fn();
    const onClose = jest.fn();
    await renderDialog({
      runs: [baseRun({ id: 42, status: 'running', workflow_id: 9, workflow_version_id: 11 })],
      onWatch,
      onClose,
    });
    fireEvent.click(screen.getByRole('button', { name: /watch/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onWatch).toHaveBeenCalledTimes(1);
    expect(onWatch).toHaveBeenCalledWith(9, 11, 42);
  });
});
