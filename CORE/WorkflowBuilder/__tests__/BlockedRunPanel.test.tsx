/**
 * GG.UI.6 — blocked-run CTA panel.
 *
 * The API refuses a run that needs a paid node/workflow the tenant doesn't
 * own (402 paywall / 403 marketplace guard, plain-string detail from
 * services/entitlement_guard.py). These tests pin the mapping from the exact
 * API refusal messages to the rendered Buy / Try / Ask-admin CTAs and the
 * /store/{kind}/{id} link, so a wording change on either side fails loudly
 * instead of silently hiding the CTA.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import BlockedRunPanel from '@/components/workflow/BlockedRunPanel';
import { parseBlockedRunError, blockedRunStoreHref, mapPreflightItems } from '@/lib/blocked-run';

const PAYWALL = "HTTPException: \"Node 'vsr_maxine' must be purchased before it can run\"";
// GG.UI.6b — dispatch-time refusal strings now carry the definition id as a
// trailing " (id <n>)" segment.
const PAYWALL_WITH_ID = "HTTPException: \"Node 'vsr_maxine' must be purchased before it can run (id 47)\"";
const SaaS_GUARD_WITH_ID = "HTTPException: \"Node 'Video Super Resolution' is a paid marketplace node and tenant has no active entitlement (id 51)\"";
const SaaS_GUARD = "HTTPException: \"Node 'Video Super Resolution' is a paid marketplace node and tenant has no active entitlement\"";
const WORKFLOW = "HTTPException: \"Workflow listing 42 is a listing that needs an add-on and tenant has no active entitlement\"";
const LICENCE = 'HTTPException: "licence invalid"';
const UNRELATED = 'HTTPException: "Cog or version not found"';

describe('parseBlockedRunError — reason payload → blocked-run reason', () => {
  it('maps the 402 paywall message to a node_paywall reason', () => {
    const r = parseBlockedRunError(PAYWALL);
    expect(r).not.toBeNull();
    expect(r!.code).toBe('node_paywall');
    expect(r!.label).toBe("Node 'vsr_maxine'");
    expect(r!.kind).toBe('node');
    expect(r!.trialAvailable).toBe(false);
  });

  it('maps the 403 SaaS marketplace-guard message', () => {
    const r = parseBlockedRunError(SaaS_GUARD);
    expect(r).not.toBeNull();
    expect(r!.code).toBe('node_paywall');
    expect(r!.label).toBe('Node \'Video Super Resolution\'');
  });

  it('maps the GG.1 paid-workflow-listing message to a workflow reason with id', () => {
    const r = parseBlockedRunError(WORKFLOW);
    expect(r).not.toBeNull();
    expect(r!.code).toBe('workflow_paid');
    expect(r!.kind).toBe('workflow');
    expect(r!.id).toBe(42);
  });

  it('does NOT treat the on-prem "licence invalid" refusal as a node paywall', () => {
    expect(parseBlockedRunError(LICENCE)).toBeNull();
  });

  it('returns null for unrelated run failures', () => {
    expect(parseBlockedRunError(UNRELATED)).toBeNull();
    expect(parseBlockedRunError(null)).toBeNull();
    expect(parseBlockedRunError(undefined)).toBeNull();
    expect(parseBlockedRunError('')).toBeNull();
  });

  it('captures the trailing (id <n>) definition id on paywall refusals (GG.UI.6b)', () => {
    const r = parseBlockedRunError(PAYWALL_WITH_ID);
    expect(r).not.toBeNull();
    expect(r!.label).toBe("Node 'vsr_maxine'");
    expect(r!.kind).toBe('node');
    expect(r!.id).toBe(47);
    expect(blockedRunStoreHref(r!)).toBe('/store/node/47');
  });

  it('captures the trailing (id <n>) definition id on SaaS marketplace refusals', () => {
    const r = parseBlockedRunError(SaaS_GUARD_WITH_ID);
    expect(r).not.toBeNull();
    expect(r!.label).toBe("Node 'Video Super Resolution'");
    expect(r!.id).toBe(51);
    expect(blockedRunStoreHref(r!)).toBe('/store/node/51');
  });

  it('falls back to the bare node listing when no id suffix is present', () => {
    const r = parseBlockedRunError(PAYWALL);
    expect(r!.id).toBeNull();
    expect(blockedRunStoreHref(r!)).toBe('/store/node');
  });
});

describe('mapPreflightItems — structured 402 purchase_required items (GG.UI.6b)', () => {
  it('maps a node item with a precise id to a node_paywall reason', () => {
    const reasons = mapPreflightItems([
      { kind: 'node', id: 47, name: 'vsr_maxine', node_kind: 'vsr_maxine', trial_available: false },
    ]);
    expect(reasons).toHaveLength(1);
    expect(reasons[0].code).toBe('node_paywall');
    expect(reasons[0].label).toBe("Node 'vsr_maxine'");
    expect(reasons[0].kind).toBe('node');
    expect(reasons[0].id).toBe(47);
    expect(reasons[0].trialAvailable).toBe(false);
    expect(blockedRunStoreHref(reasons[0])).toBe('/store/node/47');
  });

  it('maps a workflow item to a workflow_paid reason linking /store/workflow/{id}', () => {
    const reasons = mapPreflightItems([
      { kind: 'workflow', id: 42, name: 'Workflow listing 42', node_kind: null, trial_available: false },
    ]);
    expect(reasons).toHaveLength(1);
    expect(reasons[0].code).toBe('workflow_paid');
    expect(reasons[0].label).toBe('Workflow listing 42');
    expect(reasons[0].kind).toBe('workflow');
    expect(reasons[0].id).toBe(42);
    expect(blockedRunStoreHref(reasons[0])).toBe('/store/workflow/42');
  });

  it('keeps the Try CTA flag from the item and falls back to the node listing without an id', () => {
    const withTrial = mapPreflightItems([
      { kind: 'node', id: null, name: 'Demo Node', node_kind: 'demo_node', trial_available: true },
    ])[0];
    expect(withTrial.trialAvailable).toBe(true);
    expect(withTrial.id).toBeNull();
    expect(blockedRunStoreHref(withTrial)).toBe('/store/node');
  });

  it('tolerates missing/malformed input', () => {
    expect(mapPreflightItems(null)).toEqual([]);
    expect(mapPreflightItems(undefined)).toEqual([]);
    expect(mapPreflightItems([])).toEqual([]);
    expect(mapPreflightItems([null, 'x', 42] as unknown as Parameters<typeof mapPreflightItems>[0])).toEqual([]);
  });

  it('renders through BlockedRunPanel with precise Buy links', () => {
    const reasons = mapPreflightItems([
      { kind: 'node', id: 47, name: 'vsr_maxine', node_kind: 'vsr_maxine', trial_available: false },
      { kind: 'workflow', id: 42, name: 'Workflow listing 42', node_kind: null, trial_available: false },
    ]);
    render(<BlockedRunPanel reasons={reasons} />);
    expect(screen.getByTestId('blocked-run-panel')).toBeInTheDocument();
    expect(screen.getAllByTestId('blocked-run-item')).toHaveLength(2);
    const buys = screen.getAllByTestId('blocked-run-buy') as HTMLAnchorElement[];
    expect(buys[0]).toHaveAttribute('href', '/store/node/47');
    expect(buys[1]).toHaveAttribute('href', '/store/workflow/42');
  });
});

describe('blockedRunStoreHref — store link', () => {
  it('links node refusals (no definition id in the API message) to the node listing', () => {
    const r = parseBlockedRunError(PAYWALL)!;
    expect(blockedRunStoreHref(r)).toBe('/store/node');
  });

  it('links workflow-listing refusals straight to the detail page', () => {
    const r = parseBlockedRunError(WORKFLOW)!;
    expect(blockedRunStoreHref(r)).toBe('/store/workflow/42');
  });
});

describe('BlockedRunPanel — rendered CTAs', () => {
  it('shows one row per blocked item with a Buy link to the store', () => {
    render(<BlockedRunPanel reasons={[parseBlockedRunError(PAYWALL)!]} />);
    const panel = screen.getByTestId('blocked-run-panel');
    expect(panel).toBeInTheDocument();
    const item = screen.getByTestId('blocked-run-item');
    expect(item).toHaveTextContent("Needs Node 'vsr_maxine':");
    const buy = screen.getByTestId('blocked-run-buy') as HTMLAnchorElement;
    expect(buy).toHaveTextContent('Buy');
    expect(buy).toHaveAttribute('href', '/store/node');
  });

  it('hides Try unless the reason says a trial is available', () => {
    const { rerender } = render(<BlockedRunPanel reasons={[parseBlockedRunError(PAYWALL)!]} />);
    expect(screen.queryByTestId('blocked-run-try')).toBeNull();

    const withTrial: import('@/lib/blocked-run').BlockedRunReason = {
      code: 'node_paywall',
      label: "Node 'demo'",
      kind: 'node',
      id: null,
      trialAvailable: true,
      detail: "Node 'demo' must be purchased before it can run — trial available",
    };
    rerender(<BlockedRunPanel reasons={[withTrial]} />);
    // CE: the trial link is not rendered (rank 3 row above)
    expect(screen.queryByTestId('blocked-run-try')).toBeNull();
  });

  it('shows "Ask your admin" for regular users, not for admins', () => {
    const { rerender } = render(<BlockedRunPanel reasons={[parseBlockedRunError(SaaS_GUARD)!]} />);
    expect(screen.getByTestId('blocked-run-ask-admin')).toHaveTextContent('Ask your admin');

    rerender(<BlockedRunPanel reasons={[parseBlockedRunError(SaaS_GUARD)!]} isAdmin />);
    expect(screen.queryByTestId('blocked-run-ask-admin')).toBeNull();
  });

  it('links the workflow-listing row to /store/workflow/{id}', () => {
    render(<BlockedRunPanel reasons={[parseBlockedRunError(WORKFLOW)!]} />);
    expect(screen.getByTestId('blocked-run-item')).toHaveTextContent('Needs Workflow listing 42:');
    expect(screen.getByTestId('blocked-run-buy')).toHaveAttribute('href', '/store/workflow/42');
  });

  it('renders nothing for an empty reason list', () => {
    const { container } = render(<BlockedRunPanel reasons={[]} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId('blocked-run-panel')).toBeNull();
  });
});
