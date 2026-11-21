/**
 * Tests for lib/build-progress.ts — stage inference from log_tail.
 *
 * The build-queue row uses stageFromLog() to label which phase the
 * Containerizer task is in. Order of LOG_HINTS matters: more-advanced
 * stages must beat earlier ones.
 */

import {
  STAGE_ORDER,
  STAGE_ORDER_GENERATE,
  STAGE_ORDER_BUILD,
  STAGE_LABEL,
  stageFromLog,
  stageIndex,
  stagePercent,
  stagesForPhase,
  stageIndexInPhase,
  stagePercentInPhase,
  phaseHeaderText,
  assertHasPhase,
  applyTransitionToBuild,
  hasPortingStarted,
  visibleStagesForPhase,
  visibleStageIndex,
  visibleStagePercent,
  PORT_LOG_MARKER,
  type TransitionableEntry,
} from '@/lib/build-progress';

describe('STAGE_ORDER', () => {
  it('includes porting between generating and building', () => {
    const iGen = STAGE_ORDER.indexOf('generating');
    const iPort = STAGE_ORDER.indexOf('porting');
    const iBuild = STAGE_ORDER.indexOf('building');
    expect(iGen).toBeGreaterThanOrEqual(0);
    expect(iPort).toBeGreaterThan(iGen);
    expect(iBuild).toBeGreaterThan(iPort);
  });

  it('porting has a distinct label', () => {
    expect(STAGE_LABEL.porting).toBe('Porting CUDA kernels');
  });
});

describe('stageFromLog — porting detection', () => {
  it('returns "porting" when only the port marker is present', () => {
    const log = [
      'Mode:     wrap_image',
      'Generating Albedo-pattern artifacts...',
      ' done.',
      '[port] pass starting (2 variants: rocm, intel)',
    ].join('\n');
    expect(stageFromLog(log)).toBe('porting');
  });

  it('advances past porting once docker build starts', () => {
    // Port marker + build marker: build takes precedence
    const log = [
      '[port] pass starting (1 variants: rocm)',
      'Step 1/3: Building image rocm',
    ].join('\n');
    expect(stageFromLog(log)).toBe('building');
  });

  it('advances past porting all the way to registering', () => {
    const log = [
      '[port] pass starting (1 variants: rocm)',
      'Step 1/3: Building',
      'Step 2/3: Pushing',
      'Step 3/3: Registering',
    ].join('\n');
    expect(stageFromLog(log)).toBe('registering');
  });

  it('stays on generating when no port marker and no build yet', () => {
    const log = 'Mode:     wrap_image\nGenerating Albedo-pattern artifacts...';
    expect(stageFromLog(log)).toBe('generating');
  });

  it('empty log stays queued', () => {
    expect(stageFromLog('')).toBe('queued');
  });
});

describe('stageFromLog — auto-repair loop hints', () => {
  // The δ wizard's test-and-repair stage emits `[test-and-repair]`-
  // prefixed log lines as it iterates. The UI's chip should advance
  // to the new "Auto-repairing" stage when the loop is mid-flight and
  // back to "Pushing" once smoke passes.

  it('attempt log → auto_repairing', () => {
    const log = '[test-and-repair] attempt 3: building image (inner=2/30, outer=0/10)';
    expect(stageFromLog(log)).toBe('auto_repairing');
  });

  it('smoke probe log → auto_repairing', () => {
    const log = '[test-and-repair] attempt 1: smoke probing';
    expect(stageFromLog(log)).toBe('auto_repairing');
  });

  it('calling LLM repair → auto_repairing', () => {
    const log = '[test-and-repair] smoke failed (exit=0, produced_output=False) — calling LLM inner repair #2';
    expect(stageFromLog(log)).toBe('auto_repairing');
  });

  it('outer repair iteration → auto_repairing', () => {
    const log = '[test-and-repair] outer repair 1: Added apt-get install ffmpeg.';
    expect(stageFromLog(log)).toBe('auto_repairing');
  });

  it('PASS after iterations → pushing (next pipeline stage)', () => {
    const log = [
      '[test-and-repair] attempt 1: smoke probing',
      '[test-and-repair] smoke failed (exit=0, produced_output=False) — calling LLM inner repair #1',
      '[test-and-repair] attempt 2: building image',
      '[test-and-repair] attempt 2: smoke probing',
      '[test-and-repair] PASS after 2 attempt(s) (inner=1/30, outer=0/10)',
    ].join('\n');
    expect(stageFromLog(log)).toBe('pushing');
  });

  it('FAIL after exhausting budgets → failed', () => {
    const log = '[test-and-repair] FAIL after 40 attempt(s) (inner=30/30, outer=10/10)';
    expect(stageFromLog(log)).toBe('failed');
  });

  it('does not get stuck on auto_repairing once push-hero starts', () => {
    // Aggregated workflow log: hero loop passes, push-hero starts, the
    // chip should show "Pushing" not "Auto-repairing".
    const log = [
      '[test-and-repair] PASS after 1 attempt(s) (inner=0/30, outer=0/10)',
      '[push_variant] pushing example.com/tenant-1/foo:latest',
    ].join('\n');
    expect(stageFromLog(log)).toBe('pushing');
  });
});

describe('stageFromLog — DAG build-task hints', () => {
  // The new DAG build-task containers emit different log prefixes
  // ([build_variant], [push_variant], [register_node]) than the
  // legacy "Step N/3" ones. Aggregated logs from /build-run-progress
  // need to advance the stage chip off "queued" when these appear.

  it('[build_variant] building → building', () => {
    const log = "[task 14044 (hero) status=running]\n[build_variant] building example.com/tenant-1/trellis2:latest";
    expect(stageFromLog(log)).toBe('building');
  });

  it('[push_variant] → pushing', () => {
    const log = "[task 14044 (hero) status=running]\n[build_variant] success: sha256:abc\n[push_variant] spec=2 variant=hero";
    expect(stageFromLog(log)).toBe('pushing');
  });

  it('[register_node] → registering', () => {
    const log = "[task 14044 status=running]\n[register_node] starting tenant=1";
    expect(stageFromLog(log)).toBe('registering');
  });

  it('[register_node] registered NodeDefinition → scanning (success terminal)', () => {
    // Stage repro: register_node emits
    // `[register_node] registered NodeDefinition id=<n>` on success.
    // The old `Node registered` needle never matched (wrong word
    // order) so the chip stuck at "registering" forever and users
    // thought scanning never finished. New `registered
    // NodeDefinition` needle is the explicit advance signal.
    const log = "[task 2326] [register_node] spec=6\n[task 2326] [register_node] registered NodeDefinition id=119";
    expect(stageFromLog(log)).toBe('scanning');
  });

  it('build hint beats stale Generating hint in the same blob', () => {
    // The aggregated log includes BOTH the early Generating lines AND
    // the later [build_variant] line. The matcher's "first hit wins"
    // ordering must put building above generating so the chip
    // advances, not flips back.
    const log = "Using LLM provider Anthropic\n[build_variant] building stage-harbor:latest";
    expect(stageFromLog(log)).toBe('building');
  });
});

describe('stagePercent', () => {
  it('porting is past generating but short of building in percent', () => {
    const gp = stagePercent('generating');
    const pp = stagePercent('porting');
    const bp = stagePercent('building');
    expect(pp).toBeGreaterThan(gp);
    expect(pp).toBeLessThan(bp);
  });
});

describe('stageIndex', () => {
  it('porting has an index between generating and building', () => {
    expect(stageIndex('porting')).toBeGreaterThan(stageIndex('generating'));
    expect(stageIndex('porting')).toBeLessThan(stageIndex('building'));
  });
});

// ---------------------------------------------------------------------------
// Two-phase model — STAGE_ORDER_GENERATE / STAGE_ORDER_BUILD,
// stagesForPhase, stageIndexInPhase, stagePercentInPhase, phaseHeaderText.
// ---------------------------------------------------------------------------

describe('stagesForPhase', () => {
  it('returns the generate alphabet for "generation"', () => {
    expect(stagesForPhase('generation')).toEqual(['queued', 'generating', 'done']);
    expect(stagesForPhase('generation')).toBe(STAGE_ORDER_GENERATE);
  });

  it('returns the build alphabet for "build"', () => {
    expect(stagesForPhase('build')).toEqual(STAGE_ORDER_BUILD);
    // §6 wizard-as-workflow-native: 'generating' is now part of the
    // build alphabet because phase:"build" rows come from a
    // workflow_run that starts with system-wizard-generate.
    expect(stagesForPhase('build')).toEqual([
      'queued', 'generating', 'porting', 'building', 'auto_repairing',
      'pushing', 'registering', 'scanning', 'done',
    ]);
  });

  it('falls back to the legacy flat order for undefined', () => {
    expect(stagesForPhase(undefined)).toBe(STAGE_ORDER);
    // Flat order = 9 stages (added auto_repairing between building
    // and pushing).
    expect(stagesForPhase(undefined).length).toBe(9);
  });

  it('build alphabet now includes "generating" (§6 — single workflow_run runs generate first)', () => {
    expect(stagesForPhase('build')).toContain('generating');
  });

  it('generate alphabet has no "building" / "pushing" / "scanning" stages', () => {
    const gen = stagesForPhase('generation');
    expect(gen).not.toContain('building');
    expect(gen).not.toContain('pushing');
    expect(gen).not.toContain('scanning');
  });
});

describe('stageIndexInPhase', () => {
  it('returns the stage position within the phase order', () => {
    expect(stageIndexInPhase('generating', 'generation')).toBe(1);
    expect(stageIndexInPhase('done', 'generation')).toBe(2);
    // Build phase: queued=0, generating=1, porting=2, building=3,
    // auto_repairing=4, pushing=5, registering=6, scanning=7, done=8.
    // 'generating' was added to STAGE_ORDER_BUILD in §6 because
    // phase:"build" rows now come from a workflow_run that starts
    // with the system-wizard-generate node (Containerizer LLM).
    expect(stageIndexInPhase('building', 'build')).toBe(3);
    expect(stageIndexInPhase('generating', 'build')).toBe(1);
    expect(stageIndexInPhase('scanning', 'build')).toBe(7);
  });

  it('pegs at the last stage when the stage is not in the phase order (no -1, no crash)', () => {
    // 'building' is not in the generation alphabet — peg at last index, not -1.
    expect(stageIndexInPhase('building', 'generation')).toBe(STAGE_ORDER_GENERATE.length - 1);
    // No throw.
    expect(() => stageIndexInPhase('porting', 'generation')).not.toThrow();
  });

  it('returns -1 for failed (terminal failure marker)', () => {
    expect(stageIndexInPhase('failed', 'generation')).toBe(-1);
    expect(stageIndexInPhase('failed', 'build')).toBe(-1);
  });

  it('uses the legacy order for undefined phase', () => {
    expect(stageIndexInPhase('porting', undefined)).toBe(STAGE_ORDER.indexOf('porting'));
  });
});

describe('stagePercentInPhase', () => {
  it('generating in the generation phase = round(2/3 * 100) = 67', () => {
    // index 1 of 3-stage order → (1+1)/3 = 0.6667 → 67%.
    expect(stagePercentInPhase('generating', 'generation')).toBe(67);
  });

  it('building in the build phase = round(4/9 * 100) = 44', () => {
    // index 3 of 9-stage order (now includes 'generating' at index 1)
    // → (3+1)/9 = 0.444 → 44%.
    expect(stagePercentInPhase('building', 'build')).toBe(44);
  });

  it('done is always 100%, in either phase', () => {
    expect(stagePercentInPhase('done', 'generation')).toBe(100);
    expect(stagePercentInPhase('done', 'build')).toBe(100);
  });

  it('failed is always 100% (caller decides red vs green)', () => {
    expect(stagePercentInPhase('failed', 'generation')).toBe(100);
    expect(stagePercentInPhase('failed', 'build')).toBe(100);
  });

  it('queued at start of generation = round(1/3 * 100) = 33', () => {
    expect(stagePercentInPhase('queued', 'generation')).toBe(33);
  });

  it('queued at start of build = round(1/9 * 100) = 11', () => {
    // 9 stages now (generating added) → (0+1)/9 = 0.111 → 11%.
    expect(stagePercentInPhase('queued', 'build')).toBe(11);
  });
});

describe('assertHasPhase', () => {
  let warnSpy: jest.SpyInstance;

  beforeEach(() => {
    // Each test starts with a fresh mock; the helper's internal Set is
    // module-scoped, so we use unique taskIds per test to avoid the
    // dedup masking subsequent assertions.
    warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    warnSpy.mockRestore();
  });

  it('warns once when phase is undefined', () => {
    assertHasPhase({ taskId: 1001, nodeName: 'demo' });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    const msg = warnSpy.mock.calls[0][0] as string;
    expect(msg).toContain('1001');
    expect(msg).toContain('demo');
  });

  it('does not re-warn for the same taskId on subsequent calls (de-dup)', () => {
    assertHasPhase({ taskId: 1002, nodeName: 'demo2' });
    assertHasPhase({ taskId: 1002, nodeName: 'demo2' });
    assertHasPhase({ taskId: 1002, nodeName: 'demo2' });
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('does not warn when phase is set', () => {
    assertHasPhase({ taskId: 1003, nodeName: 'demo3', phase: 'generation' });
    assertHasPhase({ taskId: 1004, nodeName: 'demo4', phase: 'build' });
    expect(warnSpy).not.toHaveBeenCalled();
  });
});

describe('phaseHeaderText', () => {
  it('returns "Step 1 of 2 · Generate files" for generation', () => {
    expect(phaseHeaderText('generation')).toBe('Step 1 of 2 · Generate files');
  });

  it('returns "Step 2 of 2 · Build & register" for build', () => {
    expect(phaseHeaderText('build')).toBe('Step 2 of 2 · Build & register');
  });

  it('returns "Build & register" (no step prefix) for build_only', () => {
    // Repo (git) flow has no Phase 1 — the row header must NOT lie
    // about a missing "Step 1 of 2" by claiming "Step 2 of 2".
    expect(phaseHeaderText('build_only')).toBe('Build & register');
    expect(phaseHeaderText('build_only')).not.toContain('Step');
  });

  it('returns null for legacy rows without a phase field', () => {
    // Renderer treats null as "no header" — preserves the legacy
    // single-phase chip strip behaviour.
    expect(phaseHeaderText(undefined)).toBeNull();
  });
});

describe('build_only — single-step repo flow', () => {
  it('uses the same 8-stage build alphabet as "build"', () => {
    expect(stagesForPhase('build_only')).toEqual(stagesForPhase('build'));
  });

  it('stagePercentInPhase tracks build_only the same as build', () => {
    expect(stagePercentInPhase('building', 'build_only'))
      .toBe(stagePercentInPhase('building', 'build'));
  });
});

describe('applyTransitionToBuild — Phase 1 row morphs into Phase 2 row in place', () => {
  const phase1Row: TransitionableEntry = {
    taskId: 100,
    nodeName: 'Trellis2 Image to 3D',
    startedAt: 1_700_000_000_000,
    outcome: 'success',
    phase: 'generation',
  };

  it('mutates the existing Phase 1 row in place — no second row spawned', () => {
    const out = applyTransitionToBuild([phase1Row], 100, 200, 'Trellis2 Image to 3D', 1_700_000_900_000);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      taskId: 200,
      nodeName: 'Trellis2 Image to 3D',
      phase: 'build',
      startedAt: 1_700_000_900_000,
      outcome: undefined,
      // Preserved so notification routing can match the morphed row.
      generationTaskId: 100,
    });
  });

  it('preserves nodeName from the Phase 1 row when the caller passes it through', () => {
    const out = applyTransitionToBuild([phase1Row], 100, 200, 'Trellis2 Image to 3D');
    expect(out[0].nodeName).toBe('Trellis2 Image to 3D');
  });

  it('clears the success outcome from the Phase 1 row so the new row tracks fresh state', () => {
    const out = applyTransitionToBuild([phase1Row], 100, 200, 'Trellis2 Image to 3D');
    expect(out[0].outcome).toBeUndefined();
  });

  it('appends a fresh Phase 2 row when no Phase 1 row exists in the queue', () => {
    // Synchronous flow: user never deferred, so no Phase 1 row was created.
    const out = applyTransitionToBuild<TransitionableEntry>([], 100, 200, 'My Node', 1_700_000_000_000);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      taskId: 200,
      nodeName: 'My Node',
      startedAt: 1_700_000_000_000,
      phase: 'build',
      // Even the fresh row needs to carry the gen taskId so a
      // notification can route here instead of reopening the wizard.
      generationTaskId: 100,
    });
  });

  it('is idempotent — calling twice with the same args does not duplicate', () => {
    const once = applyTransitionToBuild([phase1Row], 100, 200, 'Trellis2 Image to 3D');
    const twice = applyTransitionToBuild(once, 100, 200, 'Trellis2 Image to 3D');
    expect(twice).toHaveLength(1);
    expect(twice[0].taskId).toBe(200);
  });

  it('leaves unrelated rows in the queue untouched', () => {
    const otherRow: TransitionableEntry = {
      taskId: 999,
      nodeName: 'Other Node',
      startedAt: 1,
      phase: 'build',
    };
    const out = applyTransitionToBuild([otherRow, phase1Row], 100, 200, 'Trellis2 Image to 3D');
    expect(out).toHaveLength(2);
    // Other row preserved with same identity + position.
    expect(out[0]).toEqual(otherRow);
    // Phase 1 row position-stable — mutated in place at index 1.
    expect(out[1].taskId).toBe(200);
    expect(out[1].phase).toBe('build');
  });

  it('preserves the original generation task id on the morphed row', () => {
    // Notification routing (findBuildRowForNotification) keys off
    // the gen taskId from stale "artifacts ready" notifications.
    // Without this preservation, a click on that notification
    // misses the morphed row and reopens the wizard at Preview,
    // letting the user re-dispatch by mistake.
    const out = applyTransitionToBuild([phase1Row], 100, 200, 'Trellis2 Image to 3D');
    expect(out[0].taskId).toBe(200);
    expect(out[0].generationTaskId).toBe(100);
  });

  it('sets generationTaskId on the fresh row when no Phase 1 exists', () => {
    // Synchronous flow (no Phase 1 row was added before phase 2
    // dispatched). The fresh build row still needs the gen taskId
    // tag so a notification keyed on it can route here.
    const out = applyTransitionToBuild<TransitionableEntry>([], 100, 200, 'Trellis2 Image to 3D');
    expect(out).toHaveLength(1);
    expect(out[0].taskId).toBe(200);
    expect(out[0].generationTaskId).toBe(100);
    expect(out[0].phase).toBe('build');
  });
});

// ---------------------------------------------------------------------------
// Porting-chip visibility — CPU-only nodes must not show "Porting CUDA kernels".
// ---------------------------------------------------------------------------
describe('porting-chip visibility', () => {
  it('hasPortingStarted is false without the marker, true with it', () => {
    expect(hasPortingStarted('')).toBe(false);
    expect(hasPortingStarted(undefined)).toBe(false);
    expect(hasPortingStarted('Generating Albedo-pattern artifacts')).toBe(false);
    expect(hasPortingStarted(`some log\n${PORT_LOG_MARKER} (2 variants: rocm, intel)`)).toBe(true);
  });

  it('hides the porting stage for the build phase when no port pass seen', () => {
    const vis = visibleStagesForPhase('build', { sawPorting: false });
    expect(vis).not.toContain('porting');
    // every other build stage is still present + order preserved
    expect(vis).toEqual(STAGE_ORDER_BUILD.filter((s) => s !== 'porting'));
  });

  it('shows the porting stage once a port pass is observed', () => {
    const vis = visibleStagesForPhase('build', { sawPorting: true });
    expect(vis).toEqual(STAGE_ORDER_BUILD);
    expect(vis).toContain('porting');
  });

  it('also hides porting for legacy (no-phase) and generation strips when unseen', () => {
    expect(visibleStagesForPhase(undefined, { sawPorting: false })).not.toContain('porting');
    // generation strip never had porting anyway — filter is a no-op there
    expect(visibleStagesForPhase('generation', { sawPorting: false }))
      .toEqual(STAGE_ORDER_GENERATE);
  });

  it('index + percent are consistent with the visible (porting-hidden) list', () => {
    // building is index 2 in the hidden list (queued, generating, building, …)
    expect(visibleStageIndex('building', 'build', { sawPorting: false })).toBe(2);
    // and index 3 when porting is shown (queued, generating, porting, building)
    expect(visibleStageIndex('building', 'build', { sawPorting: true })).toBe(3);
    // percent never exceeds 100 and done is 100
    expect(visibleStagePercent('done', 'build', { sawPorting: false })).toBe(100);
    const p = visibleStagePercent('building', 'build', { sawPorting: false });
    expect(p).toBeGreaterThan(0);
    expect(p).toBeLessThanOrEqual(100);
  });
});
