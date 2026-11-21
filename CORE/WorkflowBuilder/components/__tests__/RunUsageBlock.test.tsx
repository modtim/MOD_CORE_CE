/**
 * cUI-3 (30.6.15): the run-detail dialog's "Usage" block.
 *
 * Mock-fetch setup copied from __tests__/RunRailDeletedWorkflow.test.tsx:14-43
 * (counters reset in beforeEach, global.fetch = jest.fn, afterEach clears
 * global.fetch and restores real timers) — extended to serve several URLs
 * because the usage path reads /v1/runs/{id}/result and the CE switch reads
 * /platform/features (lib/platform-features.ts:43).
 *
 * The full dialog is too heavy to mount here (no existing test mounts it; the
 * Radix Dialog needs a click path this harness cannot drive), so — as the
 * brief allows — the block is exported from WorkflowQueueDialog.tsx
 * (`RunUsageBlock`, `fetchRunUsage`) and mounted inside a stand-in dialog body,
 * which is what lets U4 assert the rest of the dialog still renders.
 */
import React from 'react';
import { render } from '@testing-library/react';
import { RunUsageBlock, fetchRunUsage } from '@/components/WorkflowQueueDialog';
import { getPlatformFeatures, _resetPlatformFeaturesCache } from '@/lib/platform-features';

let usageBody: any;
let usageMode: 'ok' | 'reject' | 'http500' | 'http404';
let BILLING = false;

beforeEach(() => {
  usageMode = 'ok';
  usageBody = { compute_seconds: 12.5, nodes: 3, final: true };
  BILLING = false;
  jest.useFakeTimers();
  _resetPlatformFeaturesCache();
  global.fetch = jest.fn((url: any) => {
    const u = String(url);
    const resp = (body: any) => ({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    });
    const fail = (status: number) => ({
      ok: false,
      status,
      json: () => Promise.resolve({}),
    });
    if (u.includes('/runs/42/result')) {
      if (usageMode === 'reject') throw new Error('network down');
      if (usageMode === 'http500') return Promise.resolve(fail(500));
      if (usageMode === 'http404') return Promise.resolve(fail(404));
      return Promise.resolve(resp({ run_id: 42, usage: usageBody }));
    }
    if (u.includes('/platform/features')) {
      return Promise.resolve(resp({ billing_enabled: BILLING }));
    }
    return Promise.resolve(resp([]));
  }) as any;
});

afterEach(() => {
  global.fetch = undefined as any;
  jest.useRealTimers();
});

const drain = async () => {
  for (let i = 0; i < 400; i++) await Promise.resolve();
};

/** Stands in for the dialog's detail view: status row + Files list + the
 *  Usage block between them. */
const mount = async () => {
  const result = await fetchRunUsage(42, 'tok');
  const features = await getPlatformFeatures();
  render(
    <div data-testid="dialog-body">
      <div>Run ID: 42</div>
      {RunUsageBlock({
        entry: { result, terminal: true, fetches: 1 },
        billingEnabled: features?.billing_enabled === true,
      })}
      <div>Files (2): alpha, beta</div>
    </div>
  );
  await drain();
};

const usageText = () => {
  const el = document.querySelector('[data-testid="run-usage"]');
  return el ? el.textContent : '<no block>';
};

const bodyText = () => document.body.textContent;

describe('run-detail usage block', () => {
  it('U1 CE: compute + nodes render, no money words', async () => {
    await mount();
    const text = usageText();
    expect(text).toContain('Compute time');
    expect(text).toContain('Nodes run 3');
    expect(text.includes('Credits')).toBe(false);
    expect(text.includes('Cost')).toBe(false);
    expect(text.includes('$')).toBe(false);
  });

  it('U2 SaaS: exact amount string + currency', async () => {
    BILLING = true;
    usageBody = {
      compute_seconds: 12.5,
      nodes: 3,
      final: true,
      credits_charged: 40,
      cost: { amount: '0.4000', currency: 'USD' },
    };
    await mount();
    const text = usageText();
    expect(text).toContain('Credits 40');
    expect(text).toContain('0.4000 USD');
  });

  it('U3 running: partial label', async () => {
    usageBody = { compute_seconds: 4, nodes: 1, final: false };
    await mount();
    expect(usageText()).toContain('(running, partial)');
  });

  it('U4 fetch rejects: usage unavailable, rest of dialog renders', async () => {
    usageMode = 'reject';
    await mount();
    expect(usageText()).toContain('Usage unavailable');
    expect(bodyText()).toContain('Files (2): alpha, beta');
    expect(bodyText()).toContain('Run ID: 42');
  });

  it('U5 null-dropped body: no crash, no undefined/NaN', async () => {
    BILLING = true;
    usageBody = { compute_seconds: 0, nodes: 0, final: false };
    await mount();
    const text = usageText();
    expect(text.includes('undefined')).toBe(false);
    expect(text.includes('NaN')).toBe(false);
  });
});
