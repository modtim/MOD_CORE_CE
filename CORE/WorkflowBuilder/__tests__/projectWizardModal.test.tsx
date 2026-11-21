/**
 * WS4.Q5 — DOM render tests for the (legacy) Project Wizard modal.
 *
 * With `project_conversation_enabled` false, the Project Wizard renders the
 * legacy one-shot flow. Asserts the modal is a real dialog (role="dialog",
 * labelled "Project Wizard", centred/floating shell) and that the step
 * prompts are the tightened copy: the heading, the "end result" helper text,
 * and the concrete placeholder in the composer.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { ProjectWizardPanel } from '@/components/project-wizard-panel';

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

describe('ProjectWizardPanel (legacy modal, WS4.Q5)', () => {
  afterEach(() => jest.restoreAllMocks());

  it('renders as a labelled role="dialog" with the centred/floating shell', async () => {
    mockFetch((url) => {
      if (url.includes('/platform/feature-config')) {
        return { project_conversation_enabled: false };
      }
      if (url.includes('/support/sessions')) return [];
      return {};
    });

    render(<ProjectWizardPanel open onClose={() => {}} accessToken="tok" />);

    // The modal shell: a labelled, modal dialog.
    const dialog = await screen.findByRole('dialog', { name: /wizard - projects/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');

    // The centred/floating shell (matches Stack Wizard / other modals). The
    // panel portals to <body>, so the backdrop is the dialog's parent rather
    // than something inside the RTL container.
    const shell = dialog.parentElement;
    expect(shell).toBeTruthy();
    expect(shell?.className).toMatch(/fixed/);
    expect(shell?.className).toMatch(/items-center/);
    expect(shell?.className).toMatch(/justify-center/);

    // Header lives inside the dialog.
    const header = within(dialog).getByText('Wizard - Projects');
    expect(header).toBeInTheDocument();
  });

  it('shows the tightened step prompts (heading, helper text, placeholder)', async () => {
    mockFetch((url) => {
      if (url.includes('/platform/feature-config')) {
        return { project_conversation_enabled: false };
      }
      if (url.includes('/support/sessions')) return [];
      return {};
    });

    render(<ProjectWizardPanel open onClose={() => {}} accessToken="tok" />);

    const dialog = await screen.findByRole('dialog', { name: /wizard - projects/i });

    // Step title + helper: state what to enter and why.
    const heading = within(dialog).getByText('What should MOD build for you?');
    expect(heading).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Describe the end result in one or two sentences/i),
    ).toBeInTheDocument();

    // Concrete composer placeholder (a real example, not "Describe what you need…").
    expect(
      within(dialog).getByPlaceholderText(/Convert every uploaded video to 1080p MP4/i),
    ).toBeInTheDocument();
  });

  it('does not offer "Work on it" or a sample until a session exists', async () => {
    mockFetch((url) => {
      if (url.includes('/platform/feature-config')) {
        return { project_conversation_enabled: false };
      }
      if (url.includes('/support/sessions')) return [];
      return {};
    });

    render(<ProjectWizardPanel open onClose={() => {}} accessToken="tok" />);

    const dialog = await screen.findByRole('dialog', { name: /wizard - projects/i });

    // Attach sample is present but disabled (no session yet); Work on it absent.
    const attach = within(dialog).getByRole('button', { name: /attach sample/i });
    expect(attach).toBeDisabled();
    expect(within(dialog).queryByRole('button', { name: /work on it/i })).toBeNull();
  });
});
