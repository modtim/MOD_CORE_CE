/**
 * GF.17a (launch-completion-megaplan) — StackMarketplaceTab.
 *
 * Covers:
 *  - list render: name, publisher fallback, price/Free.
 *  - GPU needs text surfaces on row expansion (lazy /catalog/stack/{id} fetch).
 *  - Owned badge (and Installed variant when installed_stack_id is present).
 *  - error state + retry.
 *  - button targets: Buy/Get -> /store/stack/{id}; Installed -> Dashboard deep link.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StackMarketplaceTab } from '@/components/stack-marketplace-tab';

const jsonOk = (body: unknown) =>
    ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }) as unknown as Response;

const LIST = [
    { kind: 'stack', id: 1, name: 'Video Render Farm', description: 'GPU render stack', price_cents: 0, price_tier: null, owned: false },
    { kind: 'stack', id: 2, name: 'ML Training Rig', description: 'Training stack', price_cents: 1999, price_tier: 'pro', owned: true },
    { kind: 'stack', id: 3, name: 'Installed Stack', description: 'Already installed', price_cents: 500, price_tier: null, owned: true, installed_stack_id: 42 },
];

const DETAIL_1 = { ...LIST[0], gpu_requirements: { count: 2, accelerator: 'CUDA', vram_gb: 48 }, license_class: 'commercial', licence: 'commercial', actions: ['get'] };
const DETAIL_2 = { ...LIST[1], gpu_requirements: null, license_class: 'open', licence: 'open', actions: ['owned'] };

afterEach(() => {
    jest.resetAllMocks();
});

function mockFetch() {
    return jest.fn((url: string) => {
        const u = String(url);
        if (u.includes('/catalog/stack/1')) return Promise.resolve(jsonOk(DETAIL_1));
        if (u.includes('/catalog/stack/2')) return Promise.resolve(jsonOk(DETAIL_2));
        if (u.includes('/catalog?kind=stack')) return Promise.resolve(jsonOk(LIST));
        return Promise.resolve(jsonOk({}));
    });
}

describe('StackMarketplaceTab (GF.17a)', () => {
    it('renders the list with name, price/Free, and Owned badge', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);

        await screen.findByText('Video Render Farm');
        expect(screen.getByText('ML Training Rig')).toBeInTheDocument();
        expect(screen.getByText('Free')).toBeInTheDocument();
        expect(screen.getByText('$19.99')).toBeInTheDocument();

        // Owned badge on the paid-but-owned row, none on the not-owned row.
        expect(screen.getByTestId('stack-badge-2').textContent).toBe('Owned');
        expect(screen.queryByTestId('stack-badge-1')).not.toBeInTheDocument();
    });

    it('shows GPU needs text and licence class after expanding a row', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);
        await screen.findByText('Video Render Farm');

        fireEvent.click(screen.getByTestId('stack-row-toggle-1'));
        await waitFor(() => expect(screen.getByTestId('stack-gpu-1')).toBeInTheDocument());
        expect(screen.getByTestId('stack-gpu-1').textContent).toBe('2× CUDA 48 GB');
        expect(screen.getByTestId('stack-licence-1').textContent).toBe('commercial');
    });

    it('shows Installed (not Owned) with a Persistent Stacks deep link when installed_stack_id is present', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);
        await screen.findByText('Installed Stack');

        expect(screen.getByTestId('stack-badge-3').textContent).toBe('Installed');

        fireEvent.click(screen.getByTestId('stack-row-toggle-3'));
        // No detail endpoint stubbed for id 3 -> falls back to {} but the
        // installed state already comes from the list row itself, so the
        // deep link renders without waiting on the detail fetch.
        await waitFor(() => expect(screen.getByTestId('stack-open-installed-3')).toBeInTheDocument());
        const href = screen.getByTestId('stack-open-installed-3').getAttribute('href') || '';
        expect(href).toContain('tab=stacks&stack=42');
        expect(href).toContain('#system');
    });

    it('links the Buy/Get action to the full store page for the id', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);
        await screen.findByText('Video Render Farm');

        fireEvent.click(screen.getByTestId('stack-row-toggle-1'));
        await waitFor(() => expect(screen.getByTestId('stack-open-full-1')).toBeInTheDocument());
        expect(screen.getByTestId('stack-open-full-1').getAttribute('href')).toBe('/store/stack/1');
        expect(screen.getByTestId('stack-open-full-1').textContent).toContain('Get');
    });

    it('shows an error state with retry on fetch failure', async () => {
        global.fetch = jest.fn(() => Promise.resolve({ ok: false, status: 500, json: async () => ({}), text: async () => '' } as unknown as Response));
        render(<StackMarketplaceTab accessToken="tok" />);

        await screen.findByTestId('stack-marketplace-error');
        expect(screen.getByTestId('stack-marketplace-error').textContent).toContain('Failed to load stacks');

        // Retry re-fires the fetch.
        global.fetch = mockFetch() as unknown as typeof fetch;
        fireEvent.click(screen.getByTestId('stack-marketplace-retry'));
        await screen.findByText('Video Render Farm');
    });

    it('shows an empty state when the catalog has no stack listings', async () => {
        global.fetch = jest.fn(() => Promise.resolve(jsonOk([]))) as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);
        await screen.findByTestId('stack-marketplace-empty');
    });

    it('filters the list by the search box', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);
        await screen.findByText('Video Render Farm');

        fireEvent.change(screen.getByTestId('stack-marketplace-search'), { target: { value: 'training' } });
        await waitFor(() => expect(screen.queryByText('Video Render Farm')).not.toBeInTheDocument());
        expect(screen.getByText('ML Training Rig')).toBeInTheDocument();
    });
});
