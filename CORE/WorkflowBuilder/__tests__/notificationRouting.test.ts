/**
 * Tests for findBuildRowForNotification — the helper that decides
 * whether a notification click should jump to an existing build-queue
 * row's logs (when a Phase-2 build row exists) or fall through to the
 * wizard-resume flow.
 *
 * The helper reads localStorage under `BUILDS_STORAGE_KEY` (mirrored
 * from workflow-builder.tsx); these tests seed that key with a mock
 * queue and assert the right taskId is returned for each scenario.
 */

import { findBuildRowForNotification } from '@/components/notifications-bell';

const KEY = 'mod.buildQueue.v1';

function setQueue(entries: Array<{
  taskId: number;
  nodeName: string;
  startedAt?: number;
  outcome?: 'success' | 'failed';
  phase?: 'generation' | 'build';
}>) {
  window.localStorage.setItem(KEY, JSON.stringify(entries));
}

beforeEach(() => {
  window.localStorage.clear();
});

describe('findBuildRowForNotification', () => {
  it('returns null when localStorage queue is empty', () => {
    expect(findBuildRowForNotification(500, 'demo', KEY)).toBeNull();
  });

  it('returns null when only a generation row exists (no Phase-2 yet)', () => {
    setQueue([
      { taskId: 500, nodeName: 'demo', phase: 'generation', outcome: 'success' },
    ]);
    expect(findBuildRowForNotification(500, 'demo', KEY)).toBeNull();
  });

  it('finds the matching Phase-2 build row by nodeName via the gen row', () => {
    setQueue([
      { taskId: 500, nodeName: 'demo', phase: 'generation', outcome: 'success' },
      { taskId: 501, nodeName: 'demo', phase: 'build' },
    ]);
    expect(findBuildRowForNotification(500, '', KEY)).toBe(501);
  });

  it('falls back to notificationFallback when no gen row matches resumeTaskId', () => {
    setQueue([
      { taskId: 999, nodeName: 'other', phase: 'build' },
      { taskId: 1001, nodeName: 'demo', phase: 'build' },
    ]);
    // resumeTaskId is null (notification has no resume= hint), but the
    // fallback ('demo') matches a build row's nodeName.
    expect(findBuildRowForNotification(null, 'demo', KEY)).toBe(1001);
  });

  it('prefers the most recent build row when multiple match', () => {
    setQueue([
      { taskId: 100, nodeName: 'demo', phase: 'build' },
      { taskId: 200, nodeName: 'demo', phase: 'build' },
      { taskId: 300, nodeName: 'demo', phase: 'build' },
    ]);
    expect(findBuildRowForNotification(null, 'demo', KEY)).toBe(300);
  });

  it('returns null when no build row matches', () => {
    setQueue([
      { taskId: 700, nodeName: 'completely-different', phase: 'build' },
    ]);
    expect(findBuildRowForNotification(null, 'demo', KEY)).toBeNull();
  });

  it('ignores legacy entries without a phase field', () => {
    setQueue([
      // Legacy row — no phase. MUST NOT be matched as a build row.
      { taskId: 800, nodeName: 'demo' },
    ]);
    expect(findBuildRowForNotification(null, 'demo', KEY)).toBeNull();
  });
});
