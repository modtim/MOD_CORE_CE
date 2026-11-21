/**
 * sz-tcp.6 — unit tests for CostBasisBadge.
 *
 * Tests that each known cost-basis value renders the correct label and
 * Tailwind colour class, and that unknown / absent values produce no DOM.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import CostBasisBadge, { normalizeCostBasis } from '@/components/CostBasisBadge';

function renderBadge(props: Parameters<typeof CostBasisBadge>[0] = {}) {
  return render(<CostBasisBadge costBasis={undefined} {...props} />);
}

// ---------------------------------------------------------------------------
// normalizeCostBasis
// ---------------------------------------------------------------------------

describe('normalizeCostBasis', () => {
  it('returns null for undefined', () => {
    expect(normalizeCostBasis(undefined)).toBeNull();
  });

  it('returns null for null', () => {
    expect(normalizeCostBasis(null)).toBeNull();
  });

  it('returns null for numeric 0', () => {
    expect(normalizeCostBasis(0)).toBeNull();
  });

  it('returns null for a string that does not match any known basis', () => {
    expect(normalizeCostBasis('unknown')).toBeNull();
  });

  it('returns null for empty string', () => {
    expect(normalizeCostBasis('')).toBeNull();
  });

  it('normalizes "estimated" (case-insensitive)', () => {
    expect(normalizeCostBasis('ESTIMATED')).toBe('estimated');
    expect(normalizeCostBasis('Estimated')).toBe('estimated');
    expect(normalizeCostBasis('  estimated  ')).toBe('estimated');
  });

  it('normalizes "calibrated" (case-insensitive)', () => {
    expect(normalizeCostBasis('calibrated')).toBe('calibrated');
  });

  it('normalizes "manual" (case-insensitive)', () => {
    expect(normalizeCostBasis('MANUAL')).toBe('manual');
  });

  it('normalizes "free" (case-insensitive)', () => {
    expect(normalizeCostBasis('FREE')).toBe('free');
  });
});

// ---------------------------------------------------------------------------
// CostBasisBadge — rendering
// ---------------------------------------------------------------------------

describe('CostBasisBadge', () => {
  it('renders "Estimated" with amber classes for costBasis="estimated"', () => {
    renderBadge({ costBasis: 'estimated' });
    const el = screen.getByTestId('cost-basis-badge');
    expect(el).toHaveTextContent('Estimated');
    expect(el).toHaveAttribute('data-cost-basis', 'estimated');
    expect(el).toHaveClass('text-amber-700');
    expect(el).toHaveClass('bg-amber-50');
    expect(el).toHaveClass('border-amber-500');
  });

  it('renders "Calibrated" with emerald classes for costBasis="calibrated"', () => {
    renderBadge({ costBasis: 'calibrated' });
    const el = screen.getByTestId('cost-basis-badge');
    expect(el).toHaveTextContent('Calibrated');
    expect(el).toHaveAttribute('data-cost-basis', 'calibrated');
    expect(el).toHaveClass('text-emerald-700');
    expect(el).toHaveClass('bg-emerald-50');
    expect(el).toHaveClass('border-emerald-600');
  });

  it('renders "Manual" with blue classes for costBasis="manual"', () => {
    renderBadge({ costBasis: 'manual' });
    const el = screen.getByTestId('cost-basis-badge');
    expect(el).toHaveTextContent('Manual');
    expect(el).toHaveAttribute('data-cost-basis', 'manual');
    expect(el).toHaveClass('text-blue-700');
    expect(el).toHaveClass('bg-blue-50');
    expect(el).toHaveClass('border-blue-600');
  });

  it('renders "Free" with slate classes for costBasis="free"', () => {
    renderBadge({ costBasis: 'free' });
    const el = screen.getByTestId('cost-basis-badge');
    expect(el).toHaveTextContent('Free');
    expect(el).toHaveAttribute('data-cost-basis', 'free');
    expect(el).toHaveClass('text-slate-600');
    expect(el).toHaveClass('bg-slate-50');
    expect(el).toHaveClass('border-slate-300');
  });

  it('renders nothing for unknown cost_basis value', () => {
    const { container } = renderBadge({ costBasis: 'unknown' });
    expect(screen.queryByTestId('cost-basis-badge')).toBeNull();
    expect(container.children.length).toBe(0);
  });

  it('renders nothing when costBasis is undefined', () => {
    renderBadge({});
    expect(screen.queryByTestId('cost-basis-badge')).toBeNull();
  });

  it('renders nothing when costBasis is null', () => {
    renderBadge({ costBasis: null });
    expect(screen.queryByTestId('cost-basis-badge')).toBeNull();
  });

  it('forwards className prop onto the span', () => {
    renderBadge({ costBasis: 'free', className: 'ml-2' });
    expect(screen.getByTestId('cost-basis-badge')).toHaveClass('ml-2');
  });

  it('sets the title attribute for accessibility', () => {
    renderBadge({ costBasis: 'estimated' });
    expect(screen.getByTestId('cost-basis-badge')).toHaveAttribute('title', 'Credit cost basis: Estimated');
  });

  it('renders the correct title for each basis type', () => {
    const cases: Record<string, string> = {
      estimated: 'Estimated',
      calibrated: 'Calibrated',
      manual: 'Manual',
      free: 'Free',
    };
    for (const [basis, label] of Object.entries(cases)) {
      const { container } = renderBadge({ costBasis: basis });
      const el = container.querySelector('span[data-cost-basis]');
      expect(el).toHaveAttribute('title', `Credit cost basis: ${label}`);
    }
  });
});
