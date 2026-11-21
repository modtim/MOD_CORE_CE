/**
 * sz-tcp.9 — unit tests for formatTierHint, the New Node wizard's tier-
 * threshold hint copy under the Small / Medium / Large credit card.
 *
 * The wizard fetches GET /platform/credit-rate once on mount and feeds the
 * `credit_tier_thresholds` dict ({small_max_bytes, medium_max_bytes}) into
 * this pure formatter. Golden path: real thresholds render nice MB copy.
 * Failure modes: null / missing / zero / garbage values must fall back to
 * the 2 / 30 MB defaults silently (the wizard shows no error UI).
 */

import { formatTierHint } from '@/components/new-node-wizard';

const MB = 1024 * 1024;

describe('formatTierHint', () => {
  it('renders the documented defaults for the default thresholds', () => {
    const hints = formatTierHint({ small_max_bytes: 2 * MB, medium_max_bytes: 30 * MB });
    expect(hints.small).toBe('inputs ≤ 2 MB');
    expect(hints.medium).toBe('inputs ≤ 30 MB');
    expect(hints.large).toBe('inputs > 30 MB');
  });

  it('falls back to the 2/30 MB defaults when the fetch failed (null payload)', () => {
    const hints = formatTierHint(null);
    expect(hints.small).toBe('inputs ≤ 2 MB');
    expect(hints.medium).toBe('inputs ≤ 30 MB');
    expect(hints.large).toBe('inputs > 30 MB');
  });

  it('falls back when the payload is missing the fields', () => {
    const hints = formatTierHint({});
    expect(hints.small).toBe('inputs ≤ 2 MB');
    expect(hints.medium).toBe('inputs ≤ 30 MB');
    expect(hints.large).toBe('inputs > 30 MB');
  });

  it('falls back on zero / negative / non-numeric values', () => {
    const hints = formatTierHint({ small_max_bytes: 0, medium_max_bytes: -5 });
    expect(hints.small).toBe('inputs ≤ 2 MB');
    expect(hints.medium).toBe('inputs ≤ 30 MB');
    expect(hints.large).toBe('inputs > 30 MB');
    const garbage = formatTierHint({ small_max_bytes: 'abc', medium_max_bytes: null });
    expect(garbage.small).toBe('inputs ≤ 2 MB');
    expect(garbage.medium).toBe('inputs ≤ 30 MB');
    expect(garbage.large).toBe('inputs > 30 MB');
  });

  it('accepts numeric strings from the JSONB payload', () => {
    const hints = formatTierHint({ small_max_bytes: '1048576', medium_max_bytes: '5242880' });
    expect(hints.small).toBe('inputs ≤ 1 MB');
    expect(hints.medium).toBe('inputs ≤ 5 MB');
    expect(hints.large).toBe('inputs > 5 MB');
  });

  it('formats sub-MB thresholds as KB and keeps medium >= small', () => {
    const hints = formatTierHint({ small_max_bytes: 512 * 1024, medium_max_bytes: 1024 * 1024 });
    expect(hints.small).toBe('inputs ≤ 512 KB');
    expect(hints.medium).toBe('inputs ≤ 1 MB');
    expect(hints.large).toBe('inputs > 1 MB');
  });

  it('clamps a medium ceiling below the small one (never contradicts itself)', () => {
    const hints = formatTierHint({ small_max_bytes: 40 * MB, medium_max_bytes: 10 * MB });
    expect(hints.small).toBe('inputs ≤ 40 MB');
    expect(hints.medium).toBe('inputs ≤ 40 MB');
    expect(hints.large).toBe('inputs > 40 MB');
  });
});
