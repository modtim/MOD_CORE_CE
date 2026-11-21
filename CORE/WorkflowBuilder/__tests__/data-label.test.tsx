/**
 * GY.C7 — data-label markings: shared chip + CUI-style banner + colour map.
 *
 * Verifies the one shared component (DataLabelChip / ControlledBanner /
 * styleForLabel / worstLabel) that the builder uses on asset items, the
 * workflow header, and run rows: controlled labels render loudly with the
 * right colour, public/internal render subtly or not at all, custom labels
 * fail loud, and the banner uses the "CONTROLLED: <LABEL> — handle per
 * policy" convention.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import {
  DataLabelChip,
  ControlledBanner,
  styleForLabel,
  isControlledLabel,
  worstLabel,
} from '@/components/data-label';

describe('DataLabelChip', () => {
  it('renders a chip for CUI with the export colour', () => {
    render(<DataLabelChip label="CUI" subtleDefault={false} />);
    const chip = screen.getByTestId('data-label-chip');
    expect(chip).toHaveAttribute('data-label', 'CUI');
    expect(chip.className).toContain('border-red-700');
  });

  it('renders ITAR and EAR chips with escalating red tones', () => {
    const { rerender } = render(<DataLabelChip label="ITAR" subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip')).toHaveAttribute('data-label', 'ITAR');
    expect(screen.getByTestId('data-label-chip').className).toContain('border-red-900');
    rerender(<DataLabelChip label="EAR" subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip').className).toContain('border-red-800');
  });

  it('renders PII and PRE-RELEASE handled labels', () => {
    const { rerender } = render(<DataLabelChip label="PII" subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip')).toHaveAttribute('data-label', 'PII');
    rerender(<DataLabelChip label="PRE-RELEASE" subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip')).toHaveAttribute('data-label', 'PRE-RELEASE');
  });

  it('is case-insensitive for level labels', () => {
    render(<DataLabelChip label="cui" subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip')).toHaveAttribute('data-label', 'CUI');
  });

  it('renders unknown custom labels loudly (fail-loud, uppercased)', () => {
    render(<DataLabelChip label="client-proprietary" subtleDefault={false} />);
    const chip = screen.getByTestId('data-label-chip');
    expect(chip).toHaveAttribute('data-label', 'CLIENT-PROPRIETARY');
    expect(chip.className).toContain('border-slate-500');
  });

  it('renders public subtly when subtleDefault is false', () => {
    render(<DataLabelChip label="public" subtleDefault={false} />);
    const chip = screen.getByTestId('data-label-chip');
    expect(chip).toHaveAttribute('data-label', 'Public');
    expect(chip.className).toContain('text-neutral-400');
  });

  it('renders nothing for public when subtleDefault is true (default)', () => {
    const { container } = render(<DataLabelChip label="public" />);
    expect(container.innerHTML).toBe('');
  });

  it('treats null/absent as internal (subtle)', () => {
    const { container } = render(<DataLabelChip label={undefined} subtleDefault={false} />);
    expect(screen.getByTestId('data-label-chip')).toHaveAttribute('data-label', 'Internal');
    expect(container.innerHTML).toContain('Internal');
  });
});

describe('isControlledLabel / styleForLabel', () => {
  it('marks public/internal as non-controlled', () => {
    expect(isControlledLabel('public')).toBe(false);
    expect(isControlledLabel('internal')).toBe(false);
    expect(isControlledLabel(undefined)).toBe(false);
  });

  it('marks CUI/EAR/ITAR/confidential/restricted/PII/biometric as controlled', () => {
    for (const l of ['CUI', 'EAR', 'ITAR', 'confidential', 'restricted', 'PII', 'biometric', 'PRE-RELEASE', 'weird-custom']) {
      expect(isControlledLabel(l)).toBe(true);
    }
  });
});

describe('ControlledBanner', () => {
  it('shows the CUI marking convention for a controlled label', () => {
    render(<ControlledBanner label="CUI" />);
    const banner = screen.getByTestId('controlled-banner');
    expect(banner).toHaveAttribute('role', 'alert');
    expect(banner.textContent).toContain('CONTROLLED: CUI — handle per policy');
    expect(banner.className).toContain('bg-red-100');
  });

  it('renders nothing for non-controlled labels', () => {
    for (const l of ['internal', 'public', null, undefined]) {
      const { container } = render(<ControlledBanner label={l} />);
      expect(container.innerHTML).toBe('');
    }
  });

  it('uses the export tone for EAR/ITAR and amber for confidential', () => {
    const { rerender } = render(<ControlledBanner label="ITAR" />);
    expect(screen.getByTestId('controlled-banner').className).toContain('bg-red-100');
    rerender(<ControlledBanner label="confidential" />);
    expect(screen.getByTestId('controlled-banner').className).toContain('bg-amber-50');
  });
});

describe('worstLabel', () => {
  it('picks the worst controlled label across a set', () => {
    expect(worstLabel(['internal', 'CUI', 'EAR'])).toBe('EAR');
    expect(worstLabel(['public', 'ITAR'])).toBe('ITAR');
    expect(worstLabel(['confidential', 'CUI'])).toBe('CUI');
  });

  it('ignores non-controlled labels entirely', () => {
    expect(worstLabel(['public', 'internal', null, undefined])).toBeNull();
  });

  it('ranks custom labels as controlled', () => {
    expect(worstLabel(['weird-custom'])).toBe('weird-custom');
  });
});
