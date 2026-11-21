/**
 * Tests for parseBuildResult — the build-queue row reads the ImageBuilder
 * script's log_tail to detect a registration-rejected build whose task
 * status reports COMPLETED. Without this the row would surface a green
 * "done" for a build that failed to register.
 */

import { parseBuildResult } from '@/components/build-queue-row';

describe('parseBuildResult', () => {
  it('reports registered=true on an empty log', () => {
    expect(parseBuildResult('')).toEqual({ registered: true });
  });

  it('reports registered=true on a clean success log', () => {
    const log = [
      'Step 1a: Building canonical image (Dockerfile)...',
      '  Build successful',
      'Step 1b: Pushing canonical image to harbor.example...',
      '  Push successful',
      'Step 3/3: Registering node definition...',
      '  Registration successful',
      'Done!',
    ].join('\n');
    expect(parseBuildResult(log)).toEqual({ registered: true });
  });

  it('detects "registered: false" in the final-line JSON', () => {
    const log = [
      'Step 3/3: Registering node definition...',
      '  Registration failed (422): {"detail":{"admission_rule":"image-ref-not-allowlisted"}}',
      'Done! Image pushed: stage-harbor.example/tenant-1/x:latest',
      '{"image": "stage-harbor.example/tenant-1/x:latest", "build": "success", "push": "success", "registered": false, "node_name": "X"}',
    ].join('\n');
    const out = parseBuildResult(log);
    expect(out.registered).toBe(false);
    expect(out.reason).toContain('"registered"');
  });

  it('detects "Registration failed" line', () => {
    const log = [
      'Step 3/3: Registering node definition...',
      '  Registration failed (422): {"detail":"image-ref-not-allowlisted"}',
    ].join('\n');
    const out = parseBuildResult(log);
    expect(out.registered).toBe(false);
    expect(out.reason).toContain('Registration failed');
  });

  it('detects PARTIAL SUCCESS with one failed variant', () => {
    const log = [
      'Step 1a: Building intel variant (Dockerfile.intel)...',
      '  BUILD FAILED',
      'PARTIAL SUCCESS: 4/5 variants shipped; 1 failed:',
    ].join('\n');
    const out = parseBuildResult(log);
    expect(out.registered).toBe(false);
    expect(out.reason).toContain('PARTIAL SUCCESS');
  });
});
