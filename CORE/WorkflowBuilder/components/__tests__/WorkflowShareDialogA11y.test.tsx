/**
 * WorkflowShareDialog dismiss-button naming (2400LR-b5f).
 *
 * The shared DialogContent already renders an X button named "Close"
 * (sr-only text). The footer dismiss button used to carry the same name,
 * making getByRole('button', { name: 'Close' }) ambiguous. The footer button
 * is now "Cancel", so exactly one "Close" remains.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { WorkflowShareDialog } from '@/components/WorkflowShareDialog';

beforeEach(() => {
  global.fetch = jest.fn().mockImplementation(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve([]) }),
  ) as any;
});

afterEach(() => {
  global.fetch = undefined as any;
});

function renderDialog(onOpenChange: (open: boolean) => void) {
  return render(
    <WorkflowShareDialog
      open
      onOpenChange={onOpenChange}
      workflowId={7}
      workflowName="Demo workflow"
      accessToken="tok"
      apiUrl="http://api.test"
    />,
  );
}

describe('WorkflowShareDialog dismiss buttons', () => {
  it('has exactly one button named Close (the header X)', () => {
    renderDialog(jest.fn());
    expect(screen.getAllByRole('button', { name: 'Close' })).toHaveLength(1);
  });

  it('footer Cancel button calls onOpenChange(false)', () => {
    const onOpenChange = jest.fn();
    renderDialog(onOpenChange);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
