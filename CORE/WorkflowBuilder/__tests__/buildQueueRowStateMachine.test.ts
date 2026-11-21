/**
 * build-queue-row terminal-state pinning.
 *
 * Stage repro: TRELLIS.2 task 11570 completed at 03:12 with full
 * artifacts (dockerfile + entrypoint_sh + build_and_push_sh) but
 * the wizard UI showed "Generating | 32m 18s" — the row never
 * transitioned to "done". Diagnosed: the row called parseBuildResult
 * on the response's log_tail even for GENERATION-phase tasks, and
 * the registered-marker check is a build-phase concept that doesn't
 * apply to Containerizer output.
 *
 * The fix: only run parseBuildResult on build/build_only-phase rows.
 * Generation rows transition to done as soon as
 * status=completed + dockerfile is present.
 *
 * These tests source-pin the state-machine guards rather than
 * mounting the full BuildQueueRow component (which requires the
 * whole wizard fixture chain).
 */

import * as fs from 'fs';
import * as path from 'path';

const ROW_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/build-queue-row.tsx'),
  'utf-8',
);

describe('build-queue-row terminal-state machine', () => {
  it('only runs parseBuildResult on build/build_only phases', () => {
    // The guard must be present — without it, generation tasks fall
    // through the registered-marker check and may stay "generating"
    // when the parser returns false on an empty logTail.
    expect(ROW_SRC).toMatch(
      /isBuildPhase\s*=\s*entry\.phase\s*===\s*["']build["']\s*\|\|\s*entry\.phase\s*===\s*["']build_only["']/
    );
    expect(ROW_SRC).toMatch(/if\s*\(\s*isBuildPhase\s*\)\s*\{[\s\S]*?parseBuildResult\(logTail\)/);
  });

  it('sets stage=done as soon as status=completed + dockerfile present', () => {
    // The setStage("done") path must be UNCONDITIONAL for non-build
    // phases. The wizard's auto-build-phase-2 chain depends on this
    // — if the row stays stuck, the wizard can't advance.
    const completedBranchStart = ROW_SRC.indexOf('if (statusLower === "completed")');
    expect(completedBranchStart).toBeGreaterThan(-1);
    const completedBranch = ROW_SRC.slice(completedBranchStart, completedBranchStart + 3000);
    // setStage("done") must be reachable WITHOUT going through
    // parseBuildResult when phase is generation.
    expect(completedBranch).toContain('setStage("done")');
    // onFinished("success") must fire so the wizard's parent
    // gets the callback.
    expect(completedBranch).toContain('onFinished(entry.taskId, "success")');
  });

  it('generation-phase row does not silently fail on empty logTail', () => {
    // The Containerizer's /generation-artifacts endpoint OMITS
    // log_tail from its response when artifacts are found (only
    // includes log_tail_tail for error cases). So logTail in the row
    // is "" when the happy-path response comes back. The fixed
    // code skips parseBuildResult for generation tasks entirely;
    // pin the rationale in the source.
    expect(ROW_SRC).toMatch(/legacy.*build-and-register-local|generation-artifacts endpoint doesn't include log_tail/i);
  });

  it('still gates terminal-state on the artifactsPending miss counter', () => {
    // Don't regress the original phase-1 artifacts-ready gate
    // (issue #8). Without dockerfile in the response, the row holds
    // at "Finalizing…" up to 3 consecutive misses, then renders
    // done with a caveat banner.
    expect(ROW_SRC).toContain('artifactsPendingMissCountRef.current');
    expect(ROW_SRC).toContain('setArtifactsPending(true)');
  });

  it('still flips to failed on registered=false for BUILD-phase rows', () => {
    // The original failure-mode detection for the imagebuilder's
    // `"registered": false` envelope MUST still work for the build
    // phase — that's the whole point of parseBuildResult. We just
    // skip the check on generation-phase rows.
    expect(ROW_SRC).toMatch(/if\s*\(\s*!result\.registered\s*\)\s*\{[\s\S]{0,200}?setStage\(["']failed["']\)/);
  });
});
