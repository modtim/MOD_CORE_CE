/**
 * FX.149 p3 — palette badge NAMING THE PROVIDER on external-AI nodes.
 *
 * Source of truth is the `external-ai` soft tag on the palette payload's
 * custom_metadata.tags (services/external_ai_node_gate.py EXTERNAL_AI_TAG,
 * seeded by migrations/20261006v_fx149_external_ai_node_tags.py — the FX.132
 * soft-tag pattern, no new column). These tests pin the badge contract:
 * an untagged payload renders NOTHING (a pre-FX.149 palette looks exactly as
 * before), a tagged payload renders one violet "External AI · <provider>"
 * chip, and the provider name is never invented — it comes from
 * custom_metadata.external_ai_provider when carried, else from node_kind for
 * the seeded nim_*_cloud kinds; otherwise the badge says only "External AI".
 *
 * Wording rule (docs/security-audit/external-ai-provider-inventory.md):
 * "designed to send tenant data off-box", no compliance claims, and the
 * tooltip never says the run is allowed — the run-start gate
 * (check_run_external_ai) decides that.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import {
    ExternalAiBadge,
    isExternalAiTagged,
    externalAiProvider,
    EXTERNAL_AI_TAG,
    EXTERNAL_AI_BADGE_STYLE,
} from '@/components/node-chips';

describe('isExternalAiTagged', () => {
    it('is true only for a tags array containing the external-ai tag', () => {
        expect(isExternalAiTagged({ custom_metadata: { tags: [EXTERNAL_AI_TAG] } })).toBe(true);
        expect(isExternalAiTagged({ custom_metadata: { tags: ['prohibited-use:gov', EXTERNAL_AI_TAG] } }))
            .toBe(true);
    });

    it('is false for absent / non-array / non-string / other tags', () => {
        [null, undefined, {}, { custom_metadata: null },
         { custom_metadata: { tags: null } },
         { custom_metadata: { tags: [] } },
         { custom_metadata: { tags: ['ai'] } },
         { custom_metadata: { tags: [null, 7] } }]
            .forEach((node) => expect(isExternalAiTagged(node)).toBe(false));
    });
});

describe('externalAiProvider', () => {
    it('prefers an explicit provider on the payload', () => {
        expect(externalAiProvider({ custom_metadata: { external_ai_provider: 'OpenAI' } }))
            .toBe('OpenAI');
        expect(externalAiProvider({ node_kind: 'nim_wan_cloud', custom_metadata: { external_ai_provider: '  Anthropic  ' } }))
            .toBe('Anthropic');
    });

    it('falls back to the seeded nim_*_cloud kinds (NVIDIA NIM)', () => {
        ['nim_trellis_cloud', 'nim_studiovoice_cloud', 'nim_wan_cloud'].forEach((kind) =>
            expect(externalAiProvider({ node_kind: kind, custom_metadata: {} })).toBe('NVIDIA NIM'));
    });

    it('never invents a provider for a *_local kind or an unknown kind', () => {
        [null, undefined, {},
         { node_kind: 'nim_studiovoice_local' },
         { node_kind: 'image_generation' },
         { node_kind: 'nim_cloudish' }]
            .forEach((node) => expect(externalAiProvider(node)).toBeNull());
    });
});

describe('ExternalAiBadge', () => {
    it('renders nothing for an untagged payload', () => {
        [null, undefined, {}, { custom_metadata: { tags: ['ai'] } }]
            .forEach((node) => {
                const { container } = render(<ExternalAiBadge node={node} />);
                expect(container.innerHTML).toBe('');
            });
    });

    it('renders one chip naming the provider when the payload can name it', () => {
        render(<ExternalAiBadge node={{ node_kind: 'nim_wan_cloud', custom_metadata: { tags: [EXTERNAL_AI_TAG] } }} />);
        const badge = screen.getByText('External AI · NVIDIA NIM');
        expect(badge).toBeInTheDocument();
        expect(badge.getAttribute('data-external-ai')).toBe('true');
        expect(badge.getAttribute('data-external-ai-provider')).toBe('NVIDIA NIM');
        expect(badge.className).toContain(EXTERNAL_AI_BADGE_STYLE);
    });

    it('names an explicit provider and appends an optional className', () => {
        const { container } = render(
            <ExternalAiBadge
                node={{ custom_metadata: { tags: [EXTERNAL_AI_TAG], external_ai_provider: 'OpenAI' } }}
                className="extra"
            />
        );
        const badge = container.querySelector('span') as any;
        expect(badge.textContent).toBe('External AI · OpenAI');
        expect(badge.className).toContain('extra');
        expect(badge.getAttribute('data-external-ai-provider')).toBe('OpenAI');
    });

    it('tagged but unnamed says only "External AI" and never claims a provider', () => {
        render(<ExternalAiBadge node={{ custom_metadata: { tags: [EXTERNAL_AI_TAG] } }} />);
        const badge = screen.getByText('External AI');
        expect(badge.getAttribute('data-external-ai-provider')).toBeNull();
        expect(badge.getAttribute('title')).toContain('designed to send tenant data to an external AI provider');
    });

    it('tooltip says "designed to" and never that the run is allowed', () => {
        render(<ExternalAiBadge node={{ custom_metadata: { tags: [EXTERNAL_AI_TAG], external_ai_provider: 'Anthropic' } }} />);
        const title = screen.getByText('External AI · Anthropic').getAttribute('title') || '';
        expect(title).toContain('designed to send tenant data to Anthropic');
        expect(title).not.toMatch(/allowed|approved|compliant|certified|GDPR|HIPAA/);
    });
});
