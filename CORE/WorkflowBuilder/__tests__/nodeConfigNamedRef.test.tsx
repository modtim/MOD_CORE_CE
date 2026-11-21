/**
 * Tests for the in-node "pick a named RAG / LoRA" control in the node config
 * panel's VariableControl.
 *
 * A node's extra_variable declares itself as:
 *     { type: "named_ref", ref_type: "rag" | "lora", description: "..." }
 * The control fetches GET {API}/named-artifacts?type=<ref_type> (tenant-scoped
 * via the bearer token) and offers the returned `name`s in a searchable dropdown
 * with a refresh + a free-text "type a name" fallback. The chosen/typed NAME is
 * stored as the value. If the registry endpoint 404s / fails, it degrades to a
 * plain text input so the node still works.
 */

import React, { useState } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VariableControl } from '@/components/node-config-panel';

// Radix Popover + cmdk (used by the dropdown) reach for layout / pointer APIs
// that jsdom doesn't implement.
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

afterEach(() => {
  jest.restoreAllMocks();
  delete (global as any).fetch;
});

// Controlled harness mirroring the panel's extraVariables flow: onChange commits
// the value up, which flows back down as `value` (like updateNodeData).
function Harness({
  config,
  initial,
  onCommit,
  accessToken = 'tok',
}: {
  config: any;
  initial?: any;
  onCommit?: (v: any) => void;
  accessToken?: string | null;
}) {
  const [val, setVal] = useState<any>(initial);
  return (
    <div>
      <VariableControl
        name="RAG_NAME"
        config={config}
        value={val}
        onChange={(v: any) => {
          setVal(v);
          onCommit?.(v);
        }}
        accessToken={accessToken}
      />
      <span data-testid="committed">{String(val)}</span>
    </div>
  );
}

const ragConfig = { type: 'named_ref', ref_type: 'rag', description: 'Which saved RAG index to query' };
const loraConfig = { type: 'named_ref', ref_type: 'lora', description: 'Which brand LoRA to apply' };

function mockFetchOk(rows: any[]) {
  (global as any).fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => rows,
  });
}

describe('VariableControl named_ref control', () => {
  it('fetches the tenant artifact list for the declared ref_type', async () => {
    mockFetchOk([{ name: 'brand-voice' }]);
    render(<Harness config={ragConfig} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());
    const url = (global as any).fetch.mock.calls[0][0] as string;
    expect(url).toContain('/named-artifacts?type=rag');
    // Tenant scope: goes out with the bearer token.
    const opts = (global as any).fetch.mock.calls[0][1];
    expect(opts.headers.Authorization).toBe('Bearer tok');
  });

  it('uses the lora ref_type in the query string', async () => {
    mockFetchOk([{ name: 'acme-brand' }]);
    render(<Harness config={loraConfig} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());
    expect((global as any).fetch.mock.calls[0][0]).toContain('/named-artifacts?type=lora');
  });

  it('renders a dropdown of the returned names and stores the picked name as the value', async () => {
    const user = userEvent.setup();
    mockFetchOk([{ name: 'brand-voice' }, { name: 'legal-corpus' }]);
    const onCommit = jest.fn();
    render(<Harness config={ragConfig} onCommit={onCommit} />);

    // Wait until the fetch settled (status: loaded).
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());

    const trigger = screen.getByRole('combobox');
    await user.click(trigger);

    // Names from the mocked list appear as options.
    const option = await screen.findByText('legal-corpus');
    await user.click(option);

    expect(onCommit).toHaveBeenLastCalledWith('legal-corpus');
    expect(screen.getByTestId('committed')).toHaveTextContent('legal-corpus');
  });

  it('lets you type a name that is not in the list (free-text fallback) and stores it', async () => {
    const user = userEvent.setup();
    mockFetchOk([{ name: 'brand-voice' }]);
    const onCommit = jest.fn();
    render(<Harness config={ragConfig} onCommit={onCommit} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());

    await user.click(screen.getByRole('combobox'));
    const search = await screen.findByPlaceholderText(/Search or type a RAG name/i);
    await user.type(search, 'not-built-yet');

    const useRow = await screen.findByText(/Use "not-built-yet"/i);
    await user.click(useRow);

    expect(onCommit).toHaveBeenLastCalledWith('not-built-yet');
  });

  it('falls back to a plain text input when the endpoint 404s', async () => {
    (global as any).fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    });
    const user = userEvent.setup();
    const onCommit = jest.fn();
    render(<Harness config={ragConfig} onCommit={onCommit} />);

    // No combobox; a text input takes over so the node still works.
    const input = await screen.findByRole('textbox');
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    await user.type(input, 'manual-rag');
    expect(onCommit).toHaveBeenLastCalledWith('manual-rag');
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
  }, 20000);

  it('falls back to a plain text input when the fetch rejects (network error)', async () => {
    (global as any).fetch = jest.fn().mockRejectedValue(new Error('network down'));
    render(<Harness config={ragConfig} />);
    const input = await screen.findByRole('textbox');
    expect(input).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('shows the empty-state hint when the tenant has no saved artifacts', async () => {
    const user = userEvent.setup();
    mockFetchOk([]);
    render(<Harness config={ragConfig} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());
    await user.click(screen.getByRole('combobox'));
    expect(await screen.findByText(/No saved RAGs yet, or type a name/i)).toBeInTheDocument();
  });

  it('re-fetches when the refresh button is clicked', async () => {
    const user = userEvent.setup();
    mockFetchOk([{ name: 'brand-voice' }]);
    render(<Harness config={ragConfig} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: /Refresh saved RAGs/i }));
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalledTimes(2));
  });

  it('shows the currently-stored name on the trigger', async () => {
    mockFetchOk([{ name: 'brand-voice' }]);
    render(<Harness config={ragConfig} initial={'brand-voice'} />);
    await waitFor(() => expect((global as any).fetch).toHaveBeenCalled());
    expect(screen.getByRole('combobox')).toHaveTextContent('brand-voice');
  });
});
