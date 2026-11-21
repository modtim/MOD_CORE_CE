/**
 * sz-2.10a — Frame Inspector panel (read-only view of a paused node's
 * debug frame; API contract in CORE/API/routers/workflows_debug.py).
 *
 * Pins: all sections render from a fixture frame; the API's literal "***"
 * becomes a muted "masked" pill (never the raw string, never unmasked);
 * 404 → "Frame not available"; 5xx → short safe error with NO token text
 * in the DOM; Refresh refetches (fetch called twice); Authorization header
 * sent; runId/nodeId URL-encoded; long values truncated + expandable.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import FrameInspectorPanel from '@/components/workflow/FrameInspectorPanel';

const noop = () => {};

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

const LONG_VALUE = 'x'.repeat(600);

const FIXTURE = {
  run_id: 77,
  node_id: 'gen_1',
  paused: true,
  node_kind: 'sdxl_turbo',
  node_definition_id: 42,
  config: { steps: 25, api_key: '***' },
  inputs: { prompt: 'a robot', seed: LONG_VALUE },
  upstream_outputs: [
    { node_id: 'load_1', output_url: 'http://dev-s3/load_1.png' },
  ],
  partial_output: { stage: 'denoise', step: 3 },
  worker_context: { worker: 'dell-01', vram_mb: 24576 },
};

describe('FrameInspectorPanel — sz-2.10a read-only frame view', () => {
  it('renders all sections from a fixture frame', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200, FIXTURE));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    await waitFor(() =>
      expect(screen.getByTestId('frame-inspector-panel')).toBeInTheDocument()
    );
    await waitFor(() =>
      expect(screen.getByTestId('frame-section-Config')).toBeInTheDocument()
    );

    expect(screen.getByTestId('frame-section-node')).toBeInTheDocument();
    expect(screen.getByTestId('frame-section-Inputs')).toBeInTheDocument();
    expect(screen.getByTestId('frame-section-upstream')).toBeInTheDocument();
    expect(screen.getByTestId('frame-section-Worker context')).toBeInTheDocument();
    expect(screen.getByTestId('frame-paused-badge')).toBeInTheDocument();
    // JSON partial_output pretty-prints in <pre>
    expect(document.querySelectorAll('pre').length).toBeGreaterThan(0);
    // loading state is gone once the frame lands
    expect(screen.queryByTestId('frame-loading')).toBeNull();
  });

  it('masked "***" renders the muted pill, never the raw string', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200, FIXTURE));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    await waitFor(() =>
      expect(screen.getByTestId('frame-value-api_key')).toBeInTheDocument()
    );
    const pill = screen.getByTestId('frame-value-api_key');
    expect(pill.textContent).toBe('masked');
    expect(document.body.textContent).not.toContain('***');
  });

  it('hot-swap textarea omits masked keys from the prefilled config', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200, FIXTURE));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    const textarea = await screen.findByTitle<HTMLTextAreaElement>('Config override (JSON)');
    await waitFor(() => expect(textarea.value).toContain('steps'));
    expect(textarea.value).not.toContain('api_key');
    expect(textarea.value).not.toContain('***');
    expect(screen.getByTestId('frame-hotswap-masked-hint')).toBeInTheDocument();
  });

  it('404 → "Frame not available" state', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(404, {}));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    await waitFor(() =>
      expect(screen.getByTestId('frame-missing')).toBeInTheDocument()
    );
    expect(screen.getByTestId('frame-missing').textContent).toContain(
      'Frame not available'
    );
    expect(screen.queryByTestId('frame-section-Config')).toBeNull();
  });

  it('500 → short safe error with no token text in the DOM', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(500, { detail: 'secret-body' }));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    await waitFor(() =>
      expect(screen.getByTestId('frame-error')).toBeInTheDocument()
    );
    const body = document.body.textContent;
    expect(body).toContain('HTTP 500');
    expect(body).not.toContain('tok123');
    expect(body).not.toContain('secret-body');
  });

  it('Refresh refetches — fetch called twice, Authorization header sent, URL-encoded', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200, FIXTURE));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="a b/c"
        onClose={noop}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/road/workflow-runs/77/debug/frame/a%20b%2Fc',
      expect.objectContaining({
        headers: { Authorization: 'Bearer tok123' },
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it('long value truncated at 500 chars, show-more expands it', async () => {
    const fetchMock = jest.fn().mockResolvedValue(makeResponse(200, FIXTURE));
    global.fetch = fetchMock as any;

    render(
      <FrameInspectorPanel
        apiUrl="http://api.test"
        accessToken="tok123"
        runId={77}
        nodeId="gen_1"
        onClose={noop}
      />
    );

    await waitFor(() =>
      expect(screen.getByTestId('frame-value-seed')).toBeInTheDocument()
    );
    const cell = screen.getByTestId('frame-value-seed');
    expect(cell.textContent).toContain('…');
    expect(cell.textContent.length).toBeLessThan(600);

    fireEvent.click(screen.getByRole('button', { name: /show more/i }));
    await waitFor(() =>
      expect(screen.getByTestId('frame-value-seed')).toBeInTheDocument()
    );
    expect(screen.getByTestId('frame-value-seed').textContent).toContain(
      LONG_VALUE
    );
  });
});
