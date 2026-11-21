/**
 * DOM smoke tests for BuildQueueRow — confirms the phase header is
 * rendered for each phase, and the legacy back-compat path (phase
 * undefined) produces no header. The full polling/fetch behaviour is
 * exercised by integration; here we only assert what's visible to a
 * user reading the queue panel.
 *
 * Setting `outcome: "success"` on the entry makes BuildQueueRow start
 * in stage="done" and short-circuits the polling effects so we don't
 * have to mock the network layer.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import BuildQueueRow, { type BuildQueueEntry } from '@/components/build-queue-row';

// authenticatedFetch is called from inside the row's polling effects.
// Even though stage="done" short-circuits most of them, the variant-
// verification poll fires once on mount; mock it so jsdom doesn't
// fail with "fetch is not defined".
jest.mock('@/lib/api-client', () => ({
  authenticatedFetch: jest.fn(() =>
    Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) }),
  ),
}));

const noop = () => {};

function renderRow(entry: BuildQueueEntry) {
  return render(
    <BuildQueueRow
      entry={entry}
      accessToken="test-token"
      apiUrl="http://localhost:8000"
      onFinished={noop}
      onDismiss={noop}
    />,
  );
}

describe('BuildQueueRow phase header', () => {
  it('renders "Step 1 of 2 · Generate files" for phase=generation', () => {
    renderRow({
      taskId: 1, nodeName: 'demo', startedAt: Date.now(),
      outcome: 'success', phase: 'generation',
    });
    expect(screen.getByText(/Step 1 of 2 · Generate files/)).toBeInTheDocument();
    expect(screen.queryByText(/Step 2 of 2/)).not.toBeInTheDocument();
  });

  it('renders "Step 2 of 2 · Build & register" for phase=build', () => {
    renderRow({
      taskId: 2, nodeName: 'demo', startedAt: Date.now(),
      outcome: 'success', phase: 'build',
    });
    expect(screen.getByText(/Step 2 of 2 · Build & register/)).toBeInTheDocument();
    expect(screen.queryByText(/Step 1 of 2/)).not.toBeInTheDocument();
  });

  it('renders no phase header for legacy entries with no phase field (back-compat)', () => {
    renderRow({
      taskId: 3, nodeName: 'demo', startedAt: Date.now(),
      outcome: 'success', // phase intentionally omitted
    });
    expect(screen.queryByText(/Step 1 of 2/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Step 2 of 2/)).not.toBeInTheDocument();
    // The "Porting CUDA kernels" chip is now marker-gated: it stays hidden
    // until the build log shows a real port pass. This row has no such log,
    // so the chip must NOT render (CPU-only nodes never port).
    expect(screen.queryByText(/Porting CUDA kernels/)).not.toBeInTheDocument();
  });
});
