/**
 * FileBrowser modal a11y (2400LR-b7a).
 *
 * The overlay used to be a bare <div>: screen readers could not tell it was a
 * modal, and the Grid/List view toggles had no state. The product now carries
 * role="dialog" aria-modal="true" aria-labelledby="file-browser-title" on the
 * card, an id on the <h2>, and aria-pressed on both toggles.
 */
import React from 'react';
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react';
import { FileBrowser } from '@/components/file-browser';

beforeEach(() => {
  // fetch is stubbed: the asset list comes back empty, so the modal renders
  // deterministically with no items.
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve([]),
  }) as any;
  if (typeof localStorage !== 'undefined') localStorage.clear();
});

afterEach(() => {
  global.fetch = undefined as any;
});

// The modal keeps its view mode in useState, so it needs a real component
// boundary: RTL's render() of a bare JSX call freezes the hook state and a
// click never re-renders. <App/> gives React the boundary it re-renders on.
function App() {
  return (
    <FileBrowser
      isOpen
      onClose={() => {}}
      accessToken="tok"
      inputType="text"
      allowMultiple={true}
      nodeId="node-1"
    />
  );
}

function openBrowser() {
  render(<App />);
}

describe('FileBrowser modal a11y', () => {
  it('exposes the overlay as a named dialog', () => {
    openBrowser();
    const dialog = screen.getByRole('dialog', { name: 'Select Files' });
    expect(dialog).toBeTruthy();
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAttribute('aria-labelledby', 'file-browser-title');
  });

  it('toggles report their pressed state and flip on click', async () => {
    openBrowser();
    expect(screen.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'List view' })).toHaveAttribute('aria-pressed', 'false');
    await act(async () => {
      screen.getByRole('button', { name: 'List view' }).click();
    });
    expect(screen.getByRole('button', { name: 'List view' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'false');
  });
});

describe('FileBrowser grid card a11y (2400LR-b8y)', () => {
  const NAME = 'my_report-final.v2.png';

  function openWithFile() {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([
        { id: 'f1', name: NAME, cog_type: 'file', parent_id: null },
      ]),
    }) as any;
    render(
      <FileBrowser isOpen onClose={() => {}} accessToken="tok" inputType="file" allowMultiple nodeId="n1" />
    );
  }

  it('grid card is a named button reporting selection', async () => {
    openWithFile();
    const card = await waitFor(() => screen.getByRole('button', { name: NAME }));
    expect(card).toHaveAttribute('tabindex', '0');
    expect(card).toHaveAttribute('aria-pressed', 'false');
    await act(async () => { fireEvent.click(card); });
    expect(screen.getByRole('button', { name: NAME })).toHaveAttribute('aria-pressed', 'true');
  });

  it('Enter and Space run the same handler as click', async () => {
    openWithFile();
    const card = await waitFor(() => screen.getByRole('button', { name: NAME }));
    await act(async () => { fireEvent.keyDown(card, { key: 'Enter' }); });
    expect(screen.getByRole('button', { name: NAME })).toHaveAttribute('aria-pressed', 'true');
    // ctrl-less second activation on an already-selected file keeps it selected or toggles; assert Space is handled
    const ev = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    await act(async () => { card.dispatchEvent(ev); });
    expect(ev.defaultPrevented).toBe(true);
  });
});

describe('FileBrowser grid card nested-interactive (2400LR-b8z)', () => {
  const NAME = 'report-v2.png';

  function openWithFile() {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([
        { id: 'f1', name: NAME, cog_type: 'file', parent_id: null },
      ]),
    }) as any;
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    render(
      <FileBrowser isOpen onClose={() => {}} accessToken="tok" inputType="file" allowMultiple nodeId="n1" />
    );
  }

  it('delete button is NOT nested inside the card button', async () => {
    openWithFile();
    const card = await waitFor(() => screen.getByRole('button', { name: NAME }));
    // Within the card's own DOM subtree, there must be no delete button
    const { queryByRole } = within(card);
    expect(queryByRole('button', { name: /delete/i })).toBeNull();
  });

  it('delete button is a sibling and is independently reachable', async () => {
    openWithFile();
    const deleteBtn = await waitFor(() => screen.getByRole('button', { name: `Delete ${NAME}` }));
    expect(deleteBtn).toBeTruthy();
  });
});
