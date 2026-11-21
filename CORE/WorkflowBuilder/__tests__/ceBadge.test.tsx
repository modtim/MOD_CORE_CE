/**
 * FX.73 p4c — CE-edition badge (CeBadge) in the WorkflowBuilder palette.
 *
 * The single source of truth per node is the API's `ce_included` flag
 * (node_definitions.ce_included, stamped at CE seed time from the node-seed
 * keep-list) on the palette payload. The badge renders only when that flag is
 * true, and never claims CE for a node the edition does not ship.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { CeBadge, CE_BADGE_STYLE } from '@/components/node-chips';

describe('CeBadge', () => {
  it('renders nothing when ceIncluded is null / undefined / false', () => {
    [null, undefined, false].forEach((value) => {
      const { container } = render(<CeBadge ceIncluded={value} />);
      expect(container.innerHTML).toBe('');
    });
  });

  it('renders the CE badge when the node is CE-included', () => {
    render(<CeBadge ceIncluded />);
    expect(screen.getByText('CE')).toBeInTheDocument();
  });

  it('uses the palette badge style + carries the flag as a data attribute', () => {
    render(<CeBadge ceIncluded />);
    const badge = screen.getByText('CE');
    expect(badge.getAttribute('data-ce-included')).toBe('true');
    expect(badge.className).toContain(CE_BADGE_STYLE);
    expect(badge.getAttribute('title')).toContain('CE (open-source) edition');
  });

  it('appends an optional className for the tighter palette variant', () => {
    render(<CeBadge ceIncluded className="ml-1" />);
    expect(screen.getByText('CE').className).toContain('ml-1');
  });
});
