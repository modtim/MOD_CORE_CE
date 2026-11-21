/**
 * 2400UI129 — CE ships no GET /platform/credit-rate (CORE/API/routers/
 * platform_credit_rate.py is DENIED in open-core/manifest.yaml), so the New
 * Node wizard's once-per-mount tier-hint fetch must not fire there (it 404s
 * and the wizard silently swallows it). The gate is the same edition
 * mechanism 2400UI16 (assistant-panel.tsx) and 0958a48c3
 * (workflow-builder.tsx `isProjectWizardCE`) use: useEntitlements(), hidden
 * ONLY when the block positively reports edition "ce" — FAIL-OPEN otherwise.
 *
 * Mounting the full 7500-line wizard is too heavy (see new-node-wizard-tiers
 * .test.tsx / wizardClusterCapabilities), so — like that suite — we pin the
 * UI wiring against the wizard SOURCE and drive a faithful mirror of the
 * gate + fetch, counting authenticatedFetch calls per edition.
 */

import * as fs from 'fs';
import * as path from 'path';

// Default mock — paid edition, load resolved. Mirrors the 0958a48c3 /
// 2400UI120 tests (data-marking-banner.test.tsx).
const _useEntitlementsDefaultMock = {
  edition: "saas" as const,
  loading: false,
  has: jest.fn(() => true),
  limit: jest.fn(() => null),
  tierName: null,
  resolved: true,
  declared: jest.fn(() => null),
};

// jest.mock hoists above all imports.
jest.mock("@/hooks/use-entitlements", () => ({ useEntitlements: jest.fn(() => _useEntitlementsDefaultMock) }));

import { useEntitlements } from "@/hooks/use-entitlements";

const WIZARD_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/new-node-wizard.tsx'),
  'utf-8',
);

/** Mirror of the wizard's CE gate + once-per-mount credit-rate fetch
 *  (new-node-wizard.tsx ~1060-1120). Returns the call log and the fetch's
 *  promise so the caller can await (or not await, for the loading case). */
const mountCreditRateFetch = (): { calls: string[]; fetch: Promise<void> } => {
  const calls: string[] = [];
  let release: (() => void) | null = null;
  const wait = new Promise<void>((resolve) => { release = resolve; });
  const gate = { wait, release: release ?? (() => {}) };
  let released = false;
  let ce = false;

  const { edition, loading } = useEntitlements();
  if (edition === "ce") ce = true;
  if (!loading && !released) {
    released = true;
    gate.release();
  }

  const fetch = (async () => {
    await gate.wait;
    if (ce) return;
    calls.push('/platform/credit-rate');
  })();
  return { calls, fetch };
};

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('NewNodeWizard tier-hint fetch — 2400UI129 CE edition carve', () => {
  afterEach(() => {
    // Restore the default (paid) mock so other suites aren't affected.
    useEntitlements.mockRestore();
  });

  it('CE edition: zero /platform/credit-rate calls', async () => {
    useEntitlements.mockReturnValue({
      edition: "ce",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    const m = mountCreditRateFetch();
    await m.fetch;
    expect(m.calls).toEqual([]);
  });

  it('CE edition while the block is still loading: still zero calls', async () => {
    useEntitlements.mockReturnValue({
      edition: "ce",
      loading: true,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: false,
      declared: jest.fn(() => null),
    });
    const m = mountCreditRateFetch();
    await tick(10);
    expect(m.calls).toEqual([]);
  });

  it('paid edition (saas): exactly one call', async () => {
    useEntitlements.mockReturnValue({
      edition: "saas",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    const m = mountCreditRateFetch();
    await m.fetch;
    expect(m.calls).toEqual(['/platform/credit-rate']);
  });

  it('unresolved/fail-open (edition null): still one call — CE carve never locks paid', async () => {
    useEntitlements.mockReturnValue({
      edition: null,
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: false,
      declared: jest.fn(() => null),
    });
    const m = mountCreditRateFetch();
    await m.fetch;
    expect(m.calls).toEqual(['/platform/credit-rate']);
  });

  it('the wizard source wires the gate to useEntitlements, not a new mechanism', () => {
    expect(WIZARD_SRC).toContain('import { useEntitlements } from "@/hooks/use-entitlements";');
    expect(WIZARD_SRC).toMatch(
      /const \{ edition: creditRateEdition, loading: creditRateLoading \} = useEntitlements\(\);/,
    );
    expect(WIZARD_SRC).toMatch(/const creditRateCE = creditRateEdition === "ce";/);
    expect(WIZARD_SRC).toMatch(
      /await creditRateGateRef\.current!\.wait;\s*if \(cancelled \|\| creditRateCeRef\.current\) return;/,
    );
  });
});
