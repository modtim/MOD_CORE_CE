/**
 * Wizard cluster-capability defaulting.
 *
 * On mount, the new-node wizard fetches /woodsman/wizard/cluster-capabilities
 * and (a) defaults processType to the recommended accelerator when the
 * user hasn't already picked one, and (b) filters the Processing
 * dropdown to capabilities the deployment can actually run.
 *
 * These tests pin the integration:
 *   1. Endpoint shape parsed correctly (3 fields).
 *   2. Recommended capability replaces the legacy "CPU" default.
 *   3. The user's persisted choice wins over the recommendation.
 *   4. Failure is silent — wizard still opens.
 *
 * Source-pinning the useEffect rather than mounting the full component
 * because new-node-wizard is a 4000-line component with heavy
 * dependencies; integration coverage at the component level lives in
 * the manual smoke (MANUAL_TEST_NODE_WIZARD.md).
 */

import * as fs from 'fs';
import * as path from 'path';

const WIZARD_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/new-node-wizard.tsx'),
  'utf-8',
);

describe('new-node-wizard cluster-capability defaulting', () => {
  it('fetches /node-definitions/wizard/cluster-capabilities on mount', () => {
    // The endpoint path must be present in the wizard source — a
    // refactor that drops the fetch would land here first.
    //
    // clean-v2-api moved the wizard endpoints off the legacy
    // /node-definitions family onto /nodes (nodes_builds.router is
    // mounted with an empty prefix in CORE/API/main.py), so the pin
    // tracks the current path rather than the pre-v2 one.
    expect(WIZARD_SRC).toContain('/woodsman/wizard/cluster-capabilities');
  });

  it('uses the shared authenticated fetch, like wizard-preflight', () => {
    // Same auth path as the credit preflight — proves it is not on a
    // different (broken) one.
    //
    // This asserted a literal `Authorization … Bearer` near the URL until the
    // wizard moved onto `authenticatedFetch`, which injects the header AND
    // retries once on a mid-session token expiry. Hand-rolling the header is
    // now the broken path, so the test asserted the wrong half of its own
    // intent: pin the shared helper, not the string it happens to set.
    expect(WIZARD_SRC).toMatch(
      /authenticatedFetch\(\s*`\$\{API_URL\}\/woodsman\/wizard\/cluster-capabilities`/
    );
    // The claim in the name: preflight is on the same path.
    expect(WIZARD_SRC).toMatch(
      /authenticatedFetch\(\s*`\$\{API_URL\}\/nodes\/wizard-preflight`/
    );
    // And nothing in the wizard reaches the network unauthenticated.
    expect(WIZARD_SRC).not.toMatch(/await fetch\(/);
  });

  it('stores the response as clusterCapabilities state', () => {
    // The state hook + type alias keep the rest of the wizard
    // (dropdown filtering, recommendation hint) wired off one source.
    expect(WIZARD_SRC).toContain('setClusterCapabilities');
    expect(WIZARD_SRC).toContain('type ClusterCapabilities');
  });

  it('only overrides processType when the user has not persisted a choice', () => {
    // The "_persisted.processType" check is what protects a user who
    // returns to an in-progress build from having their explicit
    // accelerator choice silently swapped under them.
    expect(WIZARD_SRC).toMatch(
      /if\s*\(\s*!_persisted\.processType\s*&&\s*recommendedUpper\s*!==\s*["']CPU["']\s*\)/
    );
  });

  it('only overrides processType when the current value is still the legacy CPU default', () => {
    // The setProcessType callback shape ensures we don't clobber a
    // value the user picked mid-mount (e.g. clicked "CUDA" before
    // the capabilities call returned).
    expect(WIZARD_SRC).toMatch(
      /setProcessType\(\(curr\)\s*=>\s*\(curr\s*===\s*["']CPU["']\s*\?\s*recommendedUpper\s*:\s*curr\)\)/
    );
  });

  it('uppercases the API response (lower-case "cuda" → wizard state "CUDA")', () => {
    // Wizard state convention is upper-case ("CPU" / "CUDA"); the
    // capability endpoint emits lower-case. The .toUpperCase() conversion
    // bridges that without a separate translation table.
    expect(WIZARD_SRC).toContain('.recommended || "cpu").toUpperCase()');
  });

  it('fails silently — no throw on fetch error keeps the CPU default', () => {
    // A flaky endpoint shouldn't block the wizard from opening.
    // The /* fail-open */ comment marks the intent so a future change
    // doesn't "fix" the catch into a throw.
    const m = WIZARD_SRC.match(
      /wizard\/cluster-capabilities[\s\S]*?catch\s*\{\s*[\s\S]{0,200}?fail-open/
    );
    expect(m).not.toBeNull();
  });
});

describe('new-node-wizard process_type dropdown filtering', () => {
  it('filters dropdown options against clusterCapabilities.available', () => {
    // The dropdown's render block must check `has(cap)` against the
    // available list so the user can't pick an accelerator the
    // datacenter doesn't have.
    expect(WIZARD_SRC).toContain('clusterCapabilities?.available');
    expect(WIZARD_SRC).toContain('has("cuda")');
    expect(WIZARD_SRC).toContain('has("rocm")');
    expect(WIZARD_SRC).toContain('has("intel")');
    expect(WIZARD_SRC).toContain('has("npu")');
  });

  it('falls open to CPU + CUDA when capabilities not yet loaded', () => {
    // The `!avail` clause in the has() helper means a null
    // clusterCapabilities (loading state) shows ALL options — the
    // wizard doesn't gate on a fetch that may still be pending.
    expect(WIZARD_SRC).toContain('!avail || avail.includes(cap)');
  });

  it('shows the recommended-capability hint badge', () => {
    // Operators on a heterogeneous datacenter benefit from seeing
    // which accelerator the wizard picked. The badge surfaces it
    // next to the Processing label, with the LLM-prompt description
    // as a tooltip.
    expect(WIZARD_SRC).toContain('cluster:');
    expect(WIZARD_SRC).toContain('clusterCapabilities.recommended.toUpperCase()');
    expect(WIZARD_SRC).toContain('title={clusterCapabilities.description}');
  });

  it('does NOT show the hint badge when cluster is CPU-only', () => {
    // A CPU-only deployment doesn't need to advertise that fact —
    // the hint is only useful when there's an accelerator to pick.
    expect(WIZARD_SRC).toContain('.recommended.toUpperCase() !== "CPU"');
  });
});
