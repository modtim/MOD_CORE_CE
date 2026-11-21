/**
 * TriggersDialog create-form label/select association (2400LR-b5c / b5cR).
 *
 * The "Workflow" and "Trigger type" labels in the create-trigger form used to
 * be bare <label>s with no htmlFor, so getByLabel() could not resolve the
 * selects. They now carry htmlFor ids matching the selects.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TriggersDialog } from '@/components/TriggersDialog';

beforeEach(() => {
  // Lists: one existing trigger-less tenant, one workflow, so the form has data.
  global.fetch = jest.fn().mockImplementation((url: string) => {
    const body = String(url).includes('/road/triggers/')
      ? []
      : [{ id: 7, name: 'Demo workflow' }];
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  }) as any;
});

afterEach(() => {
  global.fetch = undefined as any;
});

function App() {
  return (
    <TriggersDialog
      isOpen
      onClose={() => {}}
      accessToken="tok"
      currentWorkflowId={7}
      currentWorkflowName="Demo workflow"
    />
  );
}

describe('TriggersDialog create form a11y', () => {
  it('associates the Workflow and Trigger type labels with their selects', async () => {
    render(<App />);
    const add = await screen.findByRole('button', { name: /add trigger/i });
    fireEvent.click(add);

    await waitFor(() => {
      expect(screen.getByLabelText('Workflow').tagName).toBe('SELECT');
    });
    expect(screen.getByLabelText('Trigger type').tagName).toBe('SELECT');
  });
});
