/**
 * Workflow un-archive — the Load dialog's "Archived" view + per-row Restore.
 *
 * Archive already worked (a workflow could be soft-hidden) but there was no way
 * back: no archived view and no restore action, so an archived workflow was
 * effectively lost from the UI. These tests assert the two things that make
 * un-archiving real:
 *
 *   - The Load dialog exposes an Archived/Active toggle that calls
 *     toggleArchivedView (which re-fetches with include_archived=true).
 *   - In the archived view each row offers Restore (not Archive), and choosing
 *     it calls handleUnarchiveWorkflow for that workflow — the call that clears
 *     archived_at and returns the workflow to the active list.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WorkflowDialogs } from '@/components/WorkflowDialogs';

// Radix DropdownMenu + ScrollArea use pointer/scroll/observer APIs jsdom
// doesn't implement.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

const noop = () => {};

function baseProps(overrides: Record<string, unknown>) {
  return {
    // Only the Load dialog is open; every other dialog stays closed so this
    // renders just the surface under test.
    isSaveDialogOpen: false,
    isExecuting: false,
    isEditDialogOpen: false,
    isImportDialogOpen: false,
    isSanitizationDialogOpen: false,
    isConfirmDeleteDialogOpen: false,
    isConfirmRefreshDialogOpen: false,
    isMissingFileDialogOpen: false,
    setSaveDialogOpen: noop,
    saveDialogMode: 'new',
    newWorkflowName: '',
    setNewWorkflowName: noop,
    handleSaveConfirm: noop,
    isLoadDialogOpen: true,
    setLoadDialogOpen: noop,
    dialogView: 'workflows',
    setDialogView: noop,
    workflows: [],
    versions: [],
    currentWorkflow: null,
    handleWorkflowSelection: noop,
    handleVersionSelection: noop,
    handleDeleteWorkflow: noop,
    handleRenameWorkflow: noop,
    handleDuplicateWorkflow: noop,
    handleArchiveWorkflow: noop,
    handleUnarchiveWorkflow: noop,
    archivedView: false,
    toggleArchivedView: noop,
    handleDeleteVersion: noop,
    isRefreshing: false,
    deletableNodes: [],
    handleDeleteInitiation: noop,
    handleRefreshAllNodes: noop,
    setEditDialogOpen: noop,
    setConfirmDeleteDialogOpen: noop,
    nodeToDelete: null,
    setNodeToDelete: noop,
    handleDeleteConfirm: noop,
    setConfirmRefreshDialogOpen: noop,
    handleRefreshConfirm: noop,
    setMissingFileDialogOpen: noop,
    missingFileNodeName: '',
    importValidation: null,
    pendingImportData: null,
    isImporting: false,
    handleImportConfirm: noop,
    handleImportCancel: noop,
    sanitizationAffectedCount: 0,
    confirmSaveAsIs: noop,
    confirmSaveCleanCopy: noop,
    cancelSanitization: noop,
    ...overrides,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

const ARCHIVED = [
  { id: 42, name: 'cc', latest_version_id: null, archived_at: '2026-09-08T02:00:00Z' },
];

describe('Workflow un-archive UI', () => {
  it('active view offers the Archived toggle and calls it', () => {
    const toggleArchivedView = jest.fn();
    render(<WorkflowDialogs {...baseProps({ archivedView: false, toggleArchivedView })} />);

    const toggle = screen.getByRole('button', { name: /archived/i });
    fireEvent.click(toggle);
    expect(toggleArchivedView).toHaveBeenCalledTimes(1);
  });

  it('archived view titles the dialog and shows an empty state', () => {
    render(<WorkflowDialogs {...baseProps({ archivedView: true, workflows: [] })} />);
    expect(screen.getByText('Archived Workflows')).toBeInTheDocument();
    expect(screen.getByText(/no archived workflows/i)).toBeInTheDocument();
    // The toggle now offers a way back to the active list.
    expect(screen.getByRole('button', { name: /active/i })).toBeInTheDocument();
  });

  it('archived view Restore action calls handleUnarchiveWorkflow for the row', async () => {
    const handleUnarchiveWorkflow = jest.fn();
    render(
      <WorkflowDialogs
        {...baseProps({ archivedView: true, workflows: ARCHIVED, handleUnarchiveWorkflow })}
      />,
    );

    // Open the row's actions menu, then pick Restore. Radix menus open on
    // pointer events, so drive with user-event rather than fireEvent.click.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await user.click(screen.getByRole('button', { name: /actions for cc/i }));
    const restore = await screen.findByText('Restore');
    await user.click(restore);

    await waitFor(() => expect(handleUnarchiveWorkflow).toHaveBeenCalledTimes(1));
    expect(handleUnarchiveWorkflow.mock.calls[0][0]).toMatchObject({ id: 42, name: 'cc' });
  });

  it('active view shows Archive (not Restore) in the row menu', async () => {
    render(
      <WorkflowDialogs
        {...baseProps({
          archivedView: false,
          workflows: [{ id: 30, name: 'active-wf', latest_version_id: 30, archived_at: null }],
        })}
      />,
    );
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await user.click(screen.getByRole('button', { name: /actions for active-wf/i }));
    expect(await screen.findByText('Archive')).toBeInTheDocument();
    expect(screen.queryByText('Restore')).not.toBeInTheDocument();
  });
});
