/**
 * conversational-project-builder §5 — DOM render tests for the conversational
 * Project view. Real jsdom render (not a bundle grep): mounts the panel with a
 * mocked API and asserts the actual rendered DOM for
 *
 *  - §5.1 chat pane + live checklist pane side-by-side
 *  - §5.2 per-task status in the checklist (building highlighted, accepted ticked)
 *  - §5.3 the checkpoint "Test this" hand-off card: what-to-check text, an Open
 *         link to the artifact, an Approve button, and a feedback box
 *  - §4.2 the one-tap intent-override chip row after feedback is classified
 *  - §4.6 the change-scope diff card with a Confirm affordance
 */

import React from 'react';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { ProjectConversationPanel } from '@/components/project-conversation-panel';

// A project parked at a checkpoint: awaiting-user gate + a hand-off + a
// checklist with one accepted, one building, one todo task.
const AWAITING_CONVERSATION = {
  session_id: 's1',
  gate_state: 'awaiting-user',
  turns: [
    { id: 1, author: 'me', role: 'user', body: 'build me a login page' },
    { id: 2, author: 'agent', role: 'system', body: "Here's the plan." },
  ],
  checklist: [
    { id: 10, order_index: 0, task_key: 'login-page', title: 'Login page', status: 'accepted' },
    { id: 11, order_index: 1, task_key: 'dashboard', title: 'Dashboard', status: 'building' },
    { id: 12, order_index: 2, task_key: 'settings', title: 'Settings', status: 'todo' },
  ],
  checkpoint: {
    id: 99,
    checklist_task_id: 11,
    artifact_link: 'http://example.test/preview/dashboard',
    what_to_check: 'the dashboard loads and shows the charts',
  },
};

function mockFetch(handler: (url: string, init?: RequestInit) => any) {
  (global as any).fetch = jest.fn((url: string, init?: RequestInit) => {
    const body = handler(String(url), init);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    });
  });
}

// jsdom does not implement Element.scrollTo; the panel auto-scrolls the chat
// on new turns. Stub it so the effect is a no-op under test.
beforeAll(() => {
  (Element.prototype as any).scrollTo = () => {};
});

describe('ProjectConversationPanel (§5)', () => {
  afterEach(() => jest.restoreAllMocks());

  it('renders the chat pane and the live checklist pane side by side (§5.1/§5.2)', async () => {
    mockFetch((url) => {
      if (url.includes('/support/sessions')) return [];
      if (url.includes('/conversation')) return AWAITING_CONVERSATION;
      return {};
    });

    render(
      <ProjectConversationPanel open onClose={() => {}} accessToken="tok" />,
    );

    // The panel exists as a dialog.
    expect(await screen.findByRole('dialog', { name: /project/i })).toBeInTheDocument();
    // Checklist pane header.
    expect(screen.getByText(/^Checklist$/i)).toBeInTheDocument();
    // Chat pane composer.
    expect(
      screen.getByPlaceholderText(/Convert every uploaded video|Reply to the hand-off/i),
    ).toBeInTheDocument();
  });

  it('shows per-task status and highlights the in-progress task (§5.2)', async () => {
    mockFetch((url) => {
      if (url.includes('/support/sessions')) {
        return [{ id: 's1', kind: 'project', title: 'Login project', created_at: '', last_message_at: new Date().toISOString() }];
      }
      if (url.includes('/conversation')) return AWAITING_CONVERSATION;
      return {};
    });
    render(<ProjectConversationPanel open onClose={() => {}} accessToken="tok" />);

    // Select the project so its checklist loads.
    fireEvent.click(await screen.findByRole('button', { name: /Login project/i }));

    // Each task shows a distinct lifecycle status label.
    expect(await screen.findByText(/^Accepted$/i)).toBeInTheDocument();
    expect(screen.getByText(/^Building$/i)).toBeInTheDocument();
    expect(screen.getByText(/^To do$/i)).toBeInTheDocument();

    // The in-progress (building) task is highlighted — its row carries the
    // blue ring class. Walk up from the status label to the row container.
    const dashboardTitle = screen.getByText('Dashboard');
    const row = dashboardTitle.closest('div.rounded-md');
    expect(row?.className).toMatch(/ring-blue-200/);
  }, 20000);

  it('renders the checkpoint hand-off card, Open link, Approve, feedback box, override chips and scope diff (§5.3/§4.2/§4.6)', async () => {
    // First conversation GET returns the awaiting state; the feedback POST
    // returns a change-scope proposal.
    mockFetch((url, init) => {
      if (url.includes('/support/sessions')) {
        return [{ id: 's1', kind: 'project', title: 'Login project', created_at: '', last_message_at: new Date().toISOString() }];
      }
      if (url.endsWith('/checkpoint/feedback')) {
        return {
          session_id: 's1',
          classified_intent: 'change-scope',
          intent_source: 'reasoner',
          override_options: ['approve', 'infra-broken', 'rework', 'change-scope'],
          action: 'scope-proposed',
          reply: 'Proposed a change.',
          requires_confirm: true,
          proposed_diff: {
            summary: 'Add a reports page',
            adds: [{ title: 'Reports page' }],
            reorders: [],
            removes: [],
          },
        };
      }
      if (url.includes('/conversation')) return AWAITING_CONVERSATION;
      return {};
    });

    render(<ProjectConversationPanel open onClose={() => {}} accessToken="tok" />);

    // Select the project from the rail so the conversation (awaiting) loads.
    const railBtn = await screen.findByRole('button', { name: /Login project/i });
    fireEvent.click(railBtn);

    // §5.3 hand-off card.
    expect(await screen.findByText(/Test this/i)).toBeInTheDocument();
    expect(screen.getByText(/the dashboard loads and shows the charts/i)).toBeInTheDocument();
    const openLink = screen.getByRole('link', { name: /Open/i });
    expect(openLink).toHaveAttribute('href', 'http://example.test/preview/dashboard');
    expect(screen.getByRole('button', { name: /Approve/i })).toBeInTheDocument();

    // Live checklist reflects the three statuses.
    expect(screen.getByText('Login page')).toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText(/^Accepted$/i)).toBeInTheDocument();
    expect(screen.getByText(/^Building$/i)).toBeInTheDocument();

    // Submit feedback → change-scope → override chips + diff card appear.
    const fb = screen.getByPlaceholderText(/Reply in plain language/i);
    fireEvent.change(fb, { target: { value: 'also add a reports page' } });
    // The send button next to the feedback box is the outline variant; grab all
    // buttons and click the one inside the hand-off card region.
    const card = screen.getByText(/Test this/i).closest('div')!.parentElement!;
    const sendBtns = within(card).getAllByRole('button');
    fireEvent.click(sendBtns[sendBtns.length - 1]);

    // §4.2 override chips.
    expect(await screen.findByText(/Read as/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Looks good/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Needs rework/i })).toBeInTheDocument();

    // §4.6 scope diff card + confirm.
    expect(await screen.findByText(/Proposed plan change/i)).toBeInTheDocument();
    expect(screen.getByText(/Add: Reports page/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Confirm change/i })).toBeInTheDocument();
  });
});
