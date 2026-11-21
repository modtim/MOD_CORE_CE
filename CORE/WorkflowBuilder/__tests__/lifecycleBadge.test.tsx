/**
 * FX.103 p4 — deprecation / retirement lifecycle badge (LifecycleBadge).
 *
 * FX.103 (owner decision Q89=A) gives every NodeDefinition a lifecycle
 * `status` ('active' | 'deprecated' | 'retired') plus `deprecated_at` /
 * `retire_after`; the palette API (GET /v1/nodes) also returns the part-2
 * aliases `lifecycle_state` + `retire_at`. These tests pin the badge
 * component's contract: active/absent renders nothing, deprecated is amber
 * with a "Retires on <date>" tooltip when a date is carried, retired is grey.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import {
    LifecycleBadge,
    paletteLifecycleState,
    lifecycleDateText,
    LIFECYCLE_BADGE_STYLES,
} from '@/components/node-chips';

describe('paletteLifecycleState', () => {
    it('prefers the part-2 alias and falls back to the part-1 status', () => {
        expect(paletteLifecycleState({ lifecycle_state: 'deprecated' })).toBe('deprecated');
        expect(paletteLifecycleState({ status: 'retired' })).toBe('retired');
        expect(paletteLifecycleState({ lifecycle_state: 'retired', status: 'deprecated' }))
            .toBe('retired');
    });

    it('collapses unknown / missing / run-state values to active', () => {
        [null, undefined, {}, 'active', 'RUNNING', 'success', 'blocked', '']
            .forEach((v) => expect(paletteLifecycleState(v ?? {})).toBe('active'));
        expect(paletteLifecycleState({ status: 'success' })).toBe('active');
    });

    it('is case-insensitive', () => {
        expect(paletteLifecycleState({ lifecycle_state: 'DEPRECATED' })).toBe('deprecated');
    });
});

describe('lifecycleDateText', () => {
    it('reduces an ISO-8601 datetime to YYYY-MM-DD deterministically', () => {
        expect(lifecycleDateText('2027-01-01T00:00:00Z')).toBe('2027-01-01');
        expect(lifecycleDateText('2027-01-01T12:34:56.500+02:00')).toBe('2027-01-01');
        expect(lifecycleDateText('2027-01-01')).toBe('2027-01-01');
    });

    it('returns null for absent / unparseable values', () => {
        expect(lifecycleDateText(null)).toBeNull();
        expect(lifecycleDateText(undefined)).toBeNull();
        expect(lifecycleDateText('')).toBeNull();
        expect(lifecycleDateText('not a date')).toBeNull();
    });
});

describe('LifecycleBadge', () => {
    it('renders nothing for active / absent lifecycles', () => {
        [null, undefined, 'active', 'weird'].forEach((state) => {
            const { container } = render(
                <LifecycleBadge lifecycleState={state} retireAt={'2027-01-01T00:00:00Z'} />,
            );
            expect(container.innerHTML).toBe('');
        });
    });

    it('renders an amber Deprecated badge with the retirement date tooltip', () => {
        render(<LifecycleBadge lifecycleState="deprecated" retireAt="2027-01-01T00:00:00Z" />);
        const badge = screen.getByText('Deprecated');
        expect(badge.getAttribute('title')).toBe('Retires on 2027-01-01');
        expect(badge.getAttribute('data-lifecycle-state')).toBe('deprecated');
        expect(badge.className).toContain(LIFECYCLE_BADGE_STYLES.deprecated);
    });

    it('says the date is unset when no retire_at is carried', () => {
        render(<LifecycleBadge lifecycleState="deprecated" />);
        const badge = screen.getByText('Deprecated');
        expect(badge.getAttribute('title')).toBe('Deprecated — still usable; retirement date not set');
    });

    it('renders a grey Retired badge that says it cannot run', () => {
        render(<LifecycleBadge lifecycleState="retired" retireAt="2026-11-02T00:00:00Z" />);
        const badge = screen.getByText('Retired');
        expect(badge.getAttribute('title')).toBe('Retired on 2026-11-02 — cannot run');
        expect(badge.getAttribute('data-lifecycle-state')).toBe('retired');
        expect(badge.className).toContain(LIFECYCLE_BADGE_STYLES.retired);
    });

    it('appends an optional className (palette/canvas variants share one component)', () => {
        render(<LifecycleBadge lifecycleState="deprecated" className="mx-1" />);
        expect(screen.getByText('Deprecated').className).toContain('mx-1');
    });
});
