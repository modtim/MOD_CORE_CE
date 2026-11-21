/**
 * Tests for parseBuildResults — the build-queue row reads the
 * ImageBuilder script's structured BUILD_RESULT_JSON: lines from
 * log_tail (issue #2 / node-build-reliability) and renders a
 * per-variant failure card.
 */

import { parseBuildResults } from '@/components/build-queue-row';

describe('parseBuildResults', () => {
  it('returns empty on empty log', () => {
    expect(parseBuildResults('')).toEqual({});
  });

  it('returns empty when no BUILD_RESULT_JSON line is present', () => {
    const log = 'Step 1a: Building canonical...\nStep 1a: Building cuda variant...';
    expect(parseBuildResults(log)).toEqual({});
  });

  it('parses one variant envelope', () => {
    const log = [
      'Step 1a: Building cuda variant (Dockerfile.cuda)...',
      '  Build successful',
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"succeeded","stage":"completed","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}',
    ].join('\n');
    const out = parseBuildResults(log);
    expect(Object.keys(out)).toEqual(['cuda']);
    expect(out.cuda.status).toBe('succeeded');
    expect(out.cuda.dockerfile).toBe('Dockerfile.cuda');
  });

  it('parses multiple variants in one log', () => {
    const log = [
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"succeeded","stage":"completed","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}',
      'BUILD_RESULT_JSON: {"variant":"intel","status":"failed","stage":"build","dockerfile":"Dockerfile.intel","line":17,"command":"pip install torch==2.6.0+xpu","error":"exit 1","error_class":"pip_install_failed"}',
      'BUILD_RESULT_JSON: {"variant":"cpu","status":"failed","stage":"build","dockerfile":"Dockerfile.cpu","line":6,"command":"apt-get install -y libjpeg-dev","error":"exit 100","error_class":"apt_install_failed"}',
    ].join('\n');
    const out = parseBuildResults(log);
    expect(Object.keys(out).sort()).toEqual(['cpu', 'cuda', 'intel']);
    expect(out.intel.status).toBe('failed');
    expect(out.intel.error_class).toBe('pip_install_failed');
    expect(out.cpu.error_class).toBe('apt_install_failed');
    expect(out.cpu.line).toBe(6);
  });

  it('later record for the same variant overwrites earlier', () => {
    // Re-run scenario: the same variant is built twice, the second
    // result is what we want to render.
    const log = [
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"failed","stage":"build","dockerfile":"Dockerfile.cuda","line":1,"command":null,"error":null,"error_class":"from_image_not_found"}',
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"succeeded","stage":"completed","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}',
    ].join('\n');
    const out = parseBuildResults(log);
    expect(out.cuda.status).toBe('succeeded');
  });

  it('skips malformed JSON lines without throwing', () => {
    const log = [
      'BUILD_RESULT_JSON: {not-json}',
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"failed","stage":"build","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}',
    ].join('\n');
    const out = parseBuildResults(log);
    expect(Object.keys(out)).toEqual(['cuda']);
  });

  it('skips entries missing required fields', () => {
    const log = [
      // No variant
      'BUILD_RESULT_JSON: {"status":"failed"}',
      // Wrong status enum
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"weird"}',
      // Real one
      'BUILD_RESULT_JSON: {"variant":"cuda","status":"failed","stage":"build","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}',
    ].join('\n');
    const out = parseBuildResults(log);
    expect(Object.keys(out)).toEqual(['cuda']);
  });

  it('handles BUILD_RESULT_JSON not at line start (defensive)', () => {
    // We intentionally use ^ in the regex — this should NOT match.
    const log = '  BUILD_RESULT_JSON: {"variant":"cuda","status":"succeeded","stage":"completed","dockerfile":"Dockerfile.cuda","line":null,"command":null,"error":null,"error_class":"unknown"}';
    expect(parseBuildResults(log)).toEqual({});
  });
});
