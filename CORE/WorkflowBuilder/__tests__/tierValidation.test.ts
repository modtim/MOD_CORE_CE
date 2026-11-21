/**
 * sz-tcp.10 — unit tests for the New Node wizard's tier-card VALIDATION.
 *
 * Exercises the state-machine around the pure helpers exported by
 * new-node-wizard.tsx — the wizard's `applyTier` wrapper flips the cost
 * basis to 'manual' and snapshots the untouched tier set as
 * `suggestedTiers` on the FIRST edit only, and `resetTierEdit` restores
 * that snapshot plus the basis that was in effect before the first edit.
 *
 * The all-tiers-required rule itself has no extractable pure function on
 * the frontend (it's three `<Input required>` rows on the tier card) and
 * is enforced server-side by `_validate_cost_tiers`
 * (CORE/API/services/node_definitions_node_wizard_service.py) — out of
 * scope for this file, which covers the client-side helpers only.
 */

import { applyTierEdit, resetTiers } from '@/components/new-node-wizard';

const SUGGESTED = { small: 1, medium: 2, large: 5 };

/** The wizard's `applyTier` wrapper, expressed over the pure helpers. */
function applyTier(state, tier, value) {
  const suggested = state.suggested === null ? state.tiers : state.suggested;
  const before = state.basis === 'manual' ? state.before : state.basis;
  return {
    tiers: applyTierEdit(state.tiers, tier, value),
    basis: 'manual',
    suggested,
    before,
  };
}

describe('tier-card basis bookkeeping', () => {
  it('flips the basis to manual on the first tier edit and snapshots the suggested set', () => {
    const start = { tiers: { ...SUGGESTED }, basis: 'estimated', suggested: null, before: null };
    const after = applyTier(start, 'medium', 9);
    expect(after.basis).toBe('manual');
    expect(after.before).toBe('estimated');
    expect(after.suggested).toEqual(SUGGESTED);
    expect(after.tiers).toEqual({ small: 1, medium: 9, large: 5 });
  });

  it('a second edit does not overwrite the snapshot or the remembered prior basis', () => {
    let state = { tiers: { ...SUGGESTED }, basis: null, suggested: null, before: null };
    state = applyTier(state, 'small', 0);
    expect(state.suggested).toEqual(SUGGESTED);
    expect(state.before).toBeNull();
    state = applyTier(state, 'large', 42);
    expect(state.suggested).toEqual(SUGGESTED);
    expect(state.before).toBeNull();
    expect(state.tiers).toEqual({ small: 0, medium: 2, large: 42 });
  });

  it('reset restores the suggested tiers and the pre-edit basis', () => {
    const edited = applyTier(
      { tiers: { ...SUGGESTED }, basis: 'calibrated', suggested: null, before: null },
      'medium',
      9,
    );
    const restored = resetTiers(edited.tiers, edited.suggested, edited.before);
    expect(restored.tiers).toEqual(SUGGESTED);
    expect(restored.basis).toBe('calibrated');
  });

  it('resetting twice is idempotent (the wizard clears the snapshot after the first reset)', () => {
    const edited = applyTier({ tiers: { ...SUGGESTED }, basis: null, suggested: null, before: null }, 'small', 7);
    const first = resetTiers(edited.tiers, edited.suggested, edited.before);
    // resetTierEdit then nulls suggestedTiers/basisBeforeEdit:
    const second = resetTiers(first.tiers, null, null);
    expect(second.tiers).toEqual(SUGGESTED);
    expect(second.basis).toBeNull();
  });
});
