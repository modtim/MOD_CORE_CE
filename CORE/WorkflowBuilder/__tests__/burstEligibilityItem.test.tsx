/**
 * burst-to-saas §3.12 — per-workflow burst eligibility toggle.
 *
 * The standing warning on this change is that the Gate 2 QA pass found eleven
 * defects of which nine were invisible to the suite, because the suite asserted
 * a row appeared and never that what appeared could be run or seen. So these
 * tests do not check that a button rendered. They check the two things that
 * would actually hurt:
 *
 *   - the control does not appear on an install with nowhere to spill, and
 *     does not appear when we cannot READ what its state is (fail closed)
 *   - a failed save does not leave a consent control reading "on". A toggle
 *     that says data may leave the premises when the server never agreed is
 *     the worst outcome this component has.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import BurstEligibilityItem from '@/components/BurstEligibilityItem';
import { authenticatedFetch } from '@/lib/api-client';

jest.mock('@/lib/api-client', () => ({
  __esModule: true,
  authenticatedFetch: jest.fn(),
}));

const mockFetch = authenticatedFetch as jest.MockedFunction<typeof authenticatedFetch>;

const jsonOk = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;

const API = 'http://api.test';

/** settings + workflow, in the order the component requests them. */
function mockLoad(opts: { connected: boolean; eligible?: boolean; enabledAt?: string | null }) {
  mockFetch.mockImplementation((url: string) => {
    if (String(url).includes('/burst/connect/settings')) {
      return Promise.resolve(jsonOk({ connected: opts.connected }));
    }
    if (String(url).includes('/road/')) {
      return Promise.resolve(
        jsonOk({
          id: 7,
          burst_eligible: opts.eligible ?? false,
          burst_enabled_at: opts.enabledAt ?? null,
        }),
      );
    }
    throw new Error(`unexpected url ${url}`);
  });
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe('BurstEligibilityItem', () => {
  it('renders nothing when the install is not paired with MOD', async () => {
    mockLoad({ connected: false });
    const { container } = render(<BurstEligibilityItem workflowId={7} apiUrl={API} />);

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/burst/i)).toBeNull();
  });

  it('fails closed: renders nothing when the settings read throws', async () => {
    mockFetch.mockRejectedValue(new Error('network down'));
    const { container } = render(<BurstEligibilityItem workflowId={7} apiUrl={API} />);

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the OFF state as never leaving the premises', async () => {
    mockLoad({ connected: true, eligible: false });
    render(<BurstEligibilityItem workflowId={7} apiUrl={API} />);

    expect(await screen.findByText(/off · never sent off-premises/)).toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows the ON state with the date it was permitted', async () => {
    mockLoad({ connected: true, eligible: true, enabledAt: '2026-08-12T10:00:00Z' });
    render(<BurstEligibilityItem workflowId={7} apiUrl={API} />);

    expect(await screen.findByText(/^on · enabled /)).toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'true');
  });

  it('PUTs the new value and takes the state from the server response', async () => {
    mockLoad({ connected: true, eligible: false });
    const onChanged = jest.fn();
    render(<BurstEligibilityItem workflowId={7} apiUrl={API} onChanged={onChanged} />);
    await screen.findByRole('button');

    mockFetch.mockResolvedValueOnce(
      jsonOk({ burst_eligible: true, enabled_at: '2026-08-12T10:00:00Z' }),
    );
    fireEvent.click(screen.getByRole('button'));

    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        `${API}/burst/connect/workflows/7/eligibility`,
        expect.objectContaining({ method: 'PUT', body: JSON.stringify({ eligible: true }) }),
      ),
    );
    expect(await screen.findByText(/^on · enabled /)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledWith(true);
  });

  it('reverts to OFF and says so when the save fails', async () => {
    mockLoad({ connected: true, eligible: false });
    const onChanged = jest.fn();
    render(<BurstEligibilityItem workflowId={7} apiUrl={API} onChanged={onChanged} />);
    await screen.findByRole('button');

    mockFetch.mockResolvedValueOnce({ ok: false, status: 500 } as unknown as Response);
    fireEvent.click(screen.getByRole('button'));

    expect(await screen.findByText(/Could not save\. Nothing changed\./)).toBeInTheDocument();
    // The claim that matters: it must not be left reading as permitted.
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'false');
    expect(onChanged).not.toHaveBeenCalled();
  });
});
