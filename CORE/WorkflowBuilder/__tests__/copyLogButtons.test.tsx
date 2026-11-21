/**
 * Render-verification for the raw-log Copy buttons.
 *
 * The user asked for a Copy control on EVERY raw-log / stdout / stderr /
 * container-log surface. These tests mount the real components in jsdom and
 * assert the Copy button is actually present in the rendered DOM the user
 * sees (not merely in source), and that the shared control copies text with a
 * "Copied" confirmation. Components whose log panels are only reachable after
 * live log streaming (BuildQueueRow logs panel, NodeConfigPanel execution
 * logs, NewNodeWizard sample output) are covered by source placement + the
 * production build; the shared CopyLogButton they all use is verified here.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CopyLogButton } from '@/components/ui/copy-log-button';
import { WorkflowProgress } from '@/components/workflow-progress';
import { VariantFailureModal } from '@/components/variant-failure-modal';
import { ScanDetailsDialog } from '@/components/scan-details-dialog';
import BuildQueueRow, { type BuildQueueEntry } from '@/components/build-queue-row';

// build-queue-row's live-logs fetcher hits authenticatedFetch — return log
// lines so opening the Logs panel populates logLines and renders the Copy btn.
// A plain function (not jest.fn) so afterEach's resetAllMocks can't wipe it.
jest.mock('@/lib/api-client', () => ({
  authenticatedFetch: () =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ lines: ['build step 1', 'build step 2 OK'], source: 'redis' }),
    }),
}));

// jsdom does not implement scrollIntoView (used by the logs panel autoscroll).
beforeAll(() => {
  (window.HTMLElement.prototype as any).scrollIntoView = jest.fn();
});

afterEach(() => {
  jest.resetAllMocks();
});

describe('CopyLogButton (shared control)', () => {
  it('renders a Copy button and flips to "Copied" after a successful copy', async () => {
    const writeText = jest.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });

    render(<CopyLogButton getText={() => 'hello-log-text'} />);
    const btn = screen.getByRole('button', { name: /copy/i });
    expect(btn).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(btn);
    });
    expect(writeText).toHaveBeenCalledWith('hello-log-text');
    await waitFor(() => expect(screen.getByRole('button', { name: /copied/i })).toBeInTheDocument());
  });

  it('falls back to execCommand when navigator.clipboard is unavailable', async () => {
    // @ts-expect-error — force the legacy path
    navigator.clipboard = undefined;
    const exec = jest.fn(() => true);
    (document as any).execCommand = exec;

    render(<CopyLogButton getText="fallback-text" />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /copy/i }));
    });
    expect(exec).toHaveBeenCalledWith('copy');
  });
});

describe('WorkflowProgress failure-reason Copy button', () => {
  function mockEndpoints(progress: any) {
    (global as any).fetch = jest.fn((url: string) => {
      const body = url.includes('/node-status')
        ? { workflow_run_id: 1, overall_status: progress.status, nodes: [], total_files: 0 }
        : progress;
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: () => Promise.resolve(body),
      });
    });
  }

  it('shows a Copy button in the Failure reason panel of a failed run', async () => {
    mockEndpoints({
      workflow_run_id: 1, status: 'failed', progress_percentage: 0,
      completed_nodes: 0, total_nodes: 1, current_node: null, total_cost: 0,
      started_at: new Date().toISOString(), estimated_seconds_remaining: null,
      error_message: 'orchestrator: pending_scan node blocked dispatch',
    });
    await act(async () => {
      render(<WorkflowProgress workflowRunId={1} accessToken="t" />);
    });
    await waitFor(() => expect(screen.getByText(/Failure reason/i)).toBeInTheDocument());
    // The failure block contains both the raw error text and its Copy button.
    expect(screen.getByText(/pending_scan node blocked dispatch/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^copy$/i })).toBeInTheDocument();
  });
});

describe('VariantFailureModal raw log-excerpt Copy button', () => {
  it('renders a raw-log Copy button distinct from "Copy as issue"', () => {
    render(
      <VariantFailureModal
        open
        onClose={() => {}}
        variant="cuda-12"
        nodeName="demo-node"
        explanation={{
          reason: 'build failed at layer 7',
          manual_fix: 'pin the base image',
          log_excerpt: 'ERROR: could not find libfoo.so\nstep 7/12 failed',
          source: 'pattern:missing_lib',
          original_reason: 'raw imagebuilder reason blob',
        }}
      />,
    );
    // The raw log excerpt is shown...
    expect(screen.getByText(/could not find libfoo\.so/)).toBeInTheDocument();
    // ...and there is a plain "Copy" (raw log) button AND the "Copy as issue" one.
    expect(screen.getByRole('button', { name: /^copy$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy as issue/i })).toBeInTheDocument();
  });
});

describe('BuildQueueRow per-node live-logs Copy button', () => {
  it('shows a Copy button once the Logs panel is opened and lines stream in', async () => {
    const entry: BuildQueueEntry = {
      taskId: 7, nodeName: 'demo', startedAt: Date.now(), outcome: 'success',
    } as BuildQueueEntry;
    await act(async () => {
      render(
        <BuildQueueRow
          entry={entry}
          accessToken="test-token"
          apiUrl="http://localhost:8000"
          onFinished={() => {}}
          onDismiss={() => {}}
        />,
      );
    });
    // Open the Logs panel — this triggers the fetch that populates logLines.
    await act(async () => {
      fireEvent.click(screen.getByText('Logs'));
    });
    // Let the fetch chain + state updates settle.
    await act(async () => { await new Promise((r) => setTimeout(r, 500)); });
    expect(screen.getByText('build step 2 OK')).toBeInTheDocument();
    // The Copy button renders in the same toolbar row as the log panel.
    expect(screen.getByRole('button', { name: /^copy$/i })).toBeInTheDocument();
  });
});

describe('ScanDetailsDialog scan-failure Copy button', () => {
  it('shows a Copy button next to the failure reason', () => {
    // accessToken="" makes the fetch effect early-return (no network).
    render(
      <ScanDetailsDialog
        open
        onClose={() => {}}
        node={{ id: 5, scan_status: 'failed', rejection_reason: 'trivy: failed to pull image demo:latest' } as any}
        accessToken=""
        onRescan={async () => {}}
        rescanning={false}
      />,
    );
    expect(screen.getByText(/failed to pull image demo:latest/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^copy$/i })).toBeInTheDocument();
  });
});
