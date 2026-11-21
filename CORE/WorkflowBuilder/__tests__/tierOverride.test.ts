/**
 * sz-tcp.7 — unit tests for the New Node wizard's tier-override helpers.
 *
 * `applyTierEdit` is the pure core behind the wizard's `applyTier` state
 * writer (one Small/Medium/Large input edited -> that tier changes, the
 * other two stay). `resetTiers` is the pure core behind the "Reset" link on
 * the tier card (restore the machine's suggested tiers + the basis that was
 * in effect before the author's first edit).
 */

import { applyTierEdit, resetTiers } from '@/components/new-node-wizard';

const SUGGESTED = { small: 1, medium: 2, large: 5 };

describe('applyTierEdit', () => {
  it('changes only the edited tier and leaves the other two untouched', () => {
    const edited = applyTierEdit(SUGGESTED, 'medium', 9);
    expect(edited).toEqual({ small: 1, medium: 9, large: 5 });
  });

  it('keeps the untouched tiers when the author edits Small and Large', () => {
    const smallFirst = applyTierEdit(SUGGESTED, 'small', 0);
    expect(smallFirst).toEqual({ small: 0, medium: 2, large: 5 });
    const thenLarge = applyTierEdit(smallFirst, 'large', 42);
    expect(thenLarge).toEqual({ small: 0, medium: 2, large: 42 });
  });

  it('is pure: the previous tier set is not mutated', () => {
    const prev = { small: 3, medium: 6, large: 15 };
    const edited = applyTierEdit(prev, 'small', 4);
    expect(prev).toEqual({ small: 3, medium: 6, large: 15 });
    expect(edited).toEqual({ small: 4, medium: 6, large: 15 });
  });
});

describe('resetTiers', () => {
  it('restores the suggested tiers verbatim after an override', () => {
    const edited = applyTierEdit(SUGGESTED, 'medium', 9);
    const restored = resetTiers(edited, SUGGESTED, 'estimated');
    expect(restored.tiers).toEqual(SUGGESTED);
    expect(restored.basis).toBe('estimated');
  });

  it('restores the previous basis on a fresh node (no prior basis)', () => {
    const restored = resetTiers({ small: 1, medium: 9, large: 5 }, SUGGESTED, null);
    expect(restored.tiers).toEqual(SUGGESTED);
    expect(restored.basis).toBeNull();
  });

  it('is a no-op when the wizard never recorded a suggestion', () => {
    const current = { small: 2, medium: 4, large: 8 };
    const restored = resetTiers(current, null, 'calibrated');
    expect(restored.tiers).toEqual(current);
    expect(restored.basis).toBe('calibrated');
  });
});
