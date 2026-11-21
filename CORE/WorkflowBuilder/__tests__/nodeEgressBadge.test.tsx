/**
 * FX.91 p2b [U] — declared outbound network + approval state in the palette.
 *
 * EgressStateBadge (components/egress-badge.tsx, the DECLARATION half — the
 * existing EgressBadge in that file is the per-task RUNTIME report and is
 * untouched) renders from the palette payload's `egress_domains` (p2a) and
 * `egress_approval` (p2c state, batched server-side by
 * services/node_egress_approval.palette_egress_states). These tests pin the
 * four states the task specifies, plus the honest silences: a node whose
 * declaration the badge cannot see renders NOTHING (it never claims "No
 * network"), and an absent approval state never claims a decision.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { EgressStateBadge, EGRESS_BADGE_STYLES } from '@/components/egress-badge';

const NO_NETWORK = { id: 1, name: 'Local Cog', egress_domains: [] };
const APPROVED = {
    id: 2, name: 'HF Cog', egress_domains: ['huggingface.co', 'cdn.example.com'],
    egress_approval: {
        state: 'approved',
        declared: ['huggingface.co', 'cdn.example.com'],
        approved: ['cdn.example.com', 'huggingface.co'],
    },
};
const PENDING = {
    id: 3, name: 'Pending Cog', egress_domains: ['huggingface.co'],
    egress_approval: { state: 'required', declared: ['huggingface.co'], approved: [] },
};
const STALE = {
    id: 4, name: 'Stale Cog', egress_domains: ['a.example.com', 'evil.example.com'],
    egress_approval: {
        state: 'stale',
        declared: ['a.example.com', 'evil.example.com'],
        approved: ['a.example.com'],
    },
};
const GOV_BLOCKED = {
    id: 5, name: 'Gov Cog', egress_domains: ['huggingface.co'],
    egress_approval: {
        state: 'approved',
        declared: ['huggingface.co'],
        approved: ['huggingface.co'],
        gov_blocked: true,
        gov_denied: ['huggingface.co'],
    },
};
const NO_DECLARATION = { id: 6, name: 'Pre-p2a Cog' };

describe('FX.91 p2b palette egress badge', () => {
    it('renders nothing for a node whose declaration it cannot see', () => {
        const { container } = render(<EgressStateBadge node={NO_DECLARATION} />);
        expect(container.innerHTML).toBe('');
    });

    it('badges a default-deny node "No network"', () => {
        const { container } = render(<EgressStateBadge node={NO_NETWORK} />);
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.textContent).toBe('No network');
        expect(badge.getAttribute('data-egress-state')).toBe('none');
        EGRESS_BADGE_STYLES.neutral.split(' ').forEach((c) => expect(badge.className).toContain(c));
    });

    it('badges an approved declaration neutrally and lists the domains in the tooltip', () => {
        const { container } = render(<EgressStateBadge node={APPROVED} />);
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.textContent).toBe('Network: 2 domains');
        expect(badge.getAttribute('data-egress-state')).toBe('approved');
        expect(badge.getAttribute('data-egress-domains')).toBe('huggingface.co, cdn.example.com');
        expect(badge.getAttribute('title')).toContain('Declares 2 external host(s)');
    });

    it('a single domain reads "Network: 1 domain"', () => {
        const { container } = render(
            <EgressStateBadge
                node={{
                    ...APPROVED,
                    egress_domains: ['huggingface.co'],
                    egress_approval: { ...APPROVED.egress_approval, declared: ['huggingface.co'], approved: ['huggingface.co'] },
                }}
            />,
        );
        expect((container.firstElementChild as HTMLElement).textContent).toBe('Network: 1 domain');
    });

    it('a node awaiting a decision is amber', () => {
        const { container } = render(<EgressStateBadge node={PENDING} />);
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.getAttribute('data-egress-state')).toBe('required');
        EGRESS_BADGE_STYLES.pending.split(' ').forEach((c) => expect(badge.className).toContain(c));
        expect(badge.getAttribute('title')).toContain('Needs approval');
    });

    it('a changed declaration after approval is amber too (stale)', () => {
        const { container } = render(<EgressStateBadge node={STALE} />);
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.getAttribute('data-egress-state')).toBe('stale');
        EGRESS_BADGE_STYLES.pending.split(' ').forEach((c) => expect(badge.className).toContain(c));
        expect(badge.getAttribute('title')).toContain('Approval is stale');
    });

    it('the gov profile turns it red and says so, even when the tenant approved', () => {
        const { container } = render(<EgressStateBadge node={GOV_BLOCKED} />);
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.getAttribute('data-egress-state')).toBe('gov_blocked');
        EGRESS_BADGE_STYLES.blocked.split(' ').forEach((c) => expect(badge.className).toContain(c));
        expect(badge.getAttribute('title')).toContain('Blocked by government profile');
    });

    it('an absent approval state states the declaration only, claiming no decision', () => {
        const { container } = render(
            <EgressStateBadge node={{ id: 7, egress_domains: ['huggingface.co'] }} />,
        );
        const badge = container.firstElementChild as HTMLElement;
        expect(badge.textContent).toBe('Network: 1 domain');
        expect(badge.getAttribute('data-egress-state')).toBe('unknown');
        expect(badge.getAttribute('title')).toBe('Declares 1 external host(s): huggingface.co');
    });

    it('className passthrough works for the palette row spacing', () => {
        const { container } = render(<EgressStateBadge node={PENDING} className="mx-1" />);
        expect((container.firstElementChild as HTMLElement).className).toContain('mx-1');
    });
});
