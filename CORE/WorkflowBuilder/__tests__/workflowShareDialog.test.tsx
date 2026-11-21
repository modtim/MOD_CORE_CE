/**
 * Workflow sharing — the WorkflowBuilder "Share" action.
 *
 * The concern this addresses is that sharing a workflow previously did not
 * actually grant access: there was no button and no grant call. These tests
 * assert the two things that make sharing real:
 *
 *   - Share POSTs to the workflow-permission GRANT endpoint with the picked
 *     user and level (the call that actually unlocks see/open/run), not to
 *     some no-op.
 *   - A failed grant surfaces an error and does NOT claim success — a dialog
 *     that says "shared" when the server refused is the worst outcome here.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkflowShareDialog } from '@/components/WorkflowShareDialog';

const API = 'http://api.test';

const jsonOk = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;

const jsonErr = (status: number, body: unknown) =>
  ({ ok: false, status, json: async () => body }) as unknown as Response;

const USERS = [
  { id: 41, email: 'qa-owner@modtest.dev' },
  { id: 42, email: 'qa-user@modtest.dev' },
];

let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
});

function renderOpen() {
  return render(
    <WorkflowShareDialog
      open={true}
      onOpenChange={() => {}}
      workflowId={19}
      workflowName="QA WF"
      accessToken="tok"
      apiUrl={API}
    />,
  );
}

describe('WorkflowShareDialog', () => {
  it('grants the picked user access via the workflow-permission endpoint', async () => {
    fetchMock.mockImplementation((url: string, opts?: RequestInit) => {
      if (String(url).includes('/users/')) return Promise.resolve(jsonOk(USERS));
      if (String(url).includes('/permissions/workflows/19')) {
        return Promise.resolve(
          jsonOk({ workflow_id: 19, user_id: 42, level: 'editor' }),
        );
      }
      throw new Error(`unexpected url ${url}`);
    });

    renderOpen();

    // Users load into the picker.
    await waitFor(() =>
      expect(screen.getByRole('option', { name: 'qa-user@modtest.dev' })).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText('User'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => expect(screen.getByText(/Shared with/i)).toBeInTheDocument());

    const grantCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes('/permissions/workflows/19'),
    );
    expect(grantCall).toBeTruthy();
    expect(grantCall![1].method).toBe('POST');
    expect(JSON.parse(grantCall![1].body)).toEqual({ user_id: 42, level: 'editor' });
  });

  it('shows an error and does not claim success when the grant fails', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/users/')) return Promise.resolve(jsonOk(USERS));
      if (String(url).includes('/permissions/workflows/19')) {
        return Promise.resolve(
          jsonErr(403, { detail: "You don't have permission to share this workflow" }),
        );
      }
      throw new Error(`unexpected url ${url}`);
    });

    renderOpen();

    await waitFor(() =>
      expect(screen.getByRole('option', { name: 'qa-user@modtest.dev' })).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText('User'), { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() =>
      expect(screen.getByText(/don't have permission to share/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Shared with/i)).not.toBeInTheDocument();
  });
});
