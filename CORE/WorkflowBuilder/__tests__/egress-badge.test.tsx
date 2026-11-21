/**
 * Tests for EgressBadge — renders a worker task's per-task egress report
 * (GB.EGR.11/12): enforced modes get a green badge, unenforced/advisory an
 * amber one with an advisory tooltip, and a non-empty denied list a
 * collapsible "Blocked connections (n)" section with "+ more" when
 * denied_truncated.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EgressBadge, { Egress } from '@/components/egress-badge';

describe('EgressBadge', () => {
  it('returns null when egress is absent', () => {
    const { container } = render(<EgressBadge egress={null} />);
    expect(container.innerHTML).toBe('');
    render(<EgressBadge />);
  });

  it('renders green "Egress: enforced (proxy)" for mode=proxy', () => {
    render(<EgressBadge egress={{ mode: 'proxy', allowed_hosts: ['a.com:443'] }} />);
    expect(screen.getByText('Egress: enforced (proxy)')).toBeTruthy();
  });

  it('renders green "Egress: enforced (Cilium)" for mode=cilium', () => {
    render(<EgressBadge egress={{ mode: 'cilium' }} />);
    expect(screen.getByText('Egress: enforced (Cilium)')).toBeTruthy();
  });

  it('renders amber "Egress not enforced" for mode=unenforced with tooltip', () => {
    const { container } = render(
      <EgressBadge egress={{ mode: 'unenforced', allowed_hosts: ['api.example.com:443'] }} />
    );
    expect(screen.getByText('Egress not enforced')).toBeTruthy();
    // The advisory tooltip text is present in the DOM (revealed on hover).
    expect(container.textContent).toContain('allowlist is advisory');
  });

  it('renders amber "Egress not enforced" for mode=advisory', () => {
    render(<EgressBadge egress={{ mode: 'advisory' }} />);
    expect(screen.getByText('Egress not enforced')).toBeTruthy();
  });

  it('renders a collapsible blocked list with host:port, reason, count', async () => {
    const user = userEvent.setup();
    const egress: Egress = {
      mode: 'proxy',
      denied: [
        { host: 'evil.example.com', port: 443, reason: 'not in allowlist', count: 2 },
        { host: 'evil2.example.com', port: 80, reason: 'not in allowlist', count: 1 },
      ],
      denied_truncated: false,
    };
    render(<EgressBadge egress={egress} />);

    // Collapsed by default: the trigger is visible, the rows are not.
    expect(screen.getByText('Blocked connections (2)')).toBeTruthy();
    expect(screen.queryByText(/evil\.example\.com:443/)).toBeNull();

    await user.click(screen.getByText('Blocked connections (2)'));
    expect(screen.getByText(/evil\.example\.com:443/)).toBeTruthy();
    expect(screen.getByText(/evil2\.example\.com:80/)).toBeTruthy();
    // reason + count surfaced
    expect(screen.getAllByText(/not in allowlist/).length).toBe(2);
    expect(screen.getByText(/×2/)).toBeTruthy();
    // not truncated → no "+ more"
    expect(screen.queryByText('+ more')).toBeNull();
  });

  it('shows "+ more" when denied_truncated', async () => {
    const user = userEvent.setup();
    render(
      <EgressBadge
        egress={{
          mode: 'proxy',
          denied: [{ host: 'blocked.example.com', port: 443, count: 5 }],
          denied_truncated: true,
        }}
      />
    );
    await user.click(screen.getByText('Blocked connections (1)'));
    expect(screen.getByText('+ more')).toBeTruthy();
  });

  it('omits the blocked section when denied is empty', () => {
    render(<EgressBadge egress={{ mode: 'proxy', denied: [] }} />);
    expect(screen.queryByText(/Blocked connections/)).toBeNull();
  });
});
