/**
 * tcp.4.5 (R5) — tests for the New Node wizard's tier-cost edit / reset /
 * required-fields flow (sz-tcp.5/7).
 *
 * Mounting the full 7500-line wizard is too heavy (see wizardClusterCapabilities),
 * so — like tierOverride/tierValidation — we drive the wizard's internal
 * `applyTier` / `resetTierEdit` state wrappers over the EXPORTED pure helpers
 * (applyTierEdit / resetTiers), and pin the UI wiring (reset CTA gate, the
 * tier-input onChange coercion, the required inputs, the POST-body fields)
 * against the wizard source.
 */

import * as fs from 'fs';
import * as path from 'path';
import { applyTierEdit, resetTiers } from '@/components/new-node-wizard';

const WIZARD_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/new-node-wizard.tsx'),
  'utf-8',
);

type TierSet = { small: number; medium: number; large: number };
const SUGGESTED: TierSet = { small: 1, medium: 2, large: 5 };

/** Mirror of the wizard's applyTier wrapper (new-node-wizard.tsx ~1040-1048). */
type WizardTierState = { tiers: TierSet; basis: string | null; suggested: TierSet | null; before: string | null };
const wizardApplyTier = (state: WizardTierState, tier: 'small' | 'medium' | 'large', value: number): WizardTierState => ({
  tiers: applyTierEdit(state.tiers, tier, value),
  basis: 'manual',
  suggested: state.suggested ?? state.tiers,
  before: state.basis === 'manual' ? state.before : state.basis,
});

/** Mirror of the wizard's resetTierEdit wrapper (~1049-1056). */
const wizardReset = (state: WizardTierState): WizardTierState => {
  const restored = resetTiers(state.tiers, state.suggested, state.before);
  return { tiers: restored.tiers, basis: restored.basis, suggested: null, before: null };
};

const FRESH = { tiers: { ...SUGGESTED }, basis: null, suggested: null, before: null } as WizardTierState;

// ---------------------------------------------------------------------------
// (a) editing one tier -> cost_basis flips to 'manual', reset CTA appears
// ---------------------------------------------------------------------------

describe('tier edit flips the basis and arms the reset CTA', () => {
  it('editing one tier flips cost_basis to manual and snapshots the pre-edit tiers', () => {
    const after = wizardApplyTier(FRESH, 'medium', 9);
    expect(after.basis).toBe('manual');
    expect(after.tiers).toEqual({ small: 1, medium: 9, large: 5 });
    expect(after.suggested).toEqual(SUGGESTED);
    expect(after.before).toBeNull();
  });

  it('the reset CTA renders exactly when basis is manual and a suggestion exists', () => {
    // State side: the gate's two operands are both true after one edit.
    const after = wizardApplyTier(FRESH, 'small', 4);
    expect(after.basis).toBe('manual');
    // The snapshot must be the PRE-edit tiers, not the post-edit ones (which
    // would also be non-null and defeat a weaker `.not.toBeNull()` check).
    expect(after.suggested).toEqual(SUGGESTED);
    // Source side: the Button is gated on exactly those two operands and calls
    // resetTierEdit — a gate that dropped either operand would land here.
    expect(WIZARD_SRC).toMatch(
      /effCostBasis === "manual" && suggestedTiers && \(\s*<Button[\s\S]{0,200}?onClick=\{resetTierEdit\}/,
    );
  });

  it('a second edit does not overwrite the first snapshot or the before-basis', () => {
    const one = wizardApplyTier(FRESH, 'small', 4);
    const two = wizardApplyTier(one, 'large', 42);
    expect(two.suggested).toEqual(SUGGESTED);
    expect(two.before).toBeNull();
    expect(two.tiers).toEqual({ small: 4, medium: 2, large: 42 });
  });

  it('all three tier inputs are required number inputs on the card', () => {
    // One literal renders the three rows via the tier map; `required` + min 0
    // is what blocks an empty field client-side (server: _validate_cost_tiers).
    expect(WIZARD_SRC).toMatch(
      /<Input\s+type="number"\s+min=\{0\}\s+required\s+value=\{current\}/,
    );
  });
});

// ---------------------------------------------------------------------------
// (b) reset -> all 3 tiers back to suggested, basis back to pre-edit
// ---------------------------------------------------------------------------

describe('reset restores the analyzer-suggested tiers and the pre-edit basis', () => {
  it('one edit then reset: all three tiers verbatim back, basis back to estimated', () => {
    const edited = wizardApplyTier(FRESH, 'medium', 9);
    const reset = wizardReset(edited);
    expect(reset.tiers).toEqual(SUGGESTED);
    // null basis = the server's 'estimated' default (omitted from the POST body).
    expect(reset.basis).toBeNull();
    expect(reset.suggested).toBeNull();
    expect(reset.before).toBeNull();
  });

  it('multi-edit then reset still restores all three tiers', () => {
    let state = wizardApplyTier(FRESH, 'small', 0);
    state = wizardApplyTier(state, 'large', 99);
    const reset = wizardReset(state);
    expect(reset.tiers).toEqual(SUGGESTED);
    expect(reset.basis).toBeNull();
  });

  it('edit-mode handoff: reset returns to the basis the node carried, not estimated', () => {
    const EDITED = { tiers: { ...SUGGESTED }, basis: 'calibrated', suggested: null, before: null } as WizardTierState;
    const after = wizardApplyTier(EDITED, 'medium', 7);
    expect(after.before).toBe('calibrated');
    const reset = wizardReset(after);
    expect(reset.tiers).toEqual(SUGGESTED);
    expect(reset.basis).toBe('calibrated');
  });

  it('reset with nothing recorded is a no-op on the tiers', () => {
    const reset = wizardReset(FRESH);
    expect(reset.tiers).toEqual(SUGGESTED);
  });

  it('re-editing after a reset re-arms a fresh snapshot', () => {
    const edited = wizardApplyTier(FRESH, 'medium', 9);
    const reset = wizardReset(edited);
    const again = wizardApplyTier(reset, 'small', 3);
    expect(again.suggested).toEqual(SUGGESTED);
    expect(again.tiers).toEqual({ small: 3, medium: 2, large: 5 });
  });
});

// ---------------------------------------------------------------------------
// Source pin: the real applyTier/resetTierEdit wrapper bodies do the
// bookkeeping the mirrors above assume. Without this pin, every test in (a)
// and (b) only proves the LOCAL mirror is self-consistent — it would stay
// green even if the real wrapper stopped flipping the basis, stopped
// snapshotting suggestedTiers, or stopped restoring basisBeforeEdit.
// ---------------------------------------------------------------------------

function extractArrowFnBody(name: string, params: string): string {
  const re = new RegExp(`const ${name} = \\(${params}\\) => \\{([\\s\\S]*?)\\n  \\};`);
  const m = WIZARD_SRC.match(re);
  expect(m).not.toBeNull();
  return m![1];
}

describe('applyTier / resetTierEdit wrapper bodies do what the mirrors assume', () => {
  it('applyTier snapshots suggestedTiers once, flips the basis, and records the prior basis', () => {
    const body = extractArrowFnBody('applyTier', 'tier: TierKey, value: number');
    expect(body).toMatch(/if \(suggestedTiers === null\) setSuggestedTiers\(costTiers\);/);
    expect(body).toMatch(/setEffCostBasis\("manual"\);/);
    expect(body).toMatch(/setBasisBeforeEdit\(effCostBasis\);/);
    expect(body).toMatch(/setCostTiers\(applyTierEdit\(costTiers, tier, value\)\);/);
  });

  it('resetTierEdit restores tiers/basis via resetTiers(...) and clears the snapshot', () => {
    const body = extractArrowFnBody('resetTierEdit', '');
    expect(body).toMatch(/resetTiers\(costTiers, suggestedTiers, basisBeforeEdit\)/);
    expect(body).toMatch(/setCostTiers\(restored\.tiers\);/);
    expect(body).toMatch(/setEffCostBasis\(restored\.basis\);/);
    expect(body).toMatch(/setSuggestedTiers\(null\);/);
    expect(body).toMatch(/setBasisBeforeEdit\(null\);/);
  });
});

// ---------------------------------------------------------------------------
// (c) empty tier field -> safe default, never NaN/empty in the POST body
// ---------------------------------------------------------------------------

describe('empty tier input cannot poison the build POST body', () => {
  it('the tier onChange coerces the typed value with parseInt(...) || 0', () => {
    expect(WIZARD_SRC).toMatch(
      /onChange=\{\(e\) => applyTier\(tier, parseInt\(e\.target\.value\) \|\| 0\)\}/,
    );
    // The unguarded variant (no || 0) is the bug this pins against.
    expect(/applyTier\(tier, parseInt\(e\.target\.value\)\)/.test(WIZARD_SRC)).toBe(false);
  });

  it('an empty field yields 0, and the tier set stays finite', () => {
    const coerced = parseInt('') || 0;
    expect(coerced).toBe(0);
    const after = wizardApplyTier(FRESH, 'small', coerced);
    const values = [after.tiers.small, after.tiers.medium, after.tiers.large];
    values.forEach((v) => expect(Number.isFinite(v)).toBe(true));
  });

  it('the POST body carries cost_tiers + cost_basis verbatim from the tier state', () => {
    expect(WIZARD_SRC).toMatch(/cost_tiers: costTiers,/);
    expect(WIZARD_SRC).toMatch(/cost_basis: effCostBasis \?\? undefined,/);
    // JSON.stringify drops the undefined basis; a NaN tier would stringify to
    // null — prove the serialized tier set never does for any edit sequence.
    const seq = [wizardApplyTier(FRESH, 'medium', 9), wizardReset(wizardApplyTier(FRESH, 'medium', 9))];
    seq.forEach((s) => {
      const json = JSON.stringify(s.tiers);
      expect(/NaN|null|""/.test(json)).toBe(false);
    });
  });
});
