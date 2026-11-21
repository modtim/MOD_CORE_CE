/**
 * Bucket D — WorkflowProgress ETA badges.
 *
 * Verifies the per-node ETA renders pre-completion (predicted) AND
 * post-completion (actual + delta vs predicted), plus the workflow-level
 * ETA-source label ("(ML)" vs "(from progress)"). Mocks fetch to feed
 * deterministic /progress + /node-status responses.
 */

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { WorkflowProgress } from '@/components/workflow-progress';

interface NodeStatusFixture {
  node_id: string;
  node_name: string;
  node_kind: string;
  status: string;
  total_cost: number;
  files_processed: number;
  errors: any[];
  estimated_seconds?: number | null;
  actual_seconds?: number | null;
  node_run_status?: string | null;
}

interface ProgressFixture {
  workflow_run_id: number;
  status: string;
  progress_percentage: number;
  completed_nodes: number;
  total_nodes: number;
  current_node: string | null;
  total_cost: number;
  started_at: string;
  estimated_seconds_remaining: number | null;
  estimated_total_seconds?: number | null;
  eta_source?: string | null;
}

function mockEndpoints(progress: ProgressFixture, nodes: NodeStatusFixture[]) {
  (global as any).fetch = jest.fn((url: string) => {
    const body = url.includes('/node-status')
      ? { workflow_run_id: progress.workflow_run_id, overall_status: progress.status, nodes, total_files: 0 }
      : progress;
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: () => Promise.resolve(body),
    });
  });
}

async function renderAndSettle(progress: ProgressFixture, nodes: NodeStatusFixture[]) {
  mockEndpoints(progress, nodes);
  await act(async () => {
    render(
      <WorkflowProgress workflowRunId={42} accessToken="test-token" />,
    );
  });
  await waitFor(() => {
    expect((global as any).fetch).toHaveBeenCalled();
  });
}

afterEach(() => {
  jest.resetAllMocks();
});

test('renders predicted ETA badge for a queued node', async () => {
  await renderAndSettle(
    {
      workflow_run_id: 42, status: 'running',
      progress_percentage: 25, completed_nodes: 1, total_nodes: 4,
      current_node: 'n2', total_cost: 1, started_at: new Date().toISOString(),
      estimated_seconds_remaining: 90, estimated_total_seconds: 120, eta_source: 'predicted',
    },
    [
      { node_id: 'n1', node_name: 'Decode', node_kind: 'decode-dag', status: 'success',
        total_cost: 1, files_processed: 1, errors: [],
        estimated_seconds: 30, actual_seconds: 28, node_run_status: 'completed' },
      { node_id: 'n2', node_name: 'Process', node_kind: 'process-dag', status: 'queued',
        total_cost: 0, files_processed: 0, errors: [],
        estimated_seconds: 60, actual_seconds: null, node_run_status: 'queued' },
    ],
  );

  // Pre-completion node shows the ETA label.
  await waitFor(() => {
    expect(screen.getByText('Process')).toBeInTheDocument();
  });
  // The pending row should show "ETA" (its actual is null).
  expect(screen.getAllByText('ETA').length).toBeGreaterThan(0);
});

test('renders actual + delta on completed nodes with sign coloring', async () => {
  await renderAndSettle(
    {
      workflow_run_id: 42, status: 'success',
      progress_percentage: 100, completed_nodes: 2, total_nodes: 2,
      current_node: null, total_cost: 5, started_at: new Date().toISOString(),
      estimated_seconds_remaining: 0, estimated_total_seconds: 90, eta_source: 'predicted',
    },
    [
      { node_id: 'n1', node_name: 'Faster', node_kind: 'a', status: 'success',
        total_cost: 1, files_processed: 1, errors: [],
        estimated_seconds: 60, actual_seconds: 45, node_run_status: 'completed' },
      { node_id: 'n2', node_name: 'Slower', node_kind: 'b', status: 'success',
        total_cost: 1, files_processed: 1, errors: [],
        estimated_seconds: 30, actual_seconds: 60, node_run_status: 'completed' },
    ],
  );

  await waitFor(() => {
    expect(screen.getByText('Faster')).toBeInTheDocument();
    expect(screen.getByText('Slower')).toBeInTheDocument();
  });

  // "Actual / Est." header rendered for completed rows.
  expect(screen.getAllByText(/Actual \/ Est\./).length).toBeGreaterThan(0);

  // Faster: -15s delta → green.
  expect(screen.getByText(/-15s vs/)).toBeInTheDocument();
  // Slower: +30s delta (>20% slower than estimate) → amber.
  expect(screen.getByText(/\+30s vs/)).toBeInTheDocument();
});

test('renders the workflow-level ML label when eta_source=predicted', async () => {
  await renderAndSettle(
    {
      workflow_run_id: 42, status: 'running',
      progress_percentage: 50, completed_nodes: 1, total_nodes: 2,
      current_node: 'n2', total_cost: 1, started_at: new Date().toISOString(),
      estimated_seconds_remaining: 60, estimated_total_seconds: 120, eta_source: 'predicted',
    },
    [],
  );

  await waitFor(() => {
    expect(screen.getByText(/Est\. Time Remaining/)).toBeInTheDocument();
  });
  expect(screen.getByText(/\(ML\)/)).toBeInTheDocument();
});

test('renders the from-progress label when eta_source=elapsed_extrapolation', async () => {
  await renderAndSettle(
    {
      workflow_run_id: 42, status: 'running',
      progress_percentage: 50, completed_nodes: 1, total_nodes: 2,
      current_node: 'n2', total_cost: 1, started_at: new Date().toISOString(),
      estimated_seconds_remaining: 60, estimated_total_seconds: null, eta_source: 'elapsed_extrapolation',
    },
    [],
  );

  await waitFor(() => {
    expect(screen.getByText(/Est\. Time Remaining/)).toBeInTheDocument();
  });
  expect(screen.getByText(/\(from progress\)/)).toBeInTheDocument();
});

test('hides per-node ETA when both estimated and actual are null', async () => {
  await renderAndSettle(
    {
      workflow_run_id: 42, status: 'running',
      progress_percentage: 0, completed_nodes: 0, total_nodes: 1,
      current_node: null, total_cost: 0, started_at: new Date().toISOString(),
      estimated_seconds_remaining: null,
    },
    [
      { node_id: 'n1', node_name: 'Legacy', node_kind: 'l', status: 'queued',
        total_cost: 0, files_processed: 0, errors: [],
        estimated_seconds: null, actual_seconds: null, node_run_status: 'queued' },
    ],
  );

  await waitFor(() => {
    expect(screen.getByText('Legacy')).toBeInTheDocument();
  });
  // Neither "ETA" nor "Actual / Est." labels should be present in this row.
  expect(screen.queryByText('ETA')).not.toBeInTheDocument();
  expect(screen.queryByText(/Actual \/ Est\./)).not.toBeInTheDocument();
});
